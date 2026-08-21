from __future__ import annotations

import base64
from io import BytesIO
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from app.wechat_security import check_images

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("prompt-lens")
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ADMIN_STATIC_DIR = BASE_DIR / "admin_static"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "prompt-lens.sqlite3"
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_MB", "12")) * 1024 * 1024
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_MB", "180")) * 1024 * 1024
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "90"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ENABLE_LEGACY_WEB_API = os.getenv("ENABLE_LEGACY_WEB_API", "false").lower() == "true"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}


class TimelineSegment(BaseModel):
    start: str
    end: str
    description: str
    camera_motion: str = ""
    subject_motion: str = ""


class VisualAnalysis(BaseModel):
    subject: str
    scene: str
    composition: str
    camera: str
    lighting: str
    color: str
    style: str
    details: list[str] = Field(default_factory=list)
    negative_prompt: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=0, le=100)
    timeline: list[TimelineSegment] = Field(default_factory=list)
    prompt_zh: str = ""
    prompt_en: str = ""


class PromptBundle(BaseModel):
    universal: str
    midjourney: str
    flux: str
    video: str
    chinese: str = ""
    english: str = ""
    platforms: dict[str, dict[str, str]] = Field(default_factory=dict)


class AnalysisResponse(BaseModel):
    id: int
    created_at: str
    mode: str
    source: str
    filename: str
    analysis: VisualAnalysis
    prompts: PromptBundle
    note: str


class HistoryItem(BaseModel):
    id: int
    created_at: str
    mode: str
    filename: str
    subject: str
    confidence: int


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"}, "scene": {"type": "string"},
        "composition": {"type": "string"}, "camera": {"type": "string"},
        "lighting": {"type": "string"}, "color": {"type": "string"},
        "style": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}},
        "negative_prompt": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "prompt_zh": {"type": "string"}, "prompt_en": {"type": "string"},
        "timeline": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}, "camera_motion": {"type": "string"}, "subject_motion": {"type": "string"}},
            "required": ["start", "end", "description", "camera_motion", "subject_motion"]}},
    },
    "required": ["subject", "scene", "composition", "camera", "lighting", "color", "style", "details", "negative_prompt", "confidence", "timeline", "prompt_zh", "prompt_en"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, mode TEXT NOT NULL, filename TEXT NOT NULL, subject TEXT NOT NULL, confidence INTEGER NOT NULL, payload TEXT NOT NULL)")
    return connection


def save_analysis(mode: str, filename: str, analysis: VisualAnalysis, prompts: PromptBundle) -> int:
    with get_db() as db:
        cursor = db.execute("INSERT INTO analyses (created_at, mode, filename, subject, confidence, payload) VALUES (?, ?, ?, ?, ?, ?)", (utc_now(), mode, filename, analysis.subject, analysis.confidence, json.dumps({"analysis": analysis.model_dump(), "prompts": prompts.model_dump()}, ensure_ascii=False)))
        return int(cursor.lastrowid)


