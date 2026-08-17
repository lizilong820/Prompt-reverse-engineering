from __future__ import annotations

import base64
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
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("prompt-lens")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "prompt-lens.sqlite3"
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_MB", "12")) * 1024 * 1024
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_MB", "180")) * 1024 * 1024
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "90"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
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


class PromptBundle(BaseModel):
    universal: str
    midjourney: str
    flux: str
    video: str


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
        "timeline": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}, "camera_motion": {"type": "string"}, "subject_motion": {"type": "string"}},
            "required": ["start", "end", "description", "camera_motion", "subject_motion"]}},
    },
    "required": ["subject", "scene", "composition", "camera", "lighting", "color", "style", "details", "negative_prompt", "confidence", "timeline"],
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


def make_prompts(analysis: VisualAnalysis) -> PromptBundle:
    details = ", ".join(analysis.details) or "highly resolved natural textures"
    negative = ", ".join(analysis.negative_prompt) or "text, logo, watermark, artifacts"
    universal = f"{analysis.subject}. {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. {analysis.color}. {analysis.style}. Details: {details}. Clean frame, coherent subject and environment."
    midjourney = f"{analysis.subject}, {analysis.scene}, {analysis.composition}, {analysis.camera}, {analysis.lighting}, {analysis.style}, {details} --ar 4:5 --stylize 180 --no {negative}"
    flux = f"A cinematic editorial image of {analysis.subject}, in {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. Color palette: {analysis.color}. Style: {analysis.style}. Details: {details}. Avoid {negative}."
    timeline = " ".join(f"{item.start}-{item.end}: {item.description}; camera: {item.camera_motion}; subject: {item.subject_motion}." for item in analysis.timeline)
    video = f"{analysis.subject}. {analysis.scene}. {analysis.composition}. {analysis.camera}. {analysis.lighting}. Preserve identity, wardrobe and spatial continuity. {timeline} 24fps, realistic motion blur, cinematic pacing. Avoid {negative}."
    return PromptBundle(universal=universal, midjourney=midjourney, flux=flux, video=video)


def as_data_url(content: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"


def parse_vision_payload(payload: dict[str, Any]) -> VisualAnalysis:
    try:
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content)
        return VisualAnalysis.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Vision API 返回了无法解析的结构化结果") from exc


async def call_vision(images: list[tuple[bytes, str, str]]) -> VisualAnalysis:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="服务尚未配置 OPENAI_API_KEY，无法进行真实分析")
    content: list[dict[str, Any]] = [{"type": "text", "text": "你是视觉分析专家。只返回符合 JSON Schema 的 JSON，使用简洁中文，描述可见事实，不猜测品牌和身份。若输入是视频关键帧，请填写 timeline 并描述镜头和主体运动；图片的 timeline 返回空数组。"}]
    for data, content_type, label in images:
        content.append({"type": "text", "text": f"参考帧：{label}"})
        content.append({"type": "image_url", "image_url": {"url": as_data_url(data, content_type)}})
    body = {"model": OPENAI_MODEL, "temperature": 0.15, "response_format": {"type": "json_schema", "json_schema": {"name": "visual_analysis", "strict": True, "schema": ANALYSIS_SCHEMA}}, "messages": [{"role": "user", "content": content}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            response = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="无法连接 Vision API") from exc
    if response.is_error:
        logger.error("Vision API %s: %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=502, detail="Vision API 请求失败，请检查模型、额度和 API key")
    return parse_vision_payload(response.json())


def run_command(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail="服务器缺少视频处理依赖 ffmpeg/ffprobe") from exc


def video_frames(video_path: str, duration: float) -> list[tuple[bytes, str, str]]:
    timestamps = [0.0, duration * .2, duration * .4, duration * .6, duration * .8, max(0.0, duration - .1)]
    frames: list[tuple[bytes, str, str]] = []
    for index, timestamp in enumerate(dict.fromkeys(round(value, 2) for value in timestamps), start=1):
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp), "-i", video_path, "-frames:v", "1", "-vf", "scale=1280:-2", "-f", "image2", "pipe:1"]
        try:
            result = subprocess.run(command, capture_output=True, check=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=500, detail="视频关键帧提取失败") from exc
        frames.append((result.stdout, "image/jpeg", f"{timestamp:.2f}s / frame {index}"))
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


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "configured": bool(OPENAI_API_KEY), "video_enabled": subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0}


@app.get("/", response_class=FileResponse)
async def index() -> Path:
    return STATIC_DIR / "index.html"


@app.get("/api/history", response_model=list[HistoryItem])
async def history() -> list[HistoryItem]:
    with get_db() as db:
        rows = db.execute("SELECT id, created_at, mode, filename, subject, confidence FROM analyses ORDER BY id DESC LIMIT 50").fetchall()
    return [HistoryItem.model_validate(dict(row)) for row in rows]


@app.get("/api/history/{analysis_id}", response_model=AnalysisResponse)
async def history_detail(analysis_id: int) -> AnalysisResponse:
    with get_db() as db:
        row = db.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这条分析记录")
    payload = json.loads(row["payload"])
    return AnalysisResponse(id=row["id"], created_at=row["created_at"], mode=row["mode"], source="history", filename=row["filename"], analysis=payload["analysis"], prompts=payload["prompts"], note="已从服务器历史记录恢复")


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze(file: UploadFile = File(...), mode: str = Form("image"), model: str = Form("universal")) -> AnalysisResponse:
    if mode not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="不支持的媒体类型")
    allowed_types = IMAGE_TYPES if mode == "image" else VIDEO_TYPES
    max_bytes = MAX_IMAGE_BYTES if mode == "image" else MAX_VIDEO_BYTES
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="文件类型与当前模式不匹配")
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件不能超过 {max_bytes // (1024 * 1024)}MB")
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if mode == "image":
        analysis = await call_vision([(content, file.content_type, file.filename or "image")])
    else:
        suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
            temp.write(content)
            temp.flush()
            duration = get_video_duration(temp.name)
            analysis = await call_vision(video_frames(temp.name, duration))
    prompts = make_prompts(analysis)
    analysis_id = save_analysis(mode, file.filename or "untitled", analysis, prompts)
    with get_db() as db:
        row = db.execute("SELECT created_at FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return AnalysisResponse(id=analysis_id, created_at=row["created_at"], mode=mode, source="live", filename=file.filename or "untitled", analysis=analysis, prompts=prompts, note="已完成真实媒体分析，结果已保存到服务器历史记录。")
