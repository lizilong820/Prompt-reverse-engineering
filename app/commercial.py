from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, UploadFile
from starlette.datastructures import Headers
from pydantic import BaseModel

from app.main import DATA_DIR, IMAGE_TYPES, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, VIDEO_TYPES, analyze_media_upload

logger = logging.getLogger("prompt-lens.jobs")

WX_APP_ID = os.getenv("WX_APP_ID", "").strip()
WX_APP_SECRET = os.getenv("WX_APP_SECRET", "").strip()
ENABLE_DEV_LOGIN = os.getenv("ENABLE_DEV_LOGIN", "false").lower() == "true"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
WELCOME_CREDITS = int(os.getenv("WELCOME_CREDITS", "3"))
IMAGE_CREDIT_COST = int(os.getenv("IMAGE_CREDIT_COST", "1"))
VIDEO_CREDIT_COST = int(os.getenv("VIDEO_CREDIT_COST", "3"))
AD_REWARD_CREDITS = int(os.getenv("AD_REWARD_CREDITS", "1"))
AD_DAILY_LIMIT = int(os.getenv("AD_DAILY_LIMIT", "5"))
AD_COOLDOWN_SECONDS = int(os.getenv("AD_COOLDOWN_SECONDS", "60"))
DB_PATH = Path(DATA_DIR) / "commercial.sqlite3"

commercial_router = APIRouter(prefix="/api/v1", tags=["mini-program"])


class LoginRequest(BaseModel):
    code: str