def make_prompts(analysis: VisualAnalysis, video_prompt: bool = False) -> PromptBundle:
    details = ", ".join(analysis.details) or "highly resolved natural textures"
    negative = ", ".join(analysis.negative_prompt) or "text, logo, watermark, artifacts"
    universal = analysis.prompt_zh or f"{analysis.subject}。{analysis.scene}。{analysis.composition}。{analysis.camera}。{analysis.lighting}。{analysis.color}。{analysis.style}。细节：{details}。"
    english = analysis.prompt_en or f"{analysis.subject}. {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. {analysis.color}. {analysis.style}. Details: {details}."
    midjourney = f"{analysis.subject}, {analysis.scene}, {analysis.composition}, {analysis.camera}, {analysis.lighting}, {analysis.style}, {details} --ar 4:5 --stylize 180 --no {negative}"
    flux = f"A cinematic editorial image of {analysis.subject}, in {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. Color palette: {analysis.color}. Style: {analysis.style}. Details: {details}. Avoid {negative}."
    timeline = " ".join(f"{item.start}-{item.end}: {item.description}; camera: {item.camera_motion}; subject: {item.subject_motion}." for item in analysis.timeline)
    video = analysis.prompt_zh if video_prompt and analysis.prompt_zh else f"{analysis.subject}. {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. Preserve identity, wardrobe and spatial continuity. {timeline} 24fps, realistic motion blur, cinematic pacing. Avoid {negative}."
    video_en = analysis.prompt_en if video_prompt and analysis.prompt_en else f"{analysis.subject}. {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. Preserve identity, wardrobe and spatial continuity. {timeline} 24fps, realistic motion blur, cinematic pacing. Avoid {negative}."
    image_jimeng = f"主体：{analysis.subject}\n场景：{analysis.scene}\n构图：{analysis.composition}\n镜头：{analysis.camera}\n光影：{analysis.lighting}\n色彩与风格：{analysis.color}，{analysis.style}\n细节：{details}\n负面约束：{negative}"
    video_kling = f"生成一段连续视频。主体：{analysis.subject}。场景：{analysis.scene}。首帧构图：{analysis.composition}。镜头：{analysis.camera}。光影：{analysis.lighting}。{timeline} 保持人物身份、服装、空间结构和光线连续，动作自然，运动速度真实，避免闪烁、变形、跳帧和新增内容。"
    video_jimeng = f"画面主体：{analysis.subject}。环境：{analysis.scene}。起始构图：{analysis.composition}。运镜方式：{analysis.camera}。光影与色彩：{analysis.lighting}，{analysis.color}。动作推进：{timeline} 画面节奏连贯，主体动作清楚，保持外观、服装、场景和光线稳定，结尾自然停稳。"
    video_hailuo = f"电影感连续镜头，{analysis.subject}，位于{analysis.scene}。开场画面：{analysis.composition}。镜头语言：{analysis.camera}。光线与风格：{analysis.lighting}，{analysis.style}。时间推进：{timeline} 强调真实运动惯性、自然表情和衣物动态，保持人物与背景一致，避免闪烁、肢体畸变和场景漂移。"
    video_runway = f"A continuous cinematic video of {analysis.subject} in {analysis.scene}. Start with {analysis.composition}. Camera: {analysis.camera}. Lighting: {analysis.lighting}. Maintain identity, wardrobe, spatial continuity and stable geometry. Timeline: {timeline} Natural motion, realistic speed, consistent temporal detail, no cuts, no morphing, no extra subjects."
    video_veo = f"Create a coherent video shot of {analysis.subject} in {analysis.scene}. Shot composition: {analysis.composition}. Camera direction: {analysis.camera}. Lighting and color: {analysis.lighting}; {analysis.color}. Action progression: {timeline} Preserve physical continuity, identity, wardrobe and background geometry. Use natural motion blur and end in a stable final state."
    if video_prompt:
        platforms = {
            "universal": {"label": "通用", "zh": video, "en": video_en},
            "kling": {"label": "可灵", "zh": video_kling, "en": video_en},
            "jimeng": {"label": "即梦", "zh": video_jimeng, "en": video_en},
            "hailuo": {"label": "海螺", "zh": video_hailuo, "en": video_en},
            "runway": {"label": "Runway", "zh": video, "en": video_runway},
            "veo": {"label": "Veo", "zh": video, "en": video_veo},
        }
    else:
        platforms = {
            "universal": {"label": "通用", "zh": universal, "en": english},
            "midjourney": {"label": "Midjourney", "zh": midjourney, "en": midjourney},
            "flux": {"label": "Flux", "zh": flux, "en": flux},
            "jimeng": {"label": "即梦", "zh": image_jimeng, "en": english},
        }
    return PromptBundle(universal=universal, midjourney=midjourney, flux=flux, video=video, chinese=universal, english=english, platforms=platforms)


def as_data_url(content: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"


def parse_vision_payload(payload: dict[str, Any]) -> VisualAnalysis:
    try:
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content)
        content = content.strip()
        if content.startswith("```json") and content.endswith("```"):
            content = content[7:-3].strip()
        elif content.startswith("```") and content.endswith("```"):
            content = content[3:-3].strip()
        payload = json.loads(content)
        payload.setdefault("prompt_zh", "")
        payload.setdefault("prompt_en", "")
        return VisualAnalysis.model_validate(payload)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Vision API 返回了无法解析的结构化结果") from exc


def enforce_image_expansion_duration(analysis: VisualAnalysis) -> VisualAnalysis:
    prompt_zh = analysis.prompt_zh.strip()
    prompt_en = analysis.prompt_en.strip()
    if "10秒" not in prompt_zh:
        analysis.prompt_zh = f"10秒视频。{prompt_zh}" if prompt_zh else "10秒视频。"
    if "10-second" not in prompt_en.lower() and "10 second" not in prompt_en.lower():
        analysis.prompt_en = f"10-second video. {prompt_en}" if prompt_en else "10-second video."
    if analysis.timeline:
        analysis.timeline[0].start = "00:00"
        analysis.timeline[-1].end = "00:10"
    return analysis


