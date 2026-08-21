from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.commercial import AD_DAILY_LIMIT, IMAGE_CREDIT_COST, VIDEO_CREDIT_COST, connect, hash_token, change_credits, utc_now

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_SESSION_DAYS = int(os.getenv("ADMIN_SESSION_DAYS", "7"))
try:
    REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Asia/Shanghai"))
except ZoneInfoNotFoundError:
    REPORT_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

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


def report_day_bounds(offset_days: int = 0) -> tuple[datetime, datetime]:
    local_date = utc_now().astimezone(REPORT_TIMEZONE).date() + timedelta(days=offset_days)
    start = datetime.combine(local_date, time.min, REPORT_TIMEZONE).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def classify_failure(message: str | None) -> str:
    text = (message or "未知错误").lower()
    categories = (
        (("vision", "openai", "api key", "apikey", "模型", "额度"), "Vision API"),
        (("匿名浏览器", "douyin", "抖音"), "抖音链接解析"),
        (("kuaishou", "快手"), "快手链接解析"),
        (("xiaohongshu", "小红书", "xhs"), "小红书链接解析"),
        (("关键帧", "ffmpeg", "无法读取视频", "视频处理"), "视频处理"),
        (("内容安全", "违规", "msgseccheck", "security"), "内容安全"),
        (("depth", "深度"), "深度服务"),
        (("timeout", "timed out", "超时"), "请求超时"),
        (("服务重启", "任务中断"), "服务重启中断"),
    )
    for keywords, category in categories:
        if any(keyword in text for keyword in keywords):
            return category
    return "其他错误"


TASKS_CTE = """
    WITH tasks AS (
        SELECT id,user_id,mode AS task_type,status,cost,created_at,updated_at,error_message FROM jobs
        UNION ALL
        SELECT id,user_id,'depth' AS task_type,
            CASE WHEN status='completed' THEN 'succeeded' WHEN status='failed' THEN 'failed' ELSE 'processing' END,
            cost,created_at,updated_at,error_message FROM depth_jobs
        UNION ALL
        SELECT id,user_id,'optimization' AS task_type,status,cost,created_at,updated_at,error_message FROM prompt_optimizations
    )
"""


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
        depth_jobs = db.execute("SELECT COUNT(*) FROM depth_jobs").fetchone()[0]
        succeeded = db.execute("SELECT COUNT(*) FROM jobs WHERE status='succeeded'").fetchone()[0]
        processing = db.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'").fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
        credits = db.execute("SELECT COALESCE(SUM(credits),0) FROM users").fetchone()[0]
        consumed = db.execute("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),0) FROM credit_ledger").fetchone()[0]
        ad_claims = db.execute("SELECT COUNT(*) FROM reward_claims WHERE status='claimed'").fetchone()[0]
    return {"users": users, "active_users": active_users, "jobs": jobs, "depth_jobs": depth_jobs, "succeeded": succeeded, "processing": processing, "failed": failed, "credits": credits, "compute_count": credits, "consumed_credits": consumed, "ad_claims": ad_claims, "ad_daily_limit": AD_DAILY_LIMIT}