class RewardCompleteRequest(BaseModel):
    claim_token: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            openid TEXT NOT NULL UNIQUE,
            unionid TEXT,
            credits INTEGER NOT NULL DEFAULT 0 CHECK(credits >= 0),
            is_blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS credit_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason TEXT NOT NULL,
            reference_type TEXT,
            reference_id TEXT,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reward_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('issued','claimed','expired')),
            expires_at TEXT NOT NULL,
            claimed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            mode TEXT NOT NULL,
            filename TEXT NOT NULL,
            cost INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('processing','succeeded','failed')),
            idempotency_key TEXT NOT NULL,
            result_json TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON credit_ledger(user_id, id DESC);
    """)
    try:
        db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return db


def recover_interrupted_jobs() -> None:
    """Refund jobs left in processing after an unclean service restart."""
    with connect() as db:
        rows = db.execute("SELECT id,user_id,cost FROM jobs WHERE status='processing'").fetchall()
        if not rows:
            return
        db.execute("BEGIN IMMEDIATE")
        for row in rows:
            change_credits(db, row["user_id"], row["cost"], "job_refund", "job", str(row["id"]), f"job:refund:{row['id']}")
            db.execute("UPDATE jobs SET status='failed', error_message='服务重启导致任务中断，积分已退回', updated_at=? WHERE id=?", (utc_now().isoformat(), row["id"]))
        db.commit()
    media_dir = Path(DATA_DIR) / "job-media"
    if media_dir.exists():
        for path in media_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def change_credits(db: sqlite3.Connection, user_id: int, amount: int, reason: str, reference_type: str, reference_id: str, idempotency_key: str) -> int:
    existing = db.execute("SELECT balance_after FROM credit_ledger WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing:
        return int(existing["balance_after"])
    row = db.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    balance = int(row["credits"]) + amount
    if balance < 0:
        raise HTTPException(status_code=402, detail="积分不足，请观看激励广告或购买积分")
    now = utc_now().isoformat()
    db.execute("UPDATE users SET credits=?, updated_at=? WHERE id=?", (balance, now, user_id))
    db.execute("INSERT INTO credit_ledger(user_id,amount,balance_after,reason,reference_type,reference_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (user_id, amount, balance, reason, reference_type, reference_id, idempotency_key, now))
    return balance


async def exchange_code(code: str) -> tuple[str, str | None]:
    if ENABLE_DEV_LOGIN and code.startswith("dev:"):
        return "dev_" + hashlib.sha256(code.encode()).hexdigest()[:24], None
    if not WX_APP_ID or not WX_APP_SECRET:
        raise HTTPException(status_code=503, detail="微信小程序 AppID/AppSecret 尚未配置")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("https://api.weixin.qq.com/sns/jscode2session", params={"appid": WX_APP_ID, "secret": WX_APP_SECRET, "js_code": code, "grant_type": "authorization_code"})
    payload = response.json()
    if response.is_error or payload.get("errcode") or not payload.get("openid"):
        raise HTTPException(status_code=401, detail="微信登录凭证无效")
    return payload["openid"], payload.get("unionid")


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    with connect() as db:
        row = db.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?", (hash_token(authorization[7:]), utc_now().isoformat())).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期")
    if row["is_blocked"]:
        raise HTTPException(status_code=403, detail="账户已被限制使用")
    return dict(row)


@commercial_router.post("/auth/wechat")
async def wechat_login(body: LoginRequest) -> dict[str, Any]:
    openid, unionid = await exchange_code(body.code)
    token = secrets.token_urlsafe(32)
    now = utc_now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
        if not user:
            cursor = db.execute("INSERT INTO users(openid,unionid,credits,created_at,updated_at) VALUES(?,?,?,?,?)", (openid, unionid, 0, now.isoformat(), now.isoformat()))
            user_id = int(cursor.lastrowid)
            change_credits(db, user_id, WELCOME_CREDITS, "welcome_bonus", "user", str(user_id), f"welcome:{user_id}")
        else:
            user_id = int(user["id"])
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now.isoformat(),))
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (hash_token(token), user_id, (now + timedelta(days=SESSION_DAYS)).isoformat(), now.isoformat()))
        credits = db.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()["credits"]
        db.commit()
    return {"token": token, "expires_in": SESSION_DAYS * 86400, "user": {"id": user_id, "credits": credits}, "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST}, "ad": {"reward": AD_REWARD_CREDITS, "daily_limit": AD_DAILY_LIMIT}}


@commercial_router.get("/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    today = utc_now().date().isoformat()
    with connect() as db:
        used = db.execute("SELECT COUNT(*) FROM reward_claims WHERE user_id=? AND status='claimed' AND substr(claimed_at,1,10)=?", (user["id"], today)).fetchone()[0]
    return {"id": user["id"], "credits": user["credits"], "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST}, "ad": {"reward": AD_REWARD_CREDITS, "remaining_today": max(0, AD_DAILY_LIMIT - used)}}


@commercial_router.post("/rewards/ad/prepare")
async def prepare_ad_reward(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    now = utc_now()
    token = secrets.token_urlsafe(32)
    with connect() as db:
        claimed = db.execute("SELECT COUNT(*) FROM reward_claims WHERE user_id=? AND status='claimed' AND substr(claimed_at,1,10)=?", (user["id"], now.date().isoformat())).fetchone()[0]
        if claimed >= AD_DAILY_LIMIT:
            raise HTTPException(status_code=429, detail="今日广告奖励次数已用完")
        latest = db.execute("SELECT created_at FROM reward_claims WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if latest and now - datetime.fromisoformat(latest["created_at"]) < timedelta(seconds=AD_COOLDOWN_SECONDS):
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
        db.execute("UPDATE reward_claims SET status='expired' WHERE status='issued' AND expires_at<=?", (now.isoformat(),))
        db.execute("INSERT INTO reward_claims(user_id,token_hash,status,expires_at,created_at) VALUES(?,?,?,?,?)", (user["id"], hash_token(token), "issued", (now + timedelta(minutes=10)).isoformat(), now.isoformat()))
    return {"claim_token": token, "expires_in": 600}


@commercial_router.post("/rewards/ad/complete")
async def complete_ad_reward(body: RewardCompleteRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    now = utc_now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        claim = db.execute("SELECT * FROM reward_claims WHERE token_hash=? AND user_id=?", (hash_token(body.claim_token), user["id"])).fetchone()
        if not claim or claim["status"] != "issued" or claim["expires_at"] <= now.isoformat():
            raise HTTPException(status_code=409, detail="奖励凭证无效、已使用或已过期")
        claimed_today = db.execute("SELECT COUNT(*) FROM reward_claims WHERE user_id=? AND status='claimed' AND substr(claimed_at,1,10)=?", (user["id"], now.date().isoformat())).fetchone()[0]
        if claimed_today >= AD_DAILY_LIMIT:
            raise HTTPException(status_code=429, detail="今日广告奖励次数已用完")
        balance = change_credits(db, user["id"], AD_REWARD_CREDITS, "rewarded_ad", "ad_claim", str(claim["id"]), f"ad:{claim['id']}")
        db.execute("UPDATE reward_claims SET status='claimed', claimed_at=? WHERE id=?", (now.isoformat(), claim["id"]))
        db.commit()
    return {"credits": balance, "rewarded": AD_REWARD_CREDITS}


async def process_job(job_id: int, user_id: int, media_path: Path, filename: str, content_type: str, mode: str, cost: int) -> None:
    try:
        with media_path.open("rb") as source:
            upload = UploadFile(file=source, filename=filename, headers=Headers({"content-type": content_type}))
            analysis, prompts = await analyze_media_upload(upload, mode, check_content_security=True)
        result = json.dumps({"analysis": analysis.model_dump(), "prompts": prompts.model_dump()}, ensure_ascii=False)
        with connect() as db:
            db.execute("UPDATE jobs SET status='succeeded', result_json=?, updated_at=? WHERE id=?", (result, utc_now().isoformat(), job_id))
    except Exception as exc:
        logger.exception("Job failed: job_id=%s user_id=%s mode=%s", job_id, user_id, mode)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            change_credits(db, user_id, cost, "job_refund", "job", str(job_id), f"job:refund:{job_id}")
            db.execute("UPDATE jobs SET status='failed', error_message=?, updated_at=? WHERE id=?", (str(getattr(exc, "detail", "分析失败，积分已退回"))[:500], utc_now().isoformat(), job_id))
            db.commit()
    finally:
        media_path.unlink(missing_ok=True)


async def persist_job_media(file: UploadFile, mode: str, idempotency_key: str) -> tuple[Path, str]:
    allowed = IMAGE_TYPES if mode == "image" else VIDEO_TYPES
    limit = MAX_IMAGE_BYTES if mode == "image" else MAX_VIDEO_BYTES
    if file.content_type not in allowed:
        raise HTTPException(status_code=415, detail="文件类型与当前模式不匹配")
    directory = Path(DATA_DIR) / "job-media"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "media").suffix[:10]
    path = directory / f"{hashlib.sha256(idempotency_key.encode()).hexdigest()}-{secrets.token_hex(8)}{suffix}"
    total = 0
    try:
        with path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail=f"文件不能超过 {limit // (1024 * 1024)}MB")
                target.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        return path, file.content_type
    except Exception:
        path.unlink(missing_ok=True)
        raise


@commercial_router.post("/jobs", status_code=202)
async def create_job(background_tasks: BackgroundTasks, file: UploadFile = File(...), mode: str = Form("image"), idempotency_key: str = Form(...), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if len(idempotency_key) < 12 or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="幂等键格式错误")
    if mode not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="不支持的媒体类型")
    cost = IMAGE_CREDIT_COST if mode == "image" else VIDEO_CREDIT_COST
    media_path, content_type = await persist_job_media(file, mode, idempotency_key)
    now = utc_now().isoformat()
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE user_id=? AND idempotency_key=?", (user["id"], idempotency_key)).fetchone()
            if existing:
                db.rollback()
                media_path.unlink(missing_ok=True)
                return job_payload(existing)
            cursor = db.execute("INSERT INTO jobs(user_id,mode,filename,cost,status,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (user["id"], mode, file.filename or "untitled", cost, "processing", idempotency_key, now, now))
            job_id = int(cursor.lastrowid)
            change_credits(db, user["id"], -cost, "analysis_job", "job", str(job_id), f"job:charge:{job_id}")
            db.commit()
    except Exception:
        media_path.unlink(missing_ok=True)
        raise
    background_tasks.add_task(process_job, job_id, user["id"], media_path, file.filename or "untitled", content_type, mode, cost)
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_payload(row)


def job_payload(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {"id": row["id"], "mode": row["mode"], "filename": row["filename"], "cost": row["cost"], "status": row["status"], "result": result, "error_message": row["error_message"], "created_at": row["created_at"]}


@commercial_router.get("/jobs")
async def list_jobs(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    return [job_payload(row) for row in rows]


@commercial_router.get("/jobs/{job_id}")
async def get_job(job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_payload(row)


@commercial_router.get("/credits/ledger")
async def credit_ledger(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT id,amount,balance_after,reason,reference_type,reference_id,created_at FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()
    return [dict(row) for row in rows]