async def call_vision(images: list[tuple[bytes, str, str]], analysis_depth: str = "detailed", analysis_task: str = "reconstruct") -> VisualAnalysis:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="服务尚未配置 OPENAI_API_KEY，无法进行真实分析")
    depth_guidance = {
        "standard": "标准维度：重点覆盖主体、场景、构图、镜头、光影、色彩和风格，提示词清晰精炼。",
        "detailed": "详细维度：在标准维度基础上补充材质、纹理、空间层次、环境细节、氛围、动作和负面约束。",
        "professional": "专业维度：完整描述镜头焦段、景别、机位、透视、布光方式、色彩管理、材质、后期质感、运动连续性、生成控制参数和负面约束。",
    }.get(analysis_depth, "详细维度：充分描述可见画面及生成控制细节。")
    if analysis_task == "image_expand_video":
        task_guidance = (
            "当前任务是将单张图片拓展为严格 10 秒的视频生成提示词。"
            "subject、scene、composition、camera、lighting、color、style 和 details 先准确描述首帧可见内容；"
            "timeline 必须覆盖完整 00:00-00:10，按连续时间段描述主体动作、镜头运动、环境动态和结尾画面，"
            "动作应自然可实现，保持主体身份、外观、服装、场景空间、光线和色彩连续，不得无故新增主要人物或改变场景。"
            "prompt_zh 必须是可直接用于视频生成模型的完整中文 10 秒提示词，明确首帧、时间推进、镜头语言、运动节奏、"
            "物理一致性和结尾状态；prompt_en 必须是语义一致、自然专业的完整英文 10-second video prompt。"
            "两种提示词都必须明确写出时长 10 秒。"
        )
    elif analysis_task == "video_reconstruct":
        task_guidance = (
            "当前任务是反推输入视频本身的视频生成提示词，目标是让视频生成模型尽可能复现原视频，而不是只描述某一帧。"
            "timeline 必须按输入关键帧时间覆盖完整视频过程，逐段还原主体动作、表情变化、物体运动、镜头运动、景别变化、"
            "环境动态、光影变化、节奏和结尾状态。subject、scene、composition、camera、lighting、color、style 和 details "
            "描述贯穿全片且稳定的视觉条件。prompt_zh 必须采用画面拓展视频提示词的完整格式，包含首帧设定、连续时间推进、"
            "逐段动作、镜头语言、运动速度、物理连续性和最终画面，可直接提交视频生成模型；prompt_en 必须提供语义一致的专业英文版本。"
            "必须复现观察到的内容，不得改写成静态图片提示词，不得擅自增加原视频没有的人物、动作、场景或剧情。"
        )
    else:
        task_guidance = (
            "只描述可见事实，不猜测品牌和身份。视频关键帧需要填写 timeline；"
            "单张图片的 timeline 返回空数组。"
        )
    instructions = (
        "分析输入媒体，只返回 JSON，不要 Markdown。必须完整包含且不得省略这些字段："
        "subject、scene、composition、camera、lighting、color、style、details、negative_prompt、confidence、timeline、prompt_zh、prompt_en。"
        "前七项是简洁中文字符串；details 和 negative_prompt 是字符串数组；confidence 是 0 到 100 的整数；"
        "timeline 是对象数组，每项必须包含 start、end、description、camera_motion、subject_motion。"
        "prompt_zh 是可直接用于生成模型的完整中文提示词，prompt_en 是语义一致且自然专业的完整英文提示词，不得简单拼音化。"
        f"{task_guidance}{depth_guidance}即使画面简单也必须填写全部字段。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": instructions}]
    for data, content_type, label in images:
        content.append({"type": "text", "text": f"参考帧：{label}"})
        content.append({"type": "image_url", "image_url": {"url": as_data_url(data, content_type)}})
    messages = [
        {"role": "system", "content": "你是视觉分析专家，必须严格执行用户指定的 JSON 输出格式。"},
        {"role": "user", "content": content},
    ]
    formats = [
        {"type": "json_schema", "json_schema": {"name": "visual_analysis", "strict": True, "schema": ANALYSIS_SCHEMA}},
        {"type": "json_object"},
    ]
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        for attempt, response_format in enumerate(formats, start=1):
            body = {"model": OPENAI_MODEL, "temperature": 0.15, "response_format": response_format, "messages": messages}
            try:
                response = await client.post(
                    f"{OPENAI_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail="无法连接 Vision API") from exc
            if response.is_error:
                logger.error("Vision API %s: %s", response.status_code, response.text[:500])
                if attempt == len(formats):
                    raise HTTPException(status_code=502, detail="Vision API 请求失败，请检查模型、额度和 API key")
                continue
            try:
                analysis = parse_vision_payload(response.json())
                return enforce_image_expansion_duration(analysis) if analysis_task == "image_expand_video" else analysis
            except HTTPException:
                if attempt == len(formats):
                    raise
                logger.warning("Vision API 未遵循 JSON Schema，切换到兼容 JSON 模式重试")
    raise HTTPException(status_code=502, detail="Vision API 未返回有效结果")


def run_command(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail="服务器缺少视频处理依赖 ffmpeg/ffprobe") from exc


def video_frames(video_path: str, duration: float) -> list[tuple[bytes, str, str]]:
    timestamps = [0.0, duration * .2, duration * .4, duration * .6, duration * .8, max(0.0, duration - .1)]
    frames: list[tuple[bytes, str, str]] = []
    for index, timestamp in enumerate(dict.fromkeys(round(value, 2) for value in timestamps), start=1):
        label = f"{timestamp:.2f}s / frame {index}"
        safe_end = max(0.0, duration - 0.5)
        seek_points = dict.fromkeys(round(value, 2) for value in (timestamp, min(timestamp, safe_end), max(0.0, timestamp - 0.25), max(0.0, timestamp - 1.0)))
        frame_data = b""
        frame_type = "image/jpeg"
        last_error = ""
        for seek_point in seek_points:
            # Fast seek is used first; accurate seek and PNG output cover damaged keyframes and unusual codecs.
            commands = [
                (["-ss", str(seek_point), "-i", video_path, "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe"], "image/jpeg"),
                (["-i", video_path, "-ss", str(seek_point), "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe"], "image/jpeg"),
                (["-ss", str(seek_point), "-i", video_path, "-c:v", "png", "-f", "image2pipe"], "image/png"),
            ]
            for options, mime in commands:
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "50M", "-analyzeduration", "100M", *options, "-frames:v", "1", "-vf", "scale=1280:-2", "pipe:1"]
                try:
                    result = subprocess.run(command, capture_output=True, check=False, timeout=45)
                except (OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)
                    continue
                last_error = result.stderr.decode("utf-8", errors="replace")[-300:].strip()
                candidate = result.stdout
                if result.returncode != 0 or not candidate:
                    continue
                try:
                    with Image.open(BytesIO(candidate)) as image:
                        image.verify()
                except (OSError, ValueError):
                    continue
                frame_data, frame_type = candidate, mime
                break
            if frame_data:
                break
        if not frame_data:
            logger.error("Video frame extraction failed: label=%s duration=%.2f stderr=%s", label, duration, last_error)
            raise HTTPException(status_code=422, detail="视频关键帧提取失败")
        frames.append((frame_data, frame_type, label))
    return frames


