from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_CONTENT_LEN   = int(os.getenv("MAX_CONTENT_LEN", str(500_000)))
MAX_CONTEXT_LEN   = int(os.getenv("MAX_CONTEXT_LEN", "2000"))
MAX_BATCH         = int(os.getenv("MAX_BATCH", "32"))
DEFAULT_IMAGE_DIM     = int(os.getenv("MAX_IMAGE_DIM", "768"))
DEFAULT_IMAGE_QUALITY = int(os.getenv("IMAGE_QUALITY", "85"))
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
    """CJK-aware token估算：一次正则替换，不逐字符遍历。

    SmartCrusher对大批量同构JSON数组会输出"压缩表格"格式——行与行之间用的是
    字面意义上的"\\n"两个字符（反斜杠+n），不是真正的换行符，逗号/冒号分隔
    字段也不是空白字符。原来只按真实空白字符split()，这种几乎没有空格的
    blob会被整段当成1个词，导致compressed_tokens严重低估、ratio虚低（实测：
    100条同构JSON记录，真实token数约512，旧逻辑算出来是1）。这里把字面\\n
    和逗号/冒号/分号也当成分词边界，跟真实空白字符一起处理。
    """
    cjk_chars = sum(len(m) for m in _CJK_RE.findall(text))
    normalized = _NON_WHITESPACE_WORD_BOUNDARY_RE.sub(" ", _CJK_RE.sub(" ", text))
    non_cjk_words = len(normalized.split())
    return max(1, non_cjk_words + int(cjk_chars * 0.6))


_router = None
_image_sem: anyio.Semaphore | None = None

def _patch_is_mixed_content() -> None:
    r"""阻止 ContentRouter 把真实内容误判成 'mixed'——一旦判成 mixed，内容会
    几乎原样透传，而不会真正走到对应的专用压缩器
    (HTMLExtractor / LogCompressor / DiffCompressor)。

    headroom 的 is_mixed_content() 只要命中下面4个粗糙正则指标里的2个，就判
    mixed：{代码块```、以'{'/'['开头的行、大写开头的"散文"模式、grep风格的
    "file:line:"结果}。这是一类*通用*的误判，不是只对某一种内容类型，做这
    次评测的过程中独立发现了3次：

    - 整页HTML：内嵌的<script>{...}</script> JSON-LD（命中json_blocks）+
      任意一段正文文字（命中prose）。Scrapinghub benchmark实测：5个样本里
      4个落到strategy=mixed，ratio≈1.0（HTMLExtractor根本没跑）。
    - 真实日志文件：_SEARCH_RESULT_PATTERN（r"^\S+:\d+:"，本意是抓grep的
      "file:line:"格式）误判普通时间戳，比如"...21:27:09"（\S+先吞掉文件名
      /日期前缀，然后":27:"刚好满足":\d+:"）+ prose（随便一句日志描述都会
      命中）。LogHub的OpenStack样本实测：该保留的WARNING/Exception行在
      mixed策略下recall=0%（ratio≈0.014）。
    - 真实git diff：commit message里的正文文字 + hunk里以`{`/`}`开头的代码
      行（命中json_blocks）。从这个仓库自己的git log里抽3个真实commit测，
      2/3落到strategy=mixed，ratio≈1.0（DiffCompressor根本没跑）。

    修复思路：不再一种内容类型打一个补丁，而是先依次问所有专用检测器
    （html/diff/log/search）；只要有一个给出高置信度，那就是一个真实、具
    体的类型——绝不让粗糙的mixed启发式覆盖一个有把握的具体判断。这个修复
    同时也覆盖了_detect_content()本身——它在Linux上默认走Rust核心后端（不
    是headroom纯Python的content_detector模块），而Rust后端对日志时间戳有
    完全一样的SEARCH_RESULTS误判，跟用哪个后端无关。

    这里直接打补丁（不改headroom上游源码），因为ToolCompress用pip锁定了
    headroom-ai==0.26.0这个版本；改这个仓库的源码不会影响已经装好的那个包，
    除非等它发新版本。
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

    # is_mixed_content这一关过了之后，还要防一手_detect_content()本身（Linux
    # 上默认是Rust核心后端）独立产生的误判。只重新判定两种"弱"结果
    # （SEARCH_RESULTS，跟上面同一个误判正则；PLAIN_TEXT，兜底分类）——绝不
    # 覆盖一个已经是具体类型的结果（比如JSON_ARRAY），避免好心办坏事，引入
    # 新的误判。
    #
    # 有一个例外：SOURCE_CODE。对一个真实代码库跑grep/rg，每一行内容本身就
    # 是看起来合法的代码（"file.py:42:import json"），所以Rust会很confident
    # 地判成source_code（实测验证过：在容器里对一段真实`grep -rn import`的
    # 输出，confidence=1.0）——但headroom自己的CodeAwareCompressor没法对一
    # 个每行都粘着"file:line:"前缀的内容块做AST解析，会解析失败回退成原样
    # 透传（ratio=1.0）；而专门做这件事的SearchCompressor就是为了处理这种
    # 格式设计的（它自己的测试套件里断言的例子就是
    # "src/main.py:42:def process_data(items):"）。Python版本search检测器
    # 的正则（行首r"^\S+:\d+:"，且要求30%以上的行命中）本身就很窄，误报率
    # 低，所以置信度足够高时可以放心拿它去覆盖source_code的判断——这里用了
    # 比下面通用阈值(0.6)更严格的门槛(0.9)，因为这是唯一一处要去覆盖"非弱"
    # 分类结果的情况，要格外小心。
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
    max_dimension: int = Field(default=DEFAULT_IMAGE_DIM, ge=64, le=2048)
    quality: int       = Field(default=DEFAULT_IMAGE_QUALITY, ge=10, le=95)


class ImageCompressResult(BaseModel):
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


def _compress_image_sync(image_b64: str, max_dimension: int, quality: int) -> ImageCompressResult:
    t0 = time.perf_counter()
    raw = _decode_image(image_b64)
    try:
        img = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError as e:
        raise ValueError(f"unrecognized image format: {e}") from e

    orig_w, orig_h = img.size
    original_tokens = _estimate_tokens(orig_w, orig_h)

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
        "image %dx%d→%dx%d ratio=%.2f orig=%dB comp=%dB tokens=%d→%d %.0fms",
        orig_w, orig_h, new_w, new_h, ratio,
        len(raw), len(compressed_bytes),
        original_tokens, compressed_tokens, ms,
    )
    return ImageCompressResult(
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
                lambda: _compress_image_sync(req.image, req.max_dimension, req.quality), cancellable=True
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
                lambda: _compress_image_sync(item.image, item.max_dimension, item.quality), cancellable=True
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
