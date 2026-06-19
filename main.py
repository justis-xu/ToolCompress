from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_CONTENT_LEN   = int(os.getenv("MAX_CONTENT_LEN", str(500_000)))
MAX_CONTEXT_LEN   = int(os.getenv("MAX_CONTEXT_LEN", "2000"))
MAX_BATCH         = int(os.getenv("MAX_BATCH", "32"))
DEFAULT_IMAGE_DIM     = int(os.getenv("MAX_IMAGE_DIM", "512"))
DEFAULT_IMAGE_QUALITY = int(os.getenv("IMAGE_QUALITY", "60"))
FULL_LOW_IMAGE_DIM    = int(os.getenv("FULL_LOW_IMAGE_DIM", "512"))
FULL_LOW_IMAGE_QUALITY = int(os.getenv("FULL_LOW_IMAGE_QUALITY", "60"))
SLOW_MS           = int(os.getenv("SLOW_MS", "500"))
IMAGE_CONCURRENCY = int(os.getenv("IMAGE_CONCURRENCY", "2"))

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]+")


def _cjk_bigrams(text: str) -> str:
    def _expand(m: re.Match) -> str:
        s = m.group()
        parts: list[str] = []
        for i, ch in enumerate(s):
            parts.append(ch)
            if i + 1 < len(s):
                parts.append(s[i] + s[i + 1])
        return " ".join(parts)
    return _CJK_RE.sub(_expand, text)


def _estimate_tokens(width: int, height: int) -> int:
    tiles_x = (width + 511) // 512
    tiles_y = (height + 511) // 512
    return 170 + 85 * tiles_x * tiles_y


_NON_WHITESPACE_WORD_BOUNDARY_RE = re.compile(r"\\n|[,:;]")


def _count_text_tokens(text: str) -> int:
    """
    CJK-aware token估算：一次正则替换，不逐字符遍历。
    """
    cjk_chars = sum(len(m) for m in _CJK_RE.findall(text))
    normalized = _NON_WHITESPACE_WORD_BOUNDARY_RE.sub(" ", _CJK_RE.sub(" ", text))
    non_cjk_words = len(normalized.split())
    return max(1, non_cjk_words + int(cjk_chars * 0.6))


_router = None
_image_sem: anyio.Semaphore | None = None

def _patch_is_mixed_content() -> None:
    """
    阻止 ContentRouter 把真实内容误判成 'mixed'——一旦判成 mixed，内容会
    几乎原样透传，而不会真正走到对应的专用压缩器
    (HTMLExtractor / LogCompressor / DiffCompressor)。
    """
    import headroom.transforms.content_router as _cr
    from headroom.transforms.content_detector import (
        _try_detect_html,
        _try_detect_diff,
        _try_detect_log,
        _try_detect_search,
    )

    # (检测器, 信得过它、可以覆盖mixed判断所需的最低置信度)
    _DETECTORS = [
        (_try_detect_html, 0.7),
        (_try_detect_diff, 0.7),
        (_try_detect_log, 0.5),
        (_try_detect_search, 0.6),
    ]

    def _confident_specific_type(content: str) -> Any | None:
        for detect, min_confidence in _DETECTORS:
            result = detect(content)
            if result is not None and result.confidence >= min_confidence:
                return result
        return None

    original_is_mixed = _cr.is_mixed_content

    def patched_is_mixed(content: str) -> bool:
        if _confident_specific_type(content) is not None:
            return False
        return original_is_mixed(content)

    _cr.is_mixed_content = patched_is_mixed

    _OVERRIDABLE = {_cr.ContentType.SEARCH_RESULTS, _cr.ContentType.PLAIN_TEXT}
    original_detect = _cr._detect_content

    def patched_detect(content: str) -> Any:
        result = original_detect(content)
        if result.content_type == _cr.ContentType.SOURCE_CODE:
            search_result = _try_detect_search(content)
            if search_result is not None and search_result.confidence >= 0.9:
                return search_result
            return result
        if result.content_type in _OVERRIDABLE:
            specific = _confident_specific_type(content)
            if specific is not None and specific.content_type != result.content_type:
                return specific
        return result

    _cr._detect_content = patched_detect


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _router, _image_sem
    from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
    _patch_is_mixed_content()
    logger.info("initializing ContentRouter")
    _router = ContentRouter(ContentRouterConfig(
        enable_kompress=False,
        enable_code_aware=True,
        prefer_code_aware_for_code=True,
    ))
    _router.compress('{"warmup":[1,2,3]}', context="warmup")
    _image_sem = anyio.Semaphore(IMAGE_CONCURRENCY)
    logger.info("ContentRouter ready  IMAGE_CONCURRENCY=%d SLOW_MS=%d", IMAGE_CONCURRENCY, SLOW_MS)
    yield