def get_video_duration(path: str) -> float:
    try:
        duration = float(run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="无法读取视频时长") from exc
    if duration <= 0 or duration > MAX_VIDEO_SECONDS:
        raise HTTPException(status_code=422, detail=f"视频时长必须在 1-{MAX_VIDEO_SECONDS} 秒之间")
    return duration


app = FastAPI(title="Prompt Lens API", version="1.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/admin/static", StaticFiles(directory=ADMIN_STATIC_DIR), name="admin-static")


@app.get("/health")
async def health() -> dict[str, Any]:
    dev_login = os.getenv("ENABLE_DEV_LOGIN", "false").lower() == "true"
    return {
        "status": "ok",
        "configured": bool(OPENAI_API_KEY),
        "openai_configured": bool(OPENAI_API_KEY),
        "wechat_configured": bool(os.getenv("WX_APP_ID", "").strip() and os.getenv("WX_APP_SECRET", "").strip()),
        "content_security_configured": bool(os.getenv("WX_APP_ID", "").strip() and os.getenv("WX_APP_SECRET", "").strip()),
        "ad_configured": bool(os.getenv("WX_AD_UNIT_ID", "").strip()),
        "admin_configured": bool(os.getenv("ADMIN_PASSWORD_HASH", "").strip()),
        "dev_login": dev_login,
        "video_enabled": subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0,
    }


