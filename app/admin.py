from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.commercial import AD_DAILY_LIMIT, IMAGE_CREDIT_COST, VIDEO_CREDIT_COST, change_credits, connect, hash_token, utc_now

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


class TaskOperation(BaseModel):
    reason: str = Field(min_length=2, max_length=200)


class UserProfileUpdate(BaseModel):
    admin_note: str = Field(default="", max_length=2000)
    risk_level: str = Field(default="normal", pattern="^(normal|watch|high|banned)$")
    reason: str = Field(min_length=2, max_length=200)


class UserStatusUpdate(BaseModel):
    reason: str = Field(min_length=2, max_length=200)


class FeedbackUpdate(BaseModel):
    status: str = Field(pattern="^(open|in_progress|resolved|closed)$")
    admin_tags: str = Field(default="", max_length=300)
    admin_note: str = Field(default="", max_length=2000)
    reply: str = Field(default="", max_length=2000)
    reason: str = Field(min_length=2, max_length=200)


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
    return {"role": "admin", "username": ADMIN_USERNAME}


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
        SELECT id,user_id,mode AS task_type,status,cost,filename,source_type,source_url,source_platform,
            analysis_task AS task_detail,result_json,error_message,created_at,updated_at FROM jobs
        UNION ALL
        SELECT id,user_id,'depth' AS task_type,
            CASE WHEN status='completed' THEN 'succeeded' WHEN status='failed' THEN 'failed' ELSE 'processing' END,
            cost,filename,source_type,NULL AS source_url,NULL AS source_platform,preset AS task_detail,
            result_json,error_message,created_at,updated_at FROM depth_jobs
        UNION ALL
        SELECT o.id,o.user_id,'optimization' AS task_type,o.status,o.cost,
            COALESCE(j.filename,'提示词优化') AS filename,'analysis' AS source_type,NULL AS source_url,
            o.platform AS source_platform,o.strategy AS task_detail,o.result_json,o.error_message,o.created_at,o.updated_at
            FROM prompt_optimizations o LEFT JOIN jobs j ON j.id=o.job_id
        UNION ALL
        SELECT id,user_id,'diagnostic' AS task_type,
            CASE WHEN status='succeeded' THEN 'succeeded' WHEN status='failed' THEN 'failed' ELSE 'processing' END,
            cost,COALESCE(original_filename,'视频复刻诊断') AS filename,'upload' AS source_type,NULL AS source_url,
            NULL AS source_platform,generated_filename AS task_detail,result_json,error_message,created_at,updated_at
            FROM replication_diagnostics
    )
