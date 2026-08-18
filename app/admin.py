from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.commercial import AD_DAILY_LIMIT, IMAGE_CREDIT_COST, VIDEO_CREDIT_COST, connect, hash_token, change_credits, utc_now

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_SESSION_DAYS = int(os.getenv("ADMIN_SESSION_DAYS", "7"))

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminLogin(BaseModel):
    username: str
    password: str


class CreditAdjustment(BaseModel):
    amount: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=2, max_length=120)


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(derived.hex(), digest)
    except (ValueError, TypeError):
        return False


def admin_user(x_admin_token: str | None = Header(default=None)) -> dict[str, str]:
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="需要管理员登录")
    with connect() as db:
        row = db.execute("SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at>?", (hash_token(x_admin_token), utc_now().isoformat())).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="管理员登录已过期")
    return {"role": "admin"}


def row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


@admin_router.post("/login")
async def login(body: AdminLogin) -> dict[str, Any]:
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="尚未配置 ADMIN_PASSWORD_HASH")
    if not hmac.compare_digest(body.username, ADMIN_USERNAME) or not verify_password(body.password, ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    token = secrets.token_urlsafe(40)
    now = utc_now()
    with connect() as db:
        db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now.isoformat(),))
        db.execute("INSERT INTO admin_sessions(token_hash,expires_at,created_at) VALUES(?,?,?)", (hash_token(token), (now + timedelta(days=ADMIN_SESSION_DAYS)).isoformat(), now.isoformat()))
    return {"token": token, "expires_in": ADMIN_SESSION_DAYS * 86400}


@admin_router.post("/logout")
async def logout(x_admin_token: str | None = Header(default=None), _: dict[str, str] = Depends(admin_user)) -> dict[str, bool]:
    if x_admin_token:
        with connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hash_token(x_admin_token),))
    return {"ok": True}


@admin_router.get("/overview")
async def overview(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_users = db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=0").fetchone()[0]
        jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        succeeded = db.execute("SELECT COUNT(*) FROM jobs WHERE status='succeeded'").fetchone()[0]
        processing = db.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'").fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
        credits = db.execute("SELECT COALESCE(SUM(credits),0) FROM users").fetchone()[0]
        consumed = db.execute("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),0) FROM credit_ledger").fetchone()[0]
        ad_claims = db.execute("SELECT COUNT(*) FROM reward_claims WHERE status='claimed'").fetchone()[0]
    return {"users": users, "active_users": active_users, "jobs": jobs, "succeeded": succeeded, "processing": processing, "failed": failed, "credits": credits, "consumed_credits": consumed, "ad_claims": ad_claims, "ad_daily_limit": AD_DAILY_LIMIT}


@admin_router.get("/config")
async def config_status(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    """Expose deployment readiness without returning any secret values."""
    openai_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
    wechat_app_configured = bool(os.getenv("WX_APP_ID", "").strip())
    wechat_secret_configured = bool(os.getenv("WX_APP_SECRET", "").strip())
    ad_configured = bool(os.getenv("WX_AD_UNIT_ID", "").strip())
    dev_login = os.getenv("ENABLE_DEV_LOGIN", "false").lower() == "true"
    return {
        "environment": "development" if dev_login else "production",
        "openai_configured": openai_configured,
        "wechat_configured": wechat_app_configured and wechat_secret_configured,
        "ad_configured": ad_configured,
        "admin_configured": bool(ADMIN_PASSWORD_HASH),
        "https_required": not dev_login,
        "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST},
    }


@admin_router.get("/users")
async def users(query: str = "", limit: int = 50, _: dict[str, str] = Depends(admin_user)) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    pattern = "%" + query.strip() + "%"
    with connect() as db:
        rows = db.execute("SELECT id,openid,unionid,credits,is_blocked,created_at,updated_at FROM users WHERE openid LIKE ? OR CAST(id AS TEXT) LIKE ? ORDER BY id DESC LIMIT ?", (pattern, pattern, limit)).fetchall()
    return [row_dict(row) for row in rows]


@admin_router.get("/users/{user_id}")
async def user_detail(user_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        user = db.execute("SELECT id,openid,unionid,credits,is_blocked,created_at,updated_at FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        ledger = db.execute("SELECT id,amount,balance_after,reason,reference_type,reference_id,created_at FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        jobs = db.execute("SELECT id,mode,filename,cost,status,error_message,created_at,updated_at FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
    return {"user": row_dict(user), "ledger": [row_dict(row) for row in ledger], "jobs": [row_dict(row) for row in jobs]}


@admin_router.post("/users/{user_id}/credits")
async def adjust_credits(user_id: int, body: CreditAdjustment, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        balance = change_credits(db, user_id, body.amount, "admin_adjustment:" + body.reason, "admin", str(user_id), "admin:" + secrets.token_hex(16))
        db.commit()
    return {"user_id": user_id, "credits": balance}


@admin_router.post("/users/{user_id}/block")
async def block_user(user_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("UPDATE users SET is_blocked=1,updated_at=? WHERE id=?", (utc_now().isoformat(), user_id))
    return {"user_id": user_id, "is_blocked": True}


@admin_router.post("/users/{user_id}/unblock")
async def unblock_user(user_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("UPDATE users SET is_blocked=0,updated_at=? WHERE id=?", (utc_now().isoformat(), user_id))
    return {"user_id": user_id, "is_blocked": False}


@admin_router.get("/jobs")
async def jobs(status: str = "", limit: int = 100, _: dict[str, str] = Depends(admin_user)) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    with connect() as db:
        if status in {"processing", "succeeded", "failed"}:
            rows = db.execute("SELECT jobs.*,users.openid FROM jobs JOIN users ON users.id=jobs.user_id WHERE jobs.status=? ORDER BY jobs.id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = db.execute("SELECT jobs.*,users.openid FROM jobs JOIN users ON users.id=jobs.user_id ORDER BY jobs.id DESC LIMIT ?", (limit,)).fetchall()
    return [row_dict(row) for row in rows]


@admin_router.post("/jobs/{job_id}/refund")
async def refund_job(job_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] == "processing":
            raise HTTPException(status_code=409, detail="任务仍在处理中，请稍后再退款")
        balance = change_credits(db, job["user_id"], job["cost"], "admin_refund", "job", str(job_id), f"admin:refund:{job_id}")
        db.execute("UPDATE jobs SET status='failed',error_message='管理员手动退款',updated_at=? WHERE id=?", (utc_now().isoformat(), job_id))
        db.commit()
    return {"job_id": job_id, "credits": balance}