@app.get("/", response_class=FileResponse)
async def index() -> Path:
    return STATIC_DIR / "index.html"


@app.get("/admin", response_class=FileResponse)
async def admin_index() -> Path:
    return ADMIN_STATIC_DIR / "index.html"


@app.get("/api/history", response_model=list[HistoryItem])
async def history() -> list[HistoryItem]:
    if not ENABLE_LEGACY_WEB_API:
        raise HTTPException(status_code=410, detail="Web 分析接口已停用，请使用微信小程序")
    with get_db() as db:
        rows = db.execute("SELECT id, created_at, mode, filename, subject, confidence FROM analyses ORDER BY id DESC LIMIT 50").fetchall()
    return [HistoryItem.model_validate(dict(row)) for row in rows]


@app.get("/api/history/{analysis_id}", response_model=AnalysisResponse)
async def history_detail(analysis_id: int) -> AnalysisResponse:
    if not ENABLE_LEGACY_WEB_API:
        raise HTTPException(status_code=410, detail="Web 分析接口已停用，请使用微信小程序")
    with get_db() as db:
        row = db.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这条分析记录")
    payload = json.loads(row["payload"])
    return AnalysisResponse(id=row["id"], created_at=row["created_at"], mode=row["mode"], source="history", filename=row["filename"], analysis=payload["analysis"], prompts=payload["prompts"], note="已从服务器历史记录恢复")


async def analyze_media_upload(file: UploadFile, mode: str, check_content_security: bool = False, analysis_depth: str = "detailed", analysis_task: str = "reconstruct") -> tuple[VisualAnalysis, PromptBundle]:
    if mode not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="不支持的媒体类型")
    if analysis_depth not in {"standard", "detailed", "professional"}:
        raise HTTPException(status_code=400, detail="不支持的反推维度")
    if analysis_task not in {"reconstruct", "image_expand_video"}:
        raise HTTPException(status_code=400, detail="不支持的图片任务类型")
    if analysis_task == "image_expand_video" and mode != "image":
        raise HTTPException(status_code=400, detail="画面拓展仅支持图片")
    allowed_types = IMAGE_TYPES if mode == "image" else VIDEO_TYPES
    max_bytes = MAX_IMAGE_BYTES if mode == "image" else MAX_VIDEO_BYTES
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="文件类型与当前模式不匹配")
    if mode == "image":
        content = await file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"文件不能超过 {max_bytes // (1024 * 1024)}MB")
        if not content:
            raise HTTPException(status_code=400, detail="上传文件为空")
        images = [(content, file.content_type, file.filename or "image")]
        if check_content_security:
            await check_images(images)
        analysis = await call_vision(images, analysis_depth, analysis_task)
    else:
        suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"文件不能超过 {max_bytes // (1024 * 1024)}MB")
                temp.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400, detail="上传文件为空")
            temp.flush()
            duration = get_video_duration(temp.name)
            frames = video_frames(temp.name, duration)
            if check_content_security:
                await check_images(frames)
            analysis = await call_vision(frames, analysis_depth, "video_reconstruct")
    prompts = make_prompts(analysis, video_prompt=mode == "video" or analysis_task == "image_expand_video")
    return analysis, prompts


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze(file: UploadFile = File(...), mode: str = Form("image"), model: str = Form("universal")) -> AnalysisResponse:
    if not ENABLE_LEGACY_WEB_API:
        raise HTTPException(status_code=410, detail="Web 分析接口已停用，请使用微信小程序")
    analysis, prompts = await analyze_media_upload(file, mode)
    analysis_id = save_analysis(mode, file.filename or "untitled", analysis, prompts)
    with get_db() as db:
        row = db.execute("SELECT created_at FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return AnalysisResponse(id=analysis_id, created_at=row["created_at"], mode=mode, source="live", filename=file.filename or "untitled", analysis=analysis, prompts=prompts, note="已完成真实媒体分析，结果已保存到服务器历史记录。")


from app.commercial import commercial_router, recover_interrupted_jobs  # noqa: E402
from app.admin import admin_router  # noqa: E402

app.include_router(commercial_router)
app.include_router(admin_router)
recover_interrupted_jobs()