app = FastAPI(title="tool-compress", lifespan=lifespan)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    level = logging.WARNING if ms > SLOW_MS else logging.INFO
    logger.log(level, "%s %s %d %.0fms", request.method, request.url.path, response.status_code, ms)
    return response


# ── Text models ───────────────────────────────────────────────────────────────

class TextCompressRequest(BaseModel):
    content: str = Field(..., max_length=MAX_CONTENT_LEN)
    context: str = Field(default="", max_length=MAX_CONTEXT_LEN)


class TextCompressResult(BaseModel):
    compressed: str
    strategy: str
    original_chars: int
    compressed_chars: int
    original_tokens: int
    compressed_tokens: int
    ratio: float


class TextBatchRequest(BaseModel):
    items: list[TextCompressRequest] = Field(..., min_length=1, max_length=MAX_BATCH)


class TextBatchResponse(BaseModel):
    results: list[TextCompressResult]


# ── Image models ──────────────────────────────────────────────────────────────

class ImageCompressRequest(BaseModel):
    image: str         = Field(..., description="Base64-encoded image (with or without data: prefix)")
    mode: Literal["preserve", "full_low"] = Field(default="full_low")
    max_dimension: int = Field(default=DEFAULT_IMAGE_DIM, ge=64, le=2048)
    quality: int       = Field(default=DEFAULT_IMAGE_QUALITY, ge=10, le=95)


class ImageCompressResult(BaseModel):
    mode: str
    compressed: str
    media_type: str
    original_size: int
    compressed_size: int
    ratio: float
    original_tokens: int
    compressed_tokens: int


class ImageBatchRequest(BaseModel):
    items: list[ImageCompressRequest] = Field(..., min_length=1, max_length=MAX_BATCH)


class ImageBatchResponse(BaseModel):
    results: list[ImageCompressResult]


# ── Internal helpers ──────────────────────────────────────────────────────────

_OPAQUE_BYTE_LIMIT = 250  # SmartCrusher opaque-blob triggers at >256B; stay safely below
_TRUNCATE_CHARS    = 80   # 80 CJK chars = 240B < 256B; enough context for LLM