"""

TASK_TYPE_LABELS = {
    "image": "图片反推", "video": "视频反推", "depth": "深度转换",
    "optimization": "提示词优化", "diagnostic": "视频复刻诊断",
}

TASK_TABLES = {
    "image": ("jobs", "job", "status", "failed"),
    "video": ("jobs", "job", "status", "failed"),
    "depth": ("depth_jobs", "depth_job", "status", "failed"),
    "optimization": ("prompt_optimizations", "prompt_optimization", "status", "failed"),
    "diagnostic": ("replication_diagnostics", "replication_diagnostic", "status", "failed"),
}


def normalized_task_status(task_type: str, status: str) -> str:
    if task_type == "depth":
        return "succeeded" if status == "completed" else "failed" if status == "failed" else "processing"
    if task_type == "diagnostic":
        return "succeeded" if status == "succeeded" else "failed" if status == "failed" else "processing"
    return status


def sanitize_result(value: Any) -> Any:
    blocked = {"api_key", "apikey", "authorization", "token", "secret", "password", "path"}
    if isinstance(value, dict):
        return {key: sanitize_result(item) for key, item in value.items() if key.lower() not in blocked}
    if isinstance(value, list):
        return [sanitize_result(item) for item in value]
    return value


def task_result_preview(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return sanitize_result(json.loads(raw))
    except (TypeError, ValueError):
        return raw[:4000]


def write_audit_log(db: Any, admin: dict[str, str], action: str, target_type: str, target_id: int, user_id: int, reason: str, metadata: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO admin_audit_logs(admin_username,action,target_type,target_id,user_id,reason,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (admin["username"], action, target_type, str(target_id), user_id, reason, json.dumps(metadata or {}, ensure_ascii=False), utc_now().isoformat()),
    )


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
    type_labels = {"image": "图片反推", "video": "视频反推", "depth": "深度转换", "optimization": "提示词优化", "diagnostic": "视频复刻诊断"}
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


@admin_router.get("/monitoring/upstream")
async def upstream_monitoring(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    start, end = report_day_bounds(-6)
    services = {
        "vision": ("Vision API", "error_message LIKE '%Vision%' OR error_message LIKE '%OpenAI%' OR error_message LIKE '%模型%'"),
        "video_parser": ("视频链接解析", "source_type='remote'"),
        "depth": ("深度服务", "task_type='depth'"),
        "content_security": ("内容安全", "error_message LIKE '%内容安全%' OR error_message LIKE '%违规%'"),
    }
    with connect() as db:
        task_rows = db.execute(TASKS_CTE + "SELECT task_type,status,cost,error_message,created_at,updated_at,source_type FROM tasks WHERE created_at>=? AND created_at<?", (start.isoformat(), end.isoformat())).fetchall()
    result = []
    for key, (label, condition) in services.items():
        selected = []
        for row in task_rows:
            text = (row["error_message"] or "") + " " + (row["source_type"] or "") + " " + row["task_type"]
            if key == "vision" and any(token in text.lower() for token in ("vision", "openai", "模型", "额度", "api key")):
                selected.append(row)
            elif key == "video_parser" and row["source_type"] == "remote":
                selected.append(row)
            elif key == "depth" and row["task_type"] == "depth":
                selected.append(row)
            elif key == "content_security" and any(token in text for token in ("内容安全", "违规", "security")):
                selected.append(row)
        total = len(selected)
        failed = sum(1 for row in selected if row["status"] == "failed")
        durations = [(datetime.fromisoformat(row["updated_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds() for row in selected if row["status"] in {"succeeded", "failed"}]
        rate = round((total - failed) * 100 / total, 1) if total else 100.0
        result.append({"key": key, "label": label, "requests": total, "failed": failed, "success_rate": rate, "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0, "status": "degraded" if rate < 80 else "healthy", "recommendation": "启用熔断并检查上游额度" if rate < 80 else "运行正常"})
    return {"window": {"from": start.isoformat(), "to": end.isoformat()}, "services": result, "generated_at": utc_now().isoformat()}


@admin_router.get("/users")
async def users(query: str = "", limit: int = 50, _: dict[str, str] = Depends(admin_user)) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    pattern = "%" + query.strip() + "%"
    with connect() as db:
        rows = db.execute("SELECT id,openid,unionid,credits,is_blocked,admin_note,risk_level,block_reason,blocked_at,blocked_by,created_at,updated_at FROM users WHERE openid LIKE ? OR CAST(id AS TEXT) LIKE ? ORDER BY id DESC LIMIT ?", (pattern, pattern, limit)).fetchall()
    return [row_dict(row) for row in rows]


@admin_router.get("/users/{user_id}")
async def user_detail(user_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        user = db.execute("SELECT id,openid,unionid,credits,is_blocked,admin_note,risk_level,block_reason,blocked_at,blocked_by,created_at,updated_at FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        ledger = db.execute("SELECT id,amount,balance_after,reason,reference_type,reference_id,created_at FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        jobs = db.execute("SELECT id,mode,filename,cost,status,error_message,created_at,updated_at FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        depth = db.execute("SELECT id,preset,filename,cost,status,error_message,created_at,updated_at FROM depth_jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        optimizations = db.execute("SELECT id,job_id,strategy,platform,cost,status,error_message,created_at,updated_at FROM prompt_optimizations WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        diagnostics = db.execute("SELECT id,original_filename,generated_filename,cost,status,error_message,created_at,updated_at FROM replication_diagnostics WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        ads = db.execute("SELECT id,status,expires_at,claimed_at,created_at FROM reward_claims WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
        projects = db.execute("SELECT id,title,note,is_favorite,recent_platform,created_at,updated_at FROM creative_projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 50", (user_id,)).fetchall()
        audits = db.execute("SELECT id,admin_username,action,target_type,target_id,reason,metadata_json,created_at FROM admin_audit_logs WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
    return {"user": row_dict(user), "ledger": [row_dict(row) for row in ledger], "jobs": [row_dict(row) for row in jobs], "depth_jobs": [row_dict(row) for row in depth], "optimizations": [row_dict(row) for row in optimizations], "diagnostics": [row_dict(row) for row in diagnostics], "ads": [row_dict(row) for row in ads], "projects": [row_dict(row) for row in projects], "audits": [row_dict(row) for row in audits]}


@admin_router.patch("/users/{user_id}/profile")
async def update_user_profile(user_id: int, body: UserProfileUpdate, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        note = body.admin_note.strip()
        db.execute("UPDATE users SET admin_note=?,risk_level=?,updated_at=? WHERE id=?", (note, body.risk_level, utc_now().isoformat(), user_id))
        write_audit_log(db, admin, "profile_update", "user", user_id, user_id, body.reason, {"risk_level": body.risk_level, "note_length": len(note)})
        db.commit()
        row = db.execute("SELECT id,openid,unionid,credits,is_blocked,admin_note,risk_level,block_reason,blocked_at,blocked_by,created_at,updated_at FROM users WHERE id=?", (user_id,)).fetchone()
    return row_dict(row)


@admin_router.post("/users/{user_id}/credits")
async def adjust_credits(user_id: int, body: CreditAdjustment, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        balance = change_credits(db, user_id, body.amount, "admin_adjustment:" + body.reason, "admin", str(user_id), "admin:" + secrets.token_hex(16))
        write_audit_log(db, admin, "credit_adjustment", "user", user_id, user_id, body.reason, {"amount": body.amount, "balance_after": balance})
        db.commit()
    return {"user_id": user_id, "credits": balance}


@admin_router.post("/users/{user_id}/block")
async def block_user(user_id: int, body: UserStatusUpdate | None = None, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        now = utc_now().isoformat()
        reason = body.reason if body else "后台封禁（未填写原因）"
        db.execute("UPDATE users SET is_blocked=1,risk_level='banned',block_reason=?,blocked_at=?,blocked_by=?,updated_at=? WHERE id=?", (reason, now, admin["username"], now, user_id))
        write_audit_log(db, admin, "block", "user", user_id, user_id, reason)
        db.commit()
    return {"user_id": user_id, "is_blocked": True}


@admin_router.post("/users/{user_id}/unblock")
async def unblock_user(user_id: int, body: UserStatusUpdate | None = None, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        db.execute("UPDATE users SET is_blocked=0,risk_level='normal',block_reason='',blocked_at=NULL,blocked_by=NULL,updated_at=? WHERE id=?", (utc_now().isoformat(), user_id))
        write_audit_log(db, admin, "unblock", "user", user_id, user_id, body.reason if body else "后台解除封禁（未填写原因）")
        db.commit()
    return {"user_id": user_id, "is_blocked": False}


@admin_router.get("/feedback")
async def feedback_tickets(status: str = "", query: str = "", limit: int = 50, offset: int = 0, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    allowed_statuses = {"open", "in_progress", "resolved", "closed"}
    if status and status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="不支持的工单状态")
    limit = max(1, min(limit, 200))
    offset = max(0, min(offset, 100000))
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("f.status=?")
        params.append(status)
    if query.strip():
        pattern = "%" + query.strip() + "%"
        clauses.append("(CAST(f.id AS TEXT) LIKE ? OR CAST(f.task_id AS TEXT) LIKE ? OR CAST(f.user_id AS TEXT) LIKE ? OR f.content LIKE ? OR f.admin_tags LIKE ? OR u.openid LIKE ?)")
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM feedback_tickets f JOIN users u ON u.id=f.user_id" + where, params).fetchone()[0]
        rows = db.execute(
            "SELECT f.id,f.user_id,f.task_type,f.task_id,f.category,f.content,f.status,f.admin_tags,f.reply,f.replied_at,f.created_at,f.updated_at,u.openid "
            "FROM feedback_tickets f JOIN users u ON u.id=f.user_id" + where + " ORDER BY f.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"items": [row_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@admin_router.get("/feedback/{ticket_id}")
async def feedback_ticket_detail(ticket_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT f.*,u.openid,u.credits,u.is_blocked FROM feedback_tickets f JOIN users u ON u.id=f.user_id WHERE f.id=?",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="工单不存在")
        audits = db.execute(
            "SELECT id,admin_username,action,reason,metadata_json,created_at FROM admin_audit_logs WHERE target_type='feedback_ticket' AND target_id=? ORDER BY id DESC LIMIT 50",
            (str(ticket_id),),
        ).fetchall()
    return {"ticket": row_dict(row), "audits": [row_dict(item) for item in audits]}


@admin_router.patch("/feedback/{ticket_id}")
async def update_feedback_ticket(ticket_id: int, body: FeedbackUpdate, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    now = utc_now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM feedback_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="工单不存在")
        reply = body.reply.strip()
        db.execute(
            "UPDATE feedback_tickets SET status=?,admin_tags=?,admin_note=?,reply=?,replied_at=?,replied_by=?,updated_at=? WHERE id=?",
            (body.status, body.admin_tags.strip(), body.admin_note.strip(), reply, now if reply else None, admin["username"] if reply else None, now, ticket_id),
        )
        write_audit_log(db, admin, "feedback_update", "feedback_ticket", ticket_id, row["user_id"], body.reason, {"status": body.status, "tags": body.admin_tags.strip(), "has_reply": bool(reply)})
        db.commit()
        updated = db.execute("SELECT * FROM feedback_tickets WHERE id=?", (ticket_id,)).fetchone()
    return row_dict(updated)


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
async def refund_job(job_id: int, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] == "processing":
            raise HTTPException(status_code=409, detail="任务仍在处理中，请稍后再退款")
        existing = db.execute("SELECT balance_after FROM credit_ledger WHERE reference_type='job' AND reference_id=? AND amount>0", (str(job_id),)).fetchone()
        if existing:
            return {"job_id": job_id, "credits": existing["balance_after"], "idempotent": True}
        balance = change_credits(db, job["user_id"], job["cost"], "admin_refund:旧任务入口", "job", str(job_id), f"admin:refund:job:{job_id}")
        db.execute("UPDATE jobs SET status='failed',error_message='管理员手动退款',updated_at=? WHERE id=?", (utc_now().isoformat(), job_id))
        write_audit_log(db, admin, "refund", "job", job_id, job["user_id"], "旧任务入口退款", {"cost": job["cost"]})
        db.commit()
    return {"job_id": job_id, "credits": balance, "idempotent": False}


@admin_router.get("/operations/tasks")
async def operation_tasks(
    task_type: str = "", status: str = "", query: str = "",
    created_after: str = "", created_before: str = "", failure: str = "",
    limit: int = 50, offset: int = 0, _: dict[str, str] = Depends(admin_user),
) -> dict[str, Any]:
    allowed_types = set(TASK_TYPE_LABELS)
    if task_type and task_type not in allowed_types:
        raise HTTPException(status_code=400, detail="不支持的任务类型")
    normalized_statuses = {"processing", "succeeded", "failed"}
    if status and status not in normalized_statuses:
        raise HTTPException(status_code=400, detail="不支持的任务状态")
    limit = max(1, min(limit, 200))
    offset = max(0, min(offset, 100000))
    clauses: list[str] = []
    params: list[Any] = []
    if task_type:
        clauses.append("task_type=?")
        params.append(task_type)
    if status:
        clauses.append("status=?")
        params.append(status)
    if query.strip():
        pattern = f"%{query.strip()}%"
        clauses.append("(CAST(id AS TEXT) LIKE ? OR CAST(user_id AS TEXT) LIKE ? OR filename LIKE ? OR COALESCE(error_message,'') LIKE ? OR user_id IN (SELECT id FROM users WHERE openid LIKE ?))")
        params.extend([pattern, pattern, pattern, pattern, pattern])
    if created_after:
        clauses.append("created_at>=?")
        params.append(created_after)
    if created_before:
        clauses.append("created_at<=?")
        params.append(created_before)
    if failure.strip():
        clauses.append("COALESCE(error_message,'') LIKE ?")
        params.append(f"%{failure.strip()}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as db:
        total = db.execute(TASKS_CTE + f"SELECT COUNT(*) FROM tasks{where}", params).fetchone()[0]
        rows = db.execute(
            TASKS_CTE + f"SELECT id,user_id,task_type,status,cost,filename,source_type,source_url,source_platform,task_detail,error_message,created_at,updated_at,CASE WHEN status IN ('succeeded','failed') THEN ROUND((julianday(updated_at)-julianday(created_at))*86400) ELSE NULL END AS duration_seconds FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        user_ids = {row["user_id"] for row in rows}
        users = {}
        if user_ids:
            marks = ",".join("?" for _ in user_ids)
            users = {row["id"]: row["openid"] for row in db.execute(f"SELECT id,openid FROM users WHERE id IN ({marks})", tuple(user_ids)).fetchall()}
    items = []
    for row in rows:
        item = row_dict(row)
        item["label"] = TASK_TYPE_LABELS.get(item["task_type"], item["task_type"])
        item["openid"] = users.get(item["user_id"], "未知用户")
        item["error_message"] = (item["error_message"] or "")[:240]
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def load_task(db: Any, task_type: str, task_id: int) -> Any:
    config = TASK_TABLES.get(task_type)
    if not config:
        raise HTTPException(status_code=400, detail="不支持的任务类型")
    if task_type in {"image", "video"}:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND mode=?", (task_id, task_type)).fetchone()
    else:
        row = db.execute(f"SELECT * FROM {config[0]} WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


@admin_router.get("/operations/tasks/{task_type}/{task_id}")
async def operation_task_detail(task_type: str, task_id: int, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        row = load_task(db, task_type, task_id)
        table, reference_type, _, _ = TASK_TABLES[task_type]
        ledger = db.execute("SELECT id,amount,balance_after,reason,reference_type,reference_id,created_at FROM credit_ledger WHERE reference_type=? AND reference_id=? ORDER BY id DESC", (reference_type, str(task_id))).fetchall()
        audits = db.execute("SELECT id,admin_username,action,reason,metadata_json,created_at FROM admin_audit_logs WHERE target_type=? AND target_id=? ORDER BY id DESC LIMIT 30", (reference_type, str(task_id))).fetchall()
        user = db.execute("SELECT id,openid,credits,is_blocked FROM users WHERE id=?", (row["user_id"],)).fetchone()
    payload = row_dict(row)
    payload["task_type"] = task_type
    payload["label"] = TASK_TYPE_LABELS[task_type]
    payload["status"] = normalized_task_status(task_type, payload.get("status", ""))
    payload["result"] = task_result_preview(payload.pop("result_json", None))
    for key in ("original_path", "generated_path", "artifact_path", "frames_path", "manifest_path", "package_path"):
        payload.pop(key, None)
    return {"task": payload, "user": row_dict(user), "ledger": [row_dict(item) for item in ledger], "audits": [row_dict(item) for item in audits]}


@admin_router.post("/operations/tasks/{task_type}/{task_id}/refund")
async def operation_task_refund(task_type: str, task_id: int, body: TaskOperation, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = load_task(db, task_type, task_id)
        table, reference_type, status_column, failed_status = TASK_TABLES[task_type]
        current_status = normalized_task_status(task_type, row[status_column])
        if current_status == "processing":
            raise HTTPException(status_code=409, detail="任务仍在处理中，不能退款")
        existing = db.execute("SELECT balance_after FROM credit_ledger WHERE reference_type=? AND reference_id=? AND amount>0", (reference_type, str(task_id))).fetchone()
        if existing:
            return {"task_type": task_type, "task_id": task_id, "credits": existing["balance_after"], "idempotent": True}
        balance = change_credits(db, row["user_id"], row["cost"], f"admin_refund:{body.reason}", reference_type, str(task_id), f"admin:refund:{reference_type}:{task_id}")
        db.execute(f"UPDATE {table} SET {status_column}=?,error_message=?,updated_at=? WHERE id=?", (failed_status, f"管理员退款：{body.reason}", utc_now().isoformat(), task_id))
        write_audit_log(db, admin, "refund", reference_type, task_id, row["user_id"], body.reason, {"cost": row["cost"]})
        db.commit()
    return {"task_type": task_type, "task_id": task_id, "credits": balance, "idempotent": False}


@admin_router.post("/operations/tasks/{task_type}/{task_id}/close")
async def operation_task_close(task_type: str, task_id: int, body: TaskOperation, admin: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = load_task(db, task_type, task_id)
        table, reference_type, status_column, failed_status = TASK_TABLES[task_type]
        if normalized_task_status(task_type, row[status_column]) != "processing":
            raise HTTPException(status_code=409, detail="只有处理中的任务可以关闭")
        db.execute(f"UPDATE {table} SET {status_column}=?,error_message=?,updated_at=? WHERE id=?", (failed_status, f"管理员关闭：{body.reason}", utc_now().isoformat(), task_id))
        balance = change_credits(db, row["user_id"], row["cost"], f"admin_close_refund:{body.reason}", reference_type, str(task_id), f"admin:close_refund:{reference_type}:{task_id}")
        write_audit_log(db, admin, "close", reference_type, task_id, row["user_id"], body.reason, {"refund": row["cost"]})
        db.commit()
    return {"task_type": task_type, "task_id": task_id, "credits": balance}