@admin_router.get("/analytics")
async def analytics(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    today_start, tomorrow_start = report_day_bounds()
    yesterday_start, _ = report_day_bounds(-1)
    seven_day_start, _ = report_day_bounds(-6)
    today = (today_start.isoformat(), tomorrow_start.isoformat())
    yesterday = (yesterday_start.isoformat(), today_start.isoformat())
    seven_days = (seven_day_start.isoformat(), tomorrow_start.isoformat())

    with connect() as db:
        new_users_today = db.execute("SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", today).fetchone()[0]
        new_users_yesterday = db.execute("SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", yesterday).fetchone()[0]
        active_users_today = db.execute(
            TASKS_CTE + "SELECT COUNT(DISTINCT user_id) FROM tasks WHERE created_at>=? AND created_at<?", today
        ).fetchone()[0]
        today_summary = db.execute(
            TASKS_CTE + """
                SELECT COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END),0) AS succeeded,
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed,
                    COALESCE(SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END),0) AS processing,
                    COALESCE(ROUND(AVG(CASE WHEN status IN ('succeeded','failed')
                        THEN (julianday(updated_at)-julianday(created_at))*86400 END)),0) AS avg_duration_seconds
                FROM tasks WHERE created_at>=? AND created_at<?
            """,
            today,
        ).fetchone()
        task_types = db.execute(
            TASKS_CTE + """
                SELECT task_type,COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END),0) AS succeeded,
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed,
                    COALESCE(SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END),0) AS processing,
                    COALESCE(ROUND(AVG(CASE WHEN status IN ('succeeded','failed')
                        THEN (julianday(updated_at)-julianday(created_at))*86400 END)),0) AS avg_duration_seconds
                FROM tasks WHERE created_at>=? AND created_at<? GROUP BY task_type
            """,
            seven_days,
        ).fetchall()
        trend_rows = db.execute(
            TASKS_CTE + """
                SELECT date(datetime(created_at),'+8 hours') AS day,COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END),0) AS succeeded,
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM tasks WHERE created_at>=? AND created_at<? GROUP BY day ORDER BY day
            """,
            seven_days,
        ).fetchall()
        failed_rows = db.execute(
            TASKS_CTE + """
                SELECT task_type,error_message,created_at FROM tasks
                WHERE status='failed' AND created_at>=? AND created_at<? ORDER BY created_at DESC
            """,
            seven_days,
        ).fetchall()
        platform_rows = db.execute(
            """
                SELECT CASE
                    WHEN source_platform IS NOT NULL THEN source_platform
                    WHEN lower(COALESCE(source_url,'')) LIKE '%douyin%' OR source_url LIKE '%抖音%' THEN 'douyin'
                    WHEN lower(COALESCE(source_url,'')) LIKE '%kuaishou%' OR source_url LIKE '%快手%' THEN 'kuaishou'
                    WHEN lower(COALESCE(source_url,'')) LIKE '%xiaohongshu%' OR lower(COALESCE(source_url,'')) LIKE '%xhslink%' OR source_url LIKE '%小红书%' THEN 'xiaohongshu'
                    ELSE 'direct'
                END AS platform,COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END),0) AS succeeded,
                COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM jobs WHERE source_type='remote' AND created_at>=? AND created_at<? GROUP BY platform
            """,
            seven_days,
        ).fetchall()
        ad_summary = db.execute(
            """
                SELECT COUNT(*) AS prepared,
                    COALESCE(SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END),0) AS claimed,
                    COALESCE(SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END),0) AS expired
                FROM reward_claims WHERE created_at>=? AND created_at<?
            """,
            today,
        ).fetchone()
        credit_summary = db.execute(
            """
                SELECT COALESCE(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),0) AS consumed,
                    COALESCE(SUM(CASE WHEN amount>0 AND reason LIKE '%refund%' THEN amount ELSE 0 END),0) AS refunded,
                    COALESCE(SUM(CASE WHEN amount>0 AND reason LIKE '%refund%' THEN 1 ELSE 0 END),0) AS refund_count
                FROM credit_ledger WHERE created_at>=? AND created_at<?
            """,
            today,
        ).fetchone()
        stale_analysis = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='processing' AND updated_at<?",
            ((utc_now() - timedelta(minutes=15)).isoformat(),),
        ).fetchone()[0]
        stale_depth = db.execute(
            "SELECT COUNT(*) FROM depth_jobs WHERE status NOT IN ('completed','failed') AND updated_at<?",
            ((utc_now() - timedelta(minutes=30)).isoformat(),),
        ).fetchone()[0]

    summary = row_dict(today_summary)
    summary["success_rate"] = round(summary["succeeded"] * 100 / summary["total"], 1) if summary["total"] else 0
    type_labels = {"image": "图片反推", "video": "视频反推", "depth": "深度转换", "optimization": "提示词优化"}
    type_metrics = []
    for row in task_types:
        item = row_dict(row)
        item["label"] = type_labels.get(item["task_type"], item["task_type"])
        item["success_rate"] = round(item["succeeded"] * 100 / item["total"], 1) if item["total"] else 0
        type_metrics.append(item)

    trend_by_day = {row["day"]: row_dict(row) for row in trend_rows}
    trend = []
    for offset in range(7):
        day = (seven_day_start.astimezone(REPORT_TIMEZONE).date() + timedelta(days=offset)).isoformat()
        trend.append(trend_by_day.get(day, {"day": day, "total": 0, "succeeded": 0, "failed": 0}))

    failure_counter = Counter(classify_failure(row["error_message"]) for row in failed_rows)
    failures = [{"category": category, "count": count} for category, count in failure_counter.most_common(10)]
    recent_failures = [
        {"task_type": type_labels.get(row["task_type"], row["task_type"]), "category": classify_failure(row["error_message"]),
         "message": row["error_message"] or "未知错误", "created_at": row["created_at"]}
        for row in failed_rows[:10]
    ]
    platform_labels = {"douyin": "抖音", "kuaishou": "快手", "xiaohongshu": "小红书", "direct": "视频直链"}
    platforms = []
    for row in platform_rows:
        item = row_dict(row)
        item["label"] = platform_labels.get(item["platform"], item["platform"])
        item["success_rate"] = round(item["succeeded"] * 100 / item["total"], 1) if item["total"] else 0
        platforms.append(item)

    ads = row_dict(ad_summary)
    ads["completion_rate"] = round(ads["claimed"] * 100 / ads["prepared"], 1) if ads["prepared"] else 0
    alerts: list[dict[str, str]] = []
    if summary["total"] >= 5 and summary["failed"] / summary["total"] > 0.2:
        alerts.append({"severity": "critical", "title": "今日任务失败率偏高", "detail": f"失败 {summary['failed']} / {summary['total']}，失败率 {round(summary['failed'] * 100 / summary['total'], 1)}%"})
    for item in type_metrics:
        if item["total"] >= 5 and item["failed"] / item["total"] > 0.3:
            alerts.append({"severity": "warning", "title": f"{item['label']}成功率偏低", "detail": f"近 7 日成功率 {item['success_rate']}%，共 {item['total']} 次"})
    for item in platforms:
        if item["total"] >= 3 and item["failed"] / item["total"] > 0.3:
            alerts.append({"severity": "warning", "title": f"{item['label']}解析异常", "detail": f"近 7 日成功率 {item['success_rate']}%，失败 {item['failed']} 次"})
    vision_failures = failure_counter.get("Vision API", 0)
    if vision_failures >= 3:
        alerts.append({"severity": "critical", "title": "Vision API 连续异常", "detail": f"近 7 日累计失败 {vision_failures} 次，请检查上游可用性"})
    if stale_analysis:
        alerts.append({"severity": "critical", "title": "分析任务长时间未完成", "detail": f"{stale_analysis} 个任务超过 15 分钟未更新"})
    if stale_depth:
        alerts.append({"severity": "critical", "title": "深度任务可能卡住", "detail": f"{stale_depth} 个任务超过 30 分钟未更新"})
    if ads["prepared"] >= 5 and ads["completion_rate"] < 40:
        alerts.append({"severity": "warning", "title": "激励广告完成率偏低", "detail": f"今日完成率 {ads['completion_rate']}%，准备 {ads['prepared']} 次"})

    return {
        "generated_at": utc_now().isoformat(),
        "timezone": str(REPORT_TIMEZONE),
        "users": {"new_today": new_users_today, "new_yesterday": new_users_yesterday, "active_today": active_users_today},
        "today": summary,
        "credits": row_dict(credit_summary),
        "ads": ads,
        "task_types": type_metrics,
        "trend": trend,
        "failures": failures,
        "recent_failures": recent_failures,
        "platforms": platforms,
        "alerts": alerts,
    }


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
        "content_security_configured": wechat_app_configured and wechat_secret_configured,
        "ad_configured": ad_configured,
        "admin_configured": bool(ADMIN_PASSWORD_HASH),
        "https_required": not dev_login,
        "pricing": {"image": IMAGE_CREDIT_COST, "video": VIDEO_CREDIT_COST, "depth": int(os.getenv("DEPTH_COMPUTE_COST", "1"))},
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