def _truncate_json_strings(content: str) -> str:
    """Truncate string values >_OPAQUE_BYTE_LIMIT bytes in JSON arrays/objects.

    Prevents SmartCrusher from emitting unresolvable <<ccr:HASH>> markers
    (headroom#1091) for long string cells. Non-JSON input is returned unchanged.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return content

    def _truncate_dict(d: dict) -> bool:
        changed = False
        for k, v in d.items():
            if isinstance(v, str) and len(v.encode()) > _OPAQUE_BYTE_LIMIT:
                d[k] = v[:_TRUNCATE_CHARS] + "…"
                changed = True
        return changed

    changed = False
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                changed |= _truncate_dict(row)
    elif isinstance(data, dict):
        changed = _truncate_dict(data)

    return json.dumps(data, ensure_ascii=False) if changed else content


def _compress_text_sync(content: str, context: str) -> TextCompressResult:
    t0 = time.perf_counter()
    result = _router.compress(_truncate_json_strings(content), context=_cjk_bigrams(context))
    ms = (time.perf_counter() - t0) * 1000

    # Fallback: if CCR markers still appear (headroom#1091 — opaque-blob path
    # not gated by enable_ccr_marker), pass through original to avoid sending
    # unresolvable <<ccr:HASH>> placeholders to the LLM.
    compressed = result.compressed
    strategy   = result.strategy_used.value
    if "<<ccr:" in compressed:
        logger.error(
            "ccr_marker_fallback strategy=%s orig=%dch — upstream bug headroom#1091, passing through",
            strategy, len(content),
        )
        compressed = content
        strategy   = "passthrough"

    original_tokens   = _count_text_tokens(content)
    compressed_tokens = _count_text_tokens(compressed)
    ratio = round(compressed_tokens / max(1, original_tokens), 4)
    logger.info(
        "text strategy=%s ratio=%.2f orig=%dch comp=%dch ctx=%d %.0fms",
        strategy, ratio,
        len(content), len(compressed), len(context), ms,
    )
    return TextCompressResult(
        compressed=compressed,
        strategy=strategy,
        original_chars=len(content),
        compressed_chars=len(compressed),
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
        ratio=ratio,
    )


def _decode_image(image_b64: str) -> bytes:
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    try:
        return base64.b64decode(image_b64, validate=True)
    except Exception as e:
        raise ValueError(f"invalid base64: {e}") from e


def _compress_image_sync(image_b64: str, mode: str, max_dimension: int, quality: int) -> ImageCompressResult:
    t0 = time.perf_counter()
    raw = _decode_image(image_b64)
    try:
        img = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError as e:
        raise ValueError(f"unrecognized image format: {e}") from e

    orig_w, orig_h = img.size
    original_tokens = _estimate_tokens(orig_w, orig_h)
    if mode == "full_low":
        max_dimension = min(max_dimension, FULL_LOW_IMAGE_DIM)
        quality = min(quality, FULL_LOW_IMAGE_QUALITY)

    if orig_w > max_dimension or orig_h > max_dimension:
        scale = max_dimension / max(orig_w, orig_h)
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    else:
        new_w, new_h = orig_w, orig_h

    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    compressed_bytes = buf.getvalue()
    compressed_tokens = _estimate_tokens(new_w, new_h)
    ms = (time.perf_counter() - t0) * 1000
    ratio = round(len(compressed_bytes) / max(1, len(raw)), 4)
    logger.info(
        "image mode=%s %dx%d→%dx%d q=%d ratio=%.2f orig=%dB comp=%dB tokens=%d→%d %.0fms",
        mode, orig_w, orig_h, new_w, new_h, quality, ratio,
        len(raw), len(compressed_bytes),
        original_tokens, compressed_tokens, ms,
    )
    return ImageCompressResult(
        mode=mode,
        compressed=base64.b64encode(compressed_bytes).decode(),
        media_type="image/jpeg",
        original_size=len(raw),
        compressed_size=len(compressed_bytes),
        ratio=ratio,
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    if _router is None:
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ok"}


@app.post("/compress", response_model=TextCompressResult)
async def compress(req: TextCompressRequest):
    if _router is None:
        raise HTTPException(status_code=503, detail="not ready")
    try:
        return await anyio.to_thread.run_sync(
            lambda: _compress_text_sync(req.content, req.context), cancellable=True
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("text compress failed content_len=%d", len(req.content))
        raise HTTPException(status_code=500, detail="compression failed")


@app.post("/compress/batch", response_model=TextBatchResponse)
async def compress_batch(req: TextBatchRequest):
    if _router is None:
        raise HTTPException(status_code=503, detail="not ready")
    try:
        results = await anyio.to_thread.run_sync(
            lambda: [_compress_text_sync(i.content, i.context) for i in req.items], cancellable=True
        )
        total_orig = sum(r.original_tokens for r in results)
        total_comp = sum(r.compressed_tokens for r in results)
        logger.info("batch n=%d tokens=%d→%d ratio=%.2f",
                    len(results), total_orig, total_comp,
                    total_comp / max(1, total_orig))
        return TextBatchResponse(results=results)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("batch compress failed n=%d", len(req.items))
        raise HTTPException(status_code=500, detail="compression failed")


@app.post("/compress/image", response_model=ImageCompressResult)
async def compress_image(req: ImageCompressRequest):
    try:
        async with _image_sem:
            return await anyio.to_thread.run_sync(
                lambda: _compress_image_sync(req.image, req.mode, req.max_dimension, req.quality), cancellable=True
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("image compress failed")
        raise HTTPException(status_code=500, detail="image compression failed")


@app.post("/compress/image/batch", response_model=ImageBatchResponse)
async def compress_image_batch(req: ImageBatchRequest):
    async def _one(item: ImageCompressRequest) -> ImageCompressResult:
        async with _image_sem:
            return await anyio.to_thread.run_sync(
                lambda: _compress_image_sync(item.image, item.mode, item.max_dimension, item.quality), cancellable=True
            )

    try:
        async with anyio.create_task_group() as tg:
            results: list[ImageCompressResult | None] = [None] * len(req.items)

            async def _run(idx: int, item: ImageCompressRequest) -> None:
                results[idx] = await _one(item)

            for i, item in enumerate(req.items):
                tg.start_soon(_run, i, item)

        logger.info("image batch n=%d", len(results))
        return ImageBatchResponse(results=results)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("image batch compress failed n=%d", len(req.items))
        raise HTTPException(status_code=500, detail="image compression failed")
