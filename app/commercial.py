from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.datastructures import Headers
from pydantic import BaseModel, Field

from app.video_sources.downloader import download_video, validate_remote_target
from app.video_sources.errors import InvalidUploadError
from app.video_sources.platform_downloader import detect_platform, download_platform_video, extract_url

logger = logging.getLogger("prompt-lens.jobs")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_MB", "12")) * 1024 * 1024
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_MB", "180")) * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}

WX_APP_ID = os.getenv("WX_APP_ID", "").strip()
WX_APP_SECRET = os.getenv("WX_APP_SECRET", "").strip()
ENABLE_DEV_LOGIN = os.getenv("ENABLE_DEV_LOGIN", "false").lower() == "true"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
WELCOME_CREDITS = int(os.getenv("WELCOME_CREDITS", "3"))
IMAGE_CREDIT_COST = int(os.getenv("IMAGE_CREDIT_COST", "1"))
VIDEO_CREDIT_COST = int(os.getenv("VIDEO_CREDIT_COST", "1"))
AD_REWARD_CREDITS = int(os.getenv("AD_REWARD_CREDITS", "1"))
AD_DAILY_LIMIT = int(os.getenv("AD_DAILY_LIMIT", "20"))
AD_COOLDOWN_SECONDS = int(os.getenv("AD_COOLDOWN_SECONDS", "60"))
DEPTH_COMPUTE_COST = int(os.getenv("DEPTH_COMPUTE_COST", "1"))
DEPTH_SERVICE_BASE_URL = os.getenv("DEPTH_SERVICE_BASE_URL", "https://depth.whaios.com").rstrip("/")
DEPTH_RESULT_RETENTION_HOURS = int(os.getenv("DEPTH_RESULT_RETENTION_HOURS", "24"))
DB_PATH = Path(DATA_DIR) / "commercial.sqlite3"
DEPTH_RESULTS_DIR = Path(DATA_DIR) / "depth-results"
ANALYSIS_DEPTHS = {"standard", "detailed", "professional"}
ANALYSIS_TASKS = {"reconstruct", "image_expand_video"}
DEPTH_PRESETS = {"quick_preview", "standard_depth", "motion_character"}
DEPTH_PRESET_OPTIONS: dict[str, dict[str, int | float]] = {
    "quick_preview": {"max_output_side": 768, "max_output_fps": 12, "temporal_smoothing": 0.20, "stabilize_range": 0.78},
    "standard_depth": {"max_output_side": 1280, "max_output_fps": 24, "temporal_smoothing": 0.25, "stabilize_range": 0.85},
    "motion_character": {"max_output_side": 1024, "max_output_fps": 30, "temporal_smoothing": 0.32, "stabilize_range": 0.90},
}

commercial_router = APIRouter(prefix="/api/v1", tags=["mini-program"])


class LoginRequest(BaseModel):
    code: str


class RewardCompleteRequest(BaseModel):
    claim_token: str


class RemoteAnalysisRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    analysis_depth: str = "detailed"
    idempotency_key: str = Field(min_length=12, max_length=128)


class DepthRemoteRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    preset: str = "standard_depth"
    idempotency_key: str = Field(min_length=12, max_length=128)


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
            analysis_task TEXT NOT NULL DEFAULT 'reconstruct',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS depth_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            external_id TEXT UNIQUE,
            filename TEXT NOT NULL,
            source_type TEXT NOT NULL,
            preset TEXT NOT NULL,
            cost INTEGER NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            result_json TEXT,
            error_message TEXT,
            artifact_path TEXT,
            artifact_content_type TEXT,
            artifact_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON credit_ledger(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_depth_jobs_user_created ON depth_jobs(user_id, id DESC);
    """)
    try:
        db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for statement in (
        "ALTER TABLE jobs ADD COLUMN analysis_depth TEXT NOT NULL DEFAULT 'detailed'",
        "ALTER TABLE jobs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'upload'",
        "ALTER TABLE jobs ADD COLUMN source_url TEXT",
        "ALTER TABLE jobs ADD COLUMN source_platform TEXT",
        "ALTER TABLE jobs ADD COLUMN analysis_task TEXT NOT NULL DEFAULT 'reconstruct'",
        "ALTER TABLE depth_jobs ADD COLUMN artifact_path TEXT",
        "ALTER TABLE depth_jobs ADD COLUMN artifact_content_type TEXT",
        "ALTER TABLE depth_jobs ADD COLUMN artifact_expires_at TEXT",
    ):
        try:
            db.execute(statement)
        except sqlite3.OperationalError:
            pass
    return db


def recover_interrupted_jobs() -> None:
    """Refund jobs left in processing after an unclean service restart."""
    with connect() as db:
        rows = db.execute("SELECT id,user_id,cost FROM jobs WHERE status='processing'").fetchall()
        depth_rows = db.execute("SELECT id,user_id,cost FROM depth_jobs WHERE status='submitting'").fetchall()
        if not rows and not depth_rows:
            return
        db.execute("BEGIN IMMEDIATE")
        for row in rows:
            change_credits(db, row["user_id"], row["cost"], "job_refund", "job", str(row["id"]), f"job:refund:{row['id']}")
            db.execute("UPDATE jobs SET status='failed', error_message='服务重启导致任务中断，算力次数已退回', updated_at=? WHERE id=?", (utc_now().isoformat(), row["id"]))
        for row in depth_rows:
            change_credits(db, row["user_id"], row["cost"], "tool_refund", "depth_job", str(row["id"]), f"depth:refund:{row['id']}")
            db.execute("UPDATE depth_jobs SET status='failed',error_message='服务重启导致任务中断，算力次数已退回',updated_at=? WHERE id=?", (utc_now().isoformat(), row["id"]))
        db.commit()
    for directory_name in ("job-media", "depth-media"):
        media_dir = Path(DATA_DIR) / directory_name
        if media_dir.exists():
            for path in media_dir.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)


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
        raise HTTPException(status_code=402, detail="算力次数不足，请观看激励广告获取次数")
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
    return {"token": token, "expires_in": SESSION_DAYS * 86400, "user": {"id": user_id, "credits": credits, "compute_count": credits}, "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST, "depth": DEPTH_COMPUTE_COST}, "ad": {"reward": AD_REWARD_CREDITS, "daily_limit": AD_DAILY_LIMIT}}


@commercial_router.get("/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    today = utc_now().date().isoformat()
    with connect() as db:
        used = db.execute("SELECT COUNT(*) FROM reward_claims WHERE user_id=? AND status='claimed' AND substr(claimed_at,1,10)=?", (user["id"], today)).fetchone()[0]
    return {"id": user["id"], "credits": user["credits"], "compute_count": user["credits"], "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST, "depth": DEPTH_COMPUTE_COST}, "ad": {"reward": AD_REWARD_CREDITS, "daily_limit": AD_DAILY_LIMIT, "remaining_today": max(0, AD_DAILY_LIMIT - used)}}


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
    return {"credits": balance, "compute_count": balance, "rewarded": AD_REWARD_CREDITS}


def fail_analysis_job(job_id: int, user_id: int, cost: int, message: str) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        change_credits(db, user_id, cost, "job_refund", "job", str(job_id), f"job:refund:{job_id}")
        db.execute("UPDATE jobs SET status='failed', error_message=?, updated_at=? WHERE id=?", (message[:500], utc_now().isoformat(), job_id))
        db.commit()


async def process_job(job_id: int, user_id: int, media_path: Path, filename: str, content_type: str, mode: str, cost: int, analysis_depth: str, analysis_task: str) -> None:
    try:
        # 延迟导入，避免 app.main 注册 commercial_router 时形成循环依赖。
        from app.main import analyze_media_upload

        with media_path.open("rb") as source:
            upload = UploadFile(file=source, filename=filename, headers=Headers({"content-type": content_type}))
            analysis, prompts = await analyze_media_upload(upload, mode, check_content_security=True, analysis_depth=analysis_depth, analysis_task=analysis_task)
        result = json.dumps({"analysis": analysis.model_dump(), "prompts": prompts.model_dump(), "analysis_depth": analysis_depth, "analysis_task": analysis_task}, ensure_ascii=False)
        with connect() as db:
            db.execute("UPDATE jobs SET status='succeeded', result_json=?, updated_at=? WHERE id=?", (result, utc_now().isoformat(), job_id))
    except Exception as exc:
        logger.exception("Job failed: job_id=%s user_id=%s mode=%s", job_id, user_id, mode)
        fail_analysis_job(job_id, user_id, cost, str(getattr(exc, "detail", "分析失败，算力次数已退回")))
    finally:
        media_path.unlink(missing_ok=True)


def remote_video_content_type(path: Path) -> str:
    return {
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
    }.get(path.suffix.lower(), "video/mp4")


async def process_remote_analysis_job(job_id: int, user_id: int, raw_url: str, cost: int, analysis_depth: str) -> None:
    directory = Path(DATA_DIR) / "job-media" / f"remote-{job_id}-{secrets.token_hex(6)}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        url = extract_url(raw_url)
        platform = detect_platform(url)
        if platform:
            downloaded = await asyncio.to_thread(download_platform_video, url, directory, MAX_VIDEO_BYTES)
            filename, media_path, source_platform = downloaded.filename, downloaded.source_path, downloaded.platform
        else:
            validate_remote_target(url)
            downloaded = await asyncio.to_thread(download_video, url, directory, MAX_VIDEO_BYTES)
            filename, media_path, source_platform = downloaded.filename, directory / f"source{downloaded.suffix}", "direct"
        with connect() as db:
            db.execute("UPDATE jobs SET filename=?,source_platform=?,updated_at=? WHERE id=?", (filename, source_platform, utc_now().isoformat(), job_id))
        await process_job(job_id, user_id, media_path, filename, remote_video_content_type(media_path), "video", cost, analysis_depth, "reconstruct")
    except InvalidUploadError as exc:
        logger.warning("Remote analysis download failed: job_id=%s detail=%s", job_id, exc)
        fail_analysis_job(job_id, user_id, cost, str(exc))
    except Exception as exc:
        logger.exception("Remote analysis job failed: job_id=%s", job_id)
        fail_analysis_job(job_id, user_id, cost, str(getattr(exc, "detail", "视频链接解析失败，算力次数已退回")))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def persist_job_media(file: UploadFile, mode: str, idempotency_key: str, directory_name: str = "job-media") -> tuple[Path, str]:
    allowed = IMAGE_TYPES if mode == "image" else VIDEO_TYPES
    limit = MAX_IMAGE_BYTES if mode == "image" else MAX_VIDEO_BYTES
    if file.content_type not in allowed:
        raise HTTPException(status_code=415, detail="文件类型与当前模式不匹配")
    directory = Path(DATA_DIR) / directory_name
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
async def create_job(background_tasks: BackgroundTasks, file: UploadFile = File(...), mode: str = Form("image"), analysis_depth: str = Form("detailed"), analysis_task: str = Form("reconstruct"), idempotency_key: str = Form(...), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if len(idempotency_key) < 12 or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="幂等键格式错误")
    if mode not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="不支持的媒体类型")
    if analysis_depth not in ANALYSIS_DEPTHS:
        raise HTTPException(status_code=400, detail="不支持的反推维度")
    if analysis_task not in ANALYSIS_TASKS:
        raise HTTPException(status_code=400, detail="不支持的图片任务类型")
    if analysis_task == "image_expand_video" and mode != "image":
        raise HTTPException(status_code=400, detail="画面拓展仅支持图片")
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
            cursor = db.execute("INSERT INTO jobs(user_id,mode,filename,cost,status,idempotency_key,analysis_depth,analysis_task,source_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (user["id"], mode, file.filename or "untitled", cost, "processing", idempotency_key, analysis_depth, analysis_task, "upload", now, now))
            job_id = int(cursor.lastrowid)
            change_credits(db, user["id"], -cost, "analysis_job", "job", str(job_id), f"job:charge:{job_id}")
            db.commit()
    except Exception:
        media_path.unlink(missing_ok=True)
        raise
    background_tasks.add_task(process_job, job_id, user["id"], media_path, file.filename or "untitled", content_type, mode, cost, analysis_depth, analysis_task)
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_payload(row)


@commercial_router.post("/jobs/remote", status_code=202)
async def create_remote_analysis_job(body: RemoteAnalysisRequest, background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if body.analysis_depth not in ANALYSIS_DEPTHS:
        raise HTTPException(status_code=400, detail="不支持的反推维度")
    try:
        url = extract_url(body.url)
        platform = detect_platform(url)
        if platform is None:
            validate_remote_target(url)
    except InvalidUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = utc_now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM jobs WHERE user_id=? AND idempotency_key=?", (user["id"], body.idempotency_key)).fetchone()
        if existing:
            db.rollback()
            return job_payload(existing)
        cursor = db.execute(
            "INSERT INTO jobs(user_id,mode,filename,cost,status,idempotency_key,analysis_depth,source_type,source_url,source_platform,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], "video", "链接视频", VIDEO_CREDIT_COST, "processing", body.idempotency_key, body.analysis_depth, "remote", url, platform or "direct", now, now),
        )
        job_id = int(cursor.lastrowid)
        change_credits(db, user["id"], -VIDEO_CREDIT_COST, "analysis_job", "job", str(job_id), f"job:charge:{job_id}")
        db.commit()
    background_tasks.add_task(process_remote_analysis_job, job_id, user["id"], url, VIDEO_CREDIT_COST, body.analysis_depth)
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_payload(row)


def fail_depth_job(local_job_id: int, user_id: int, cost: int, message: str) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM depth_jobs WHERE id=? AND user_id=?", (local_job_id, user_id)).fetchone()
        if not row or row["status"] in {"failed", "completed"}:
            db.rollback()
            return
        change_credits(db, user_id, cost, "tool_refund", "depth_job", str(local_job_id), f"depth:refund:{local_job_id}")
        db.execute("UPDATE depth_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (message[:500], utc_now().isoformat(), local_job_id))
        db.commit()


async def submit_depth_upload_job(local_job_id: int, user_id: int, media_path: Path, filename: str, content_type: str, preset: str, cost: int) -> None:
    try:
        processing_options = {"preset": preset, **DEPTH_PRESET_OPTIONS[preset]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            with media_path.open("rb") as source:
                response = await client.post(
                    f"{DEPTH_SERVICE_BASE_URL}/api/jobs",
                    files={"file": (filename, source, content_type)},
                    data=processing_options,
                )
        payload = response.json()
        if response.is_error or not payload.get("id"):
            raise RuntimeError(payload.get("message") or payload.get("detail") or "深度服务拒绝任务")
        with connect() as db:
            db.execute("UPDATE depth_jobs SET external_id=?,status=?,result_json=?,updated_at=? WHERE id=?", (payload["id"], payload.get("status", "queued"), json.dumps(payload, ensure_ascii=False), utc_now().isoformat(), local_job_id))
    except Exception as exc:
        logger.exception("Depth upload submission failed: local_job_id=%s", local_job_id)
        fail_depth_job(local_job_id, user_id, cost, str(getattr(exc, "detail", exc)) or "深度服务暂时不可用")
    finally:
        media_path.unlink(missing_ok=True)


async def submit_depth_remote_job(local_job_id: int, user_id: int, url: str, preset: str, cost: int) -> None:
    try:
        processing_options = {"url": url, "preset": preset, **DEPTH_PRESET_OPTIONS[preset]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(f"{DEPTH_SERVICE_BASE_URL}/api/jobs/remote", json=processing_options)
        payload = response.json()
        if response.is_error or not payload.get("id"):
            raise RuntimeError(payload.get("message") or payload.get("detail") or "深度服务拒绝任务")
        with connect() as db:
            db.execute("UPDATE depth_jobs SET external_id=?,status=?,result_json=?,updated_at=? WHERE id=?", (payload["id"], payload.get("status", "queued"), json.dumps(payload, ensure_ascii=False), utc_now().isoformat(), local_job_id))
    except Exception as exc:
        logger.exception("Depth remote submission failed: local_job_id=%s", local_job_id)
        fail_depth_job(local_job_id, user_id, cost, str(getattr(exc, "detail", exc)) or "深度服务暂时不可用")


def parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def depth_result_expiration(row: sqlite3.Row) -> datetime | None:
    keys = set(row.keys())
    explicit = parse_utc_datetime(row["artifact_expires_at"]) if "artifact_expires_at" in keys else None
    if explicit:
        return explicit
    if row["status"] == "completed":
        completed_at = parse_utc_datetime(row["updated_at"])
        if completed_at:
            return completed_at + timedelta(hours=DEPTH_RESULT_RETENTION_HOURS)
    return None


def depth_result_path(row: sqlite3.Row) -> Path | None:
    keys = set(row.keys())
    raw_path = row["artifact_path"] if "artifact_path" in keys else None
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    try:
        path.relative_to(DEPTH_RESULTS_DIR.resolve())
    except ValueError:
        logger.error("Rejected depth artifact outside result directory: %s", path)
        return None
    return path


def depth_result_available(row: sqlite3.Row) -> bool:
    path = depth_result_path(row)
    expiration = depth_result_expiration(row)
    return bool(path and path.is_file() and expiration and expiration > utc_now())


def cleanup_expired_depth_results() -> None:
    now = utc_now()
    with connect() as db:
        rows = db.execute("SELECT * FROM depth_jobs WHERE artifact_path IS NOT NULL").fetchall()
        for row in rows:
            expiration = depth_result_expiration(row)
            if not expiration or expiration > now:
                continue
            path = depth_result_path(row)
            if path:
                path.unlink(missing_ok=True)
            db.execute("UPDATE depth_jobs SET artifact_path=NULL,artifact_content_type=NULL WHERE id=?", (row["id"],))


async def cache_depth_result(local_job_id: int, external_id: str) -> None:
    DEPTH_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = DEPTH_RESULTS_DIR / f".{local_job_id}-{secrets.token_hex(6)}.part"
    content_type = "video/mp4"
    suffix = ".mp4"
    try:
        timeout = httpx.Timeout(None, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", f"{DEPTH_SERVICE_BASE_URL}/api/jobs/{external_id}/download") as response:
                if response.is_error:
                    raise RuntimeError(f"深度结果下载失败: HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "video/mp4").split(";", 1)[0].strip()
                suffix = {"video/webm": ".webm", "video/quicktime": ".mov"}.get(content_type, ".mp4")
                with temporary_path.open("wb") as target:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        target.write(chunk)
        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            raise RuntimeError("深度服务返回了空视频")
        final_path = DEPTH_RESULTS_DIR / f"depth-{local_job_id}{suffix}"
        temporary_path.replace(final_path)
        now = utc_now()
        expiration = now + timedelta(hours=DEPTH_RESULT_RETENTION_HOURS)
        with connect() as db:
            cursor = db.execute(
                "UPDATE depth_jobs SET status='completed',artifact_path=?,artifact_content_type=?,artifact_expires_at=?,updated_at=? WHERE id=?",
                (str(final_path), content_type, expiration.isoformat(), now.isoformat(), local_job_id),
            )
            if cursor.rowcount != 1:
                final_path.unlink(missing_ok=True)
                raise RuntimeError("深度任务不存在")
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def depth_job_payload(row: sqlite3.Row, external: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = json.loads(row["result_json"]) if row["result_json"] else {}
    source = external or stored
    status = row["status"]
    expiration = depth_result_expiration(row)
    available = depth_result_available(row)
    if status == "completed" and expiration and expiration <= utc_now():
        status = "expired"
    payload = {
        "id": row["id"], "filename": source.get("filename", row["filename"]), "source_type": row["source_type"],
        "preset": row["preset"], "cost": row["cost"], "status": status, "progress": source.get("progress", 0),
        "message": source.get("message") or ("正在提交任务" if status == "submitting" else ""),
        "error": row["error_message"] or source.get("error"), "metadata": source.get("metadata"),
        "source_platform": source.get("source_platform"), "created_at": row["created_at"],
        "available_until": expiration.isoformat() if expiration else None,
        "preview_url": None, "download_url": None,
    }
    if status == "completed" and available:
        payload["preview_url"] = f"/api/v1/depth/jobs/{row['id']}/preview"
        payload["download_url"] = f"/api/v1/depth/jobs/{row['id']}/download"
    return payload


@commercial_router.post("/depth/jobs", status_code=202)
async def create_depth_upload_job(background_tasks: BackgroundTasks, file: UploadFile = File(...), preset: str = Form("standard_depth"), idempotency_key: str = Form(...), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if preset not in DEPTH_PRESETS:
        raise HTTPException(status_code=400, detail="不支持的深度转换模式")
    if len(idempotency_key) < 12 or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="幂等键格式错误")
    media_path, content_type = await persist_job_media(file, "video", f"depth-{idempotency_key}", directory_name="depth-media")
    now = utc_now().isoformat()
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM depth_jobs WHERE user_id=? AND idempotency_key=?", (user["id"], idempotency_key)).fetchone()
            if existing:
                db.rollback()
                media_path.unlink(missing_ok=True)
                return depth_job_payload(existing)
            cursor = db.execute("INSERT INTO depth_jobs(user_id,filename,source_type,preset,cost,status,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], file.filename or "video.mp4", "upload", preset, DEPTH_COMPUTE_COST, "submitting", idempotency_key, now, now))
            local_job_id = int(cursor.lastrowid)
            change_credits(db, user["id"], -DEPTH_COMPUTE_COST, "depth_job", "depth_job", str(local_job_id), f"depth:charge:{local_job_id}")
            db.commit()
    except Exception:
        media_path.unlink(missing_ok=True)
        raise
    background_tasks.add_task(submit_depth_upload_job, local_job_id, user["id"], media_path, file.filename or "video.mp4", content_type, preset, DEPTH_COMPUTE_COST)
    with connect() as db:
        return depth_job_payload(db.execute("SELECT * FROM depth_jobs WHERE id=?", (local_job_id,)).fetchone())


@commercial_router.post("/depth/jobs/remote", status_code=202)
async def create_depth_remote_job(body: DepthRemoteRequest, background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if body.preset not in DEPTH_PRESETS:
        raise HTTPException(status_code=400, detail="不支持的深度转换模式")
    try:
        url = extract_url(body.url)
    except InvalidUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = utc_now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM depth_jobs WHERE user_id=? AND idempotency_key=?", (user["id"], body.idempotency_key)).fetchone()
        if existing:
            db.rollback()
            return depth_job_payload(existing)
        cursor = db.execute("INSERT INTO depth_jobs(user_id,filename,source_type,preset,cost,status,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], "链接视频", "remote", body.preset, DEPTH_COMPUTE_COST, "submitting", body.idempotency_key, now, now))
        local_job_id = int(cursor.lastrowid)
        change_credits(db, user["id"], -DEPTH_COMPUTE_COST, "depth_job", "depth_job", str(local_job_id), f"depth:charge:{local_job_id}")
        db.commit()
    background_tasks.add_task(submit_depth_remote_job, local_job_id, user["id"], url, body.preset, DEPTH_COMPUTE_COST)
    with connect() as db:
        return depth_job_payload(db.execute("SELECT * FROM depth_jobs WHERE id=?", (local_job_id,)).fetchone())


@commercial_router.get("/depth/jobs")
async def list_depth_jobs(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    cleanup_expired_depth_results()
    with connect() as db:
        rows = db.execute("SELECT * FROM depth_jobs WHERE user_id=? ORDER BY id DESC LIMIT 30", (user["id"],)).fetchall()
    return [depth_job_payload(row) for row in rows]


@commercial_router.get("/depth/jobs/{local_job_id}")
async def get_depth_job(local_job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM depth_jobs WHERE id=? AND user_id=?", (local_job_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="深度任务不存在")
    if row["status"] == "failed" or not row["external_id"]:
        return depth_job_payload(row)
    if row["status"] == "completed":
        current = depth_job_payload(row)
        if current["status"] == "expired" or current["preview_url"]:
            return current
        with connect() as db:
            db.execute("UPDATE depth_jobs SET status='finalizing' WHERE id=?", (local_job_id,))
        try:
            await cache_depth_result(local_job_id, row["external_id"])
        except Exception as exc:
            logger.warning("Depth result caching failed: local_job_id=%s detail=%s", local_job_id, exc)
            raise HTTPException(status_code=503, detail="转换已完成，结果视频正在保存，请稍后刷新") from exc
        with connect() as db:
            return depth_job_payload(db.execute("SELECT * FROM depth_jobs WHERE id=?", (local_job_id,)).fetchone())
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            response = await client.get(f"{DEPTH_SERVICE_BASE_URL}/api/jobs/{row['external_id']}")
        payload = response.json()
        if response.is_error:
            raise RuntimeError(payload.get("message") or payload.get("detail") or "深度任务状态读取失败")
    except Exception as exc:
        logger.warning("Depth status request failed: local_job_id=%s detail=%s", local_job_id, exc)
        raise HTTPException(status_code=503, detail="深度服务暂时不可用，请稍后刷新") from exc
    status = payload.get("status", "queued")
    if status == "failed":
        fail_depth_job(local_job_id, user["id"], row["cost"], payload.get("error") or "深度转换失败，算力次数已退回")
    elif status == "completed":
        with connect() as db:
            db.execute("UPDATE depth_jobs SET status='finalizing',result_json=?,error_message=NULL,updated_at=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), utc_now().isoformat(), local_job_id))
        try:
            await cache_depth_result(local_job_id, row["external_id"])
        except Exception as exc:
            logger.warning("Depth result caching failed: local_job_id=%s detail=%s", local_job_id, exc)
            raise HTTPException(status_code=503, detail="转换已完成，结果视频正在保存，请稍后刷新") from exc
    else:
        with connect() as db:
            db.execute("UPDATE depth_jobs SET status=?,result_json=?,error_message=?,updated_at=? WHERE id=?", (status, json.dumps(payload, ensure_ascii=False), payload.get("error"), utc_now().isoformat(), local_job_id))
    with connect() as db:
        fresh = db.execute("SELECT * FROM depth_jobs WHERE id=?", (local_job_id,)).fetchone()
    return depth_job_payload(fresh, payload)


@commercial_router.get("/depth/jobs/{local_job_id}/{artifact}")
async def depth_job_artifact(local_job_id: int, artifact: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    if artifact not in {"preview", "download"}:
        raise HTTPException(status_code=404, detail="文件不存在")
    await get_depth_job(local_job_id, user)
    with connect() as db:
        row = db.execute("SELECT * FROM depth_jobs WHERE id=? AND user_id=?", (local_job_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="深度任务不存在")
    expiration = depth_result_expiration(row)
    if expiration and expiration <= utc_now():
        path = depth_result_path(row)
        if path:
            path.unlink(missing_ok=True)
        raise HTTPException(status_code=410, detail="深度视频已超过 24 小时保留期")
    path = depth_result_path(row)
    if row["status"] != "completed" or not path or not path.is_file():
        raise HTTPException(status_code=404, detail="深度视频尚未生成")
    disposition = "attachment" if artifact == "download" else "inline"
    headers = {
        "Content-Disposition": f'{disposition}; filename="depth-{local_job_id}{path.suffix}"',
        "Cache-Control": "private, max-age=300",
    }
    return FileResponse(path, media_type=row["artifact_content_type"] or "video/mp4", headers=headers)


def job_payload(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    keys = set(row.keys())
    return {"id": row["id"], "mode": row["mode"], "filename": row["filename"], "cost": row["cost"], "status": row["status"], "result": result, "error_message": row["error_message"], "analysis_depth": row["analysis_depth"] if "analysis_depth" in keys else "detailed", "analysis_task": row["analysis_task"] if "analysis_task" in keys else "reconstruct", "source_type": row["source_type"] if "source_type" in keys else "upload", "source_platform": row["source_platform"] if "source_platform" in keys else None, "created_at": row["created_at"]}


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
