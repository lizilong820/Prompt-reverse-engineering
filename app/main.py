from __future__ import annotations

import base64
import json
import logging
import os
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
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "12")) * 1024 * 1024
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


class VisualAnalysis(BaseModel):
    subject: str = ""
    scene: str = ""
    composition: str = ""
    camera: str = ""
    lighting: str = ""
    color: str = ""
    style: str = ""
    details: list[str] = Field(default_factory=list)
    negative_prompt: list[str] = Field(default_factory=list)
    confidence: int = Field(default=78, ge=0, le=100)


class PromptBundle(BaseModel):
    universal: str
    midjourney: str
    flux: str
    video: str


class AnalysisResponse(BaseModel):
    mode: str
    source: str
    analysis: VisualAnalysis
    prompts: PromptBundle
    note: str


DEMO_ANALYSIS = VisualAnalysis(
    subject="一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然",
    scene="现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑",
    composition="4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线",
    camera="50mm 定焦镜头，浅景深，主体清晰，背景城市灯光自然散景",
    lighting="冷色窗光从侧面进入，室内暖色实用灯勾勒轮廓，低饱和电影感",
    color="青蓝城市光、深红大衣、炭灰室内，冷暖对比克制",
    style="高端电影感生活方式摄影，真实皮肤质感，轻微胶片颗粒",
    details=["雨滴附着在玻璃表面", "羊毛大衣织物纹理清晰", "背景灯光有柔和光晕", "画面留有呼吸感"],
    negative_prompt=["文字", "logo", "水印", "过度磨皮", "畸形手部", "过饱和", "杂乱背景"],
    confidence=86,
)


def make_prompts(analysis: VisualAnalysis, mode: str = "image") -> PromptBundle:
    detail_line = ", ".join(analysis.details)
    negative_line = ", ".join(analysis.negative_prompt)
    universal = (
        f"{analysis.subject}. {analysis.scene}. {analysis.composition}. "
        f"{analysis.camera}. {analysis.lighting}. {analysis.color}. {analysis.style}. "
        f"细节：{detail_line}。高真实度，画面干净，主体和环境关系自然。"
    )
    midjourney = (
        f"{analysis.subject}, {analysis.scene}, {analysis.composition}, {analysis.camera}, "
        f"{analysis.lighting}, {analysis.style}, {detail_line}, editorial cinematic photography "
        f"--ar 4:5 --stylize 180 --no {negative_line}"
    )
    flux = (
        f"A cinematic editorial photograph of {analysis.subject}, set in {analysis.scene}. "
        f"{analysis.composition}. {analysis.camera}. {analysis.lighting}. "
        f"Color palette: {analysis.color}. Style: {analysis.style}. "
        f"Important details: {detail_line}. Avoid {negative_line}."
    )
    video = (
        f"{analysis.subject}, {analysis.scene}. Start with {analysis.composition}; "
        "the camera makes a very slow push-in while the subject remains natural and still. "
        f"Keep {analysis.lighting} and {analysis.color}; preserve identity, clothing and background continuity. "
        "Subtle rain movement on the glass, realistic motion blur, 24fps, cinematic pacing. "
        f"Avoid {negative_line}."
    )
    return PromptBundle(universal=universal, midjourney=midjourney, flux=flux, video=video)


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "scene": {"type": "string"},
        "composition": {"type": "string"},
        "camera": {"type": "string"},
        "lighting": {"type": "string"},
        "color": {"type": "string"},
        "style": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}},
        "negative_prompt": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "subject",
        "scene",
        "composition",
        "camera",
        "lighting",
        "color",
        "style",
        "details",
        "negative_prompt",
        "confidence",
    ],
}


def _data_url(content: bytes, content_type: str) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _extract_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    raise ValueError("Vision API returned an empty response")


async def _analyze_with_api(content: bytes, content_type: str) -> VisualAnalysis:
    prompt = (
        "Analyze this reference image for a prompt reconstruction tool. Return only JSON matching the schema. "
        "Describe visible facts, avoid inventing brands or identities, and use concise Chinese. "
        "The negative_prompt list should contain likely generation artifacts to avoid."
    )
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "visual_analysis", "strict": True, "schema": ANALYSIS_SCHEMA},
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(content, content_type)}},
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=10.0)) as client:
        response = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=body)
    if response.is_error:
        logger.error("Vision API error %s: %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=502, detail="Vision API 请求失败，请检查模型配置或稍后重试")
    try:
        return VisualAnalysis.model_validate(json.loads(_extract_response_text(response.json())))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.exception("Invalid Vision API response")
        raise HTTPException(status_code=502, detail="Vision API 返回格式不可解析") from exc


app = FastAPI(title="Prompt Lens API", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "live" if OPENAI_API_KEY else "demo"}


@app.get("/", response_class=FileResponse)
async def index() -> Path:
    return STATIC_DIR / "index.html"


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze(file: UploadFile = File(...), mode: str = Form("image"), model: str = Form("universal")) -> AnalysisResponse:
    if mode not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="不支持的媒体类型")
    if mode == "video":
        raise HTTPException(status_code=501, detail="视频分镜分析即将开放，当前版本先支持图片反推")
    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG、WEBP 或 GIF 图片")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"图片不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    source = "live" if OPENAI_API_KEY else "demo"
    analysis = await _analyze_with_api(content, file.content_type) if OPENAI_API_KEY else DEMO_ANALYSIS
    return AnalysisResponse(
        mode=mode,
        source=source,
        analysis=analysis,
        prompts=make_prompts(analysis, mode),
        note="已按视觉事实重建，可继续编辑后用于生成。" if source == "live" else "当前为 Demo 分析。配置 OPENAI_API_KEY 后将调用真实 Vision API。",
    )
