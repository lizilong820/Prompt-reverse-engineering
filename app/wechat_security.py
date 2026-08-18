from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from typing import Iterable

import httpx
from fastapi import HTTPException
from PIL import Image, ImageOps

logger = logging.getLogger("prompt-lens.wechat-security")

WX_APP_ID = os.getenv("WX_APP_ID", "").strip()
WX_APP_SECRET = os.getenv("WX_APP_SECRET", "").strip()
WECHAT_SECURITY_URL = "https://api.weixin.qq.com"
MAX_CHECK_BYTES = 1024 * 1024
_access_token = ""
_access_token_expires_at = 0.0
_token_lock = asyncio.Lock()


def _prepare_image(content: bytes) -> bytes:
    """Convert uploads to a small JPEG accepted by img_sec_check."""
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            for quality in (88, 80, 72, 64, 56):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                data = output.getvalue()
                if len(data) <= MAX_CHECK_BYTES:
                    return data
            # Highly detailed images can still exceed 1 MB after compression.
            while len(data) > MAX_CHECK_BYTES and min(image.size) > 256:
                image = image.resize((max(256, int(image.width * 0.8)), max(256, int(image.height * 0.8))), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=56, optimize=True)
                data = output.getvalue()
            return data
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=415, detail="无法读取上传图片") from exc


async def _get_access_token(force_refresh: bool = False) -> str:
    global _access_token, _access_token_expires_at
    if not WX_APP_ID or not WX_APP_SECRET:
        raise HTTPException(status_code=503, detail="微信内容安全服务尚未配置")
    if not force_refresh and _access_token and time.time() < _access_token_expires_at:
        return _access_token
    async with _token_lock:
        if not force_refresh and _access_token and time.time() < _access_token_expires_at:
            return _access_token
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.get(
                    f"{WECHAT_SECURITY_URL}/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": WX_APP_ID, "secret": WX_APP_SECRET},
                )
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="内容安全服务暂时不可用，请稍后重试") from exc
        if response.is_error or payload.get("errcode") or not payload.get("access_token"):
            logger.error("WeChat access token failed: status=%s errcode=%s errmsg=%s", response.status_code, payload.get("errcode"), payload.get("errmsg"))
            raise HTTPException(status_code=503, detail="内容安全服务暂时不可用，请稍后重试")
        _access_token = payload["access_token"]
        _access_token_expires_at = time.time() + max(60, int(payload.get("expires_in", 7200)) - 300)
        return _access_token


async def check_images(images: Iterable[tuple[bytes, str, str]]) -> None:
    """Fail closed: every uploaded image/frame must pass WeChat moderation."""
    prepared = [(data, label) for data, _, label in images]
    if not prepared:
        raise HTTPException(status_code=422, detail="无法提取可检测的媒体内容")
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
        for content, label in prepared:
            for token_attempt in range(2):
                try:
                    response = await client.post(
                        f"{WECHAT_SECURITY_URL}/wxa/img_sec_check",
                        params={"access_token": token},
                        files={"media": ("upload.jpg", _prepare_image(content), "image/jpeg")},
                    )
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.exception("WeChat image security request failed: label=%s", label)
                    raise HTTPException(status_code=503, detail="内容安全服务暂时不可用，请稍后重试") from exc
                errcode = int(payload.get("errcode", -1))
                if errcode == 0:
                    break
                if errcode in {40001, 40014, 42001} and token_attempt == 0:
                    token = await _get_access_token(force_refresh=True)
                    continue
                logger.warning("WeChat image security rejected/failed: label=%s status=%s errcode=%s errmsg=%s", label, response.status_code, errcode, payload.get("errmsg"))
                if errcode == 87014:
                    raise HTTPException(status_code=422, detail="上传内容含违规信息")
                raise HTTPException(status_code=503, detail="内容安全服务暂时不可用，请稍后重试")
            else:
                raise HTTPException(status_code=503, detail="内容安全服务暂时不可用，请稍后重试")

