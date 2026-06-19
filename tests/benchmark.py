#!/usr/bin/env python3
"""Benchmark tool-compress service.

模式：
  python benchmark.py                          # 性能测试（HTTP 延迟 + 压缩率）
  python benchmark.py --load                   # 并发压测（扫描 1→2→4→8→16 并发）
  python benchmark.py --load --concurrency 8   # 并发压测（扫描到 8）
  python benchmark.py --quality                # 效果测试（调服务，看关键词保留率）
  python benchmark.py --eval                   # LLM 效果测试（OpenAI 协议）
  python benchmark.py --all                    # 性能 + 效果都跑
  python benchmark.py --url http://x:8010      # 指定服务地址
  python benchmark.py --iterations 50          # 性能测试迭代次数
  python benchmark.py --duration 60            # 每场景至少跑多少秒

--eval 模式需要：
  export OPENAI_API_KEY=sk-...
  export OPENAI_BASE_URL=https://api.openai.com/v1   # 可选，默认 OpenAI，支持任意兼容服务
  export EVAL_MODEL=gpt-4o-mini                       # 可选，默认 gpt-4o-mini
  pip install openai headroom-ai[evals]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import threading
import time
from io import BytesIO

import httpx
from PIL import Image

DEFAULT_URL      = "http://localhost:8010"
DEFAULT_N        = 100
DEFAULT_DURATION = 60  # seconds; --duration overrides --iterations

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]+")


def cjk_bigrams(text: str) -> str:
    def _expand(m: re.Match) -> str:
        s = m.group()
        parts: list[str] = []
        for i, ch in enumerate(s):
            parts.append(ch)
            if i + 1 < len(s):
                parts.append(s[i] + s[i + 1])
        return " ".join(parts)
    return _CJK_RE.sub(_expand, text)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_json(n: int) -> str:
    return json.dumps([
        {"id": i, "name": f"user_{i}", "email": f"user{i}@example.com",
         "status": "active" if i % 3 else "inactive", "score": i * 1.5}
        for i in range(n)
    ])

def _make_log(n: int) -> str:
    return "\n".join(
        f"2024-01-01 12:{i//60:02d}:{i%60:02d} {'ERROR' if i % 20 == 0 else 'INFO'} "
        f"[app.service] {'connection failed: timeout' if i % 20 == 0 else f'processing request {i}'}"
        for i in range(n)
    )

def _make_search(n: int) -> str:
    return "\n".join(
        f"src/module_{i % 10}/handler.py:{i * 3}:    raise ValueError('invalid input: {i}')"
        if i % 5 == 0 else
        f"src/module_{i % 10}/handler.py:{i * 3}:    return process(data[{i}])"
        for i in range(n)
    )

def _make_code(n: int) -> str:
    return "\n".join([
        "import os, sys, json, logging",
        "from typing import Optional, List",
        "",
    ] + [
        f"def process_{i}(data: List[dict]) -> Optional[dict]:\n"
        f"    \"\"\"Process item {i}.\"\"\"\n"
        f"    if not data:\n        return None\n"
        f"    result = {{}}\n"
        f"    for item in data:\n        result[item['id']] = item['value']\n"
        f"    return result\n"
        for i in range(n)
    ])

JSON_100  = _make_json(100)
JSON_500  = _make_json(500)
JSON_1000 = _make_json(1000)

LOG_500  = _make_log(500)
LOG_1000 = _make_log(1000)
LOG_2000 = _make_log(2000)

SEARCH_RESULTS      = _make_search(200)
SEARCH_RESULTS_500  = _make_search(500)
SEARCH_RESULTS_1000 = _make_search(1000)

CODE_200  = _make_code(30)
CODE_500  = _make_code(70)
CODE_1000 = _make_code(140)

ZH_JSON = json.dumps([
    {"序号": i, "状态": "错误" if i % 10 == 0 else "正常",
     "消息": f"处理请求{i}失败：连接超时" if i % 10 == 0 else f"请求{i}处理成功",
     "时间": f"2024-01-01T12:00:{i:02d}Z"}
    for i in range(100)
], ensure_ascii=False)

ZH_SEARCH = "\n".join(
    f"src/service/auth.py:{i * 3}:{'认证失败：用户不存在' if i % 5 == 0 else f'处理请求{i}，返回成功'}"
    for i in range(100)
)

MIXED_SEARCH = "\n".join(
    f"src/api/handler.py:{i * 2}:"
    + ("ERROR: 认证失败 AuthenticationError user_id={i}" if i % 5 == 0
       else f"INFO: 请求处理完成 request_id={i} status=200")
    for i in range(100)
)

ZH_LOG_PURE = "\n".join(
    f"[{i:04d}] {'【错误】连接超时，重试第{r}次'.format(r=i%3+1) if i % 8 == 0 else f'【信息】处理任务{i}，状态正常'}"
    for i in range(200)
)

ZH_LOG_WITH_LEVEL = "\n".join(
    f"2024-01-01 12:{i//60:02d}:{i%60:02d} {'ERROR' if i % 10 == 0 else 'INFO'} "
    f"[认证服务] {'用户认证失败：密码错误，user_id=' + str(i) if i % 10 == 0 else f'用户{i}登录成功'}"
    for i in range(300)
)

ZH_CODE = "\n".join([
    "import os",
    "from typing import Optional",
    "",
] + [
    f"def 处理用户请求_{i}(用户id: int, 数据: dict) -> Optional[dict]:\n"
    f"    \"\"\"处理第{i}类用户请求，返回处理结果。\"\"\"\n"
    f"    if not 数据:\n        return None\n"
    f"    结果 = {{}}\n"
    f"    for 键, 值 in 数据.items():\n        结果[键] = 值\n"
    f"    return 结果\n"
    for i in range(20)
])


GIT_DIFF = "\n".join([
    "diff --git a/src/auth.py b/src/auth.py",
    "index 1234567..abcdefg 100644",
    "--- a/src/auth.py",
    "+++ b/src/auth.py",
] + [
    f"@@ -{i*10},7 +{i*10},8 @@\n"
    + "\n".join(
        f" {'    return True' if j % 3 == 0 else f'    process({j})'}"
        for j in range(6)
    )
    + f"\n-    old_logic_{i}()\n+    new_logic_{i}()\n+    log_change_{i}()"
    for i in range(30)
])


def _make_image_b64(width: int = 1024, height: int = 768) -> str:
    img = Image.new("RGB", (width, height), color=(100, 149, 237))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── 性能测试（HTTP）──────────────────────────────────────────────────────────────

def bench(client: httpx.Client, url: str, payload: dict, n: int, duration: float = 0) -> dict:
    """Run n iterations, or keep going until `duration` seconds elapsed (whichever is more)."""
    client.post(url, json=payload)  # warmup
    times = []
    deadline = time.perf_counter() + duration if duration else None
    i = 0
    while True:
        t0 = time.perf_counter()
        r = client.post(url, json=payload)
        times.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        i += 1
        if deadline:
            if time.perf_counter() >= deadline and i >= n:
                break
        else:
            if i >= n:
                break
    total_s = sum(times) / 1000
    result = r.json()
    return {
        "n":    len(times),
        "p50":  round(statistics.median(times), 1),
        "p95":  round(sorted(times)[int(len(times) * 0.95)], 1),
        "mean": round(statistics.mean(times), 1),
        "qps":  round(len(times) / total_s, 1) if total_s > 0 else 0,
        "result": result,
    }


def run_perf(base: str, n: int, duration: float = 0) -> None:
    label = f"≥{duration:.0f}s" if duration else f"n={n}"
    print(f"\n{'='*90}")
    print(f"性能测试  {base}  ({label})")
    print(f"{'='*90}")
    print(f"{'场景':<38} {'n':>5} {'p50':>7} {'p95':>7} {'mean':>7}  {'qps':>6}  {'压缩率':>8}  策略")
    print("-" * 90)

    image_b64     = _make_image_b64()
    image_b64_lg  = _make_image_b64(1536, 1024)
    scenarios = [
        ("JSON 100条（英文）",           "/compress",       {"content": JSON_100,            "context": "find errors"}),
        ("JSON 500条（英文）",           "/compress",       {"content": JSON_500,            "context": "find errors"}),
        ("JSON 1000条（英文）",          "/compress",       {"content": JSON_1000,           "context": "find errors"}),
        ("JSON 100条（中文）",           "/compress",       {"content": ZH_JSON,             "context": "查找错误"}),
        ("日志 500行（英文）",           "/compress",       {"content": LOG_500,             "context": "connection error"}),
        ("日志 1000行（英文）",          "/compress",       {"content": LOG_1000,            "context": "connection error"}),
        ("日志 2000行（英文）",          "/compress",       {"content": LOG_2000,            "context": "connection error"}),
        ("日志 300行（中文）",           "/compress",       {"content": ZH_LOG_WITH_LEVEL,   "context": "认证失败"}),
        ("搜索 200行（英文）",           "/compress",       {"content": SEARCH_RESULTS,      "context": "ValueError"}),
        ("搜索 500行（英文）",           "/compress",       {"content": SEARCH_RESULTS_500,  "context": "ValueError"}),
        ("搜索 1000行（英文）",          "/compress",       {"content": SEARCH_RESULTS_1000, "context": "ValueError"}),
        ("搜索（中文）",                 "/compress",       {"content": ZH_SEARCH,           "context": "认证失败"}),
        ("代码 200行（英文）",           "/compress",       {"content": CODE_200,            "context": "process function"}),
        ("代码 500行（英文）",           "/compress",       {"content": CODE_500,            "context": "process function"}),
        ("代码 1000行（英文）",          "/compress",       {"content": CODE_1000,           "context": "process function"}),
        ("代码（中文注释）",             "/compress",       {"content": ZH_CODE,             "context": "处理请求"}),
        ("搜索（中英混写）",             "/compress",       {"content": MIXED_SEARCH,        "context": "认证失败 ERROR"}),
        ("Git diff 30 hunks",            "/compress",       {"content": GIT_DIFF,            "context": "auth logic change"}),
        ("图片 1024×768 → 768px",       "/compress/image", {"image": image_b64}),
        ("图片 1024×768 → 512px",       "/compress/image", {"image": image_b64, "max_dimension": 512}),
        ("图片 1536×1024 → 768px",      "/compress/image", {"image": image_b64_lg}),
    ]

    batch_payload     = {"items": [{"content": JSON_100,  "context": "find errors"}] * 8}
    batch_payload_lg  = {"items": [{"content": JSON_1000, "context": "find errors"}] * 8}

    with httpx.Client(base_url=base, timeout=30) as client:
        for name, endpoint, payload in scenarios:
            stats = bench(client, endpoint, payload, n, duration)
            r = stats["result"]
            ratio_str = f"{r['ratio']:.1%}"
            strategy  = r.get("strategy", r.get("media_type", ""))
            print(f"{name:<38} {stats['n']:>5} {stats['p50']:>6}ms {stats['p95']:>6}ms {stats['mean']:>6}ms  {stats['qps']:>5.1f}/s  {ratio_str:>8}  {strategy}")

        for blabel, bpayload in [("Batch 8×JSON-100", batch_payload), ("Batch 8×JSON-1000", batch_payload_lg)]:
            stats = bench(client, "/compress/batch", bpayload, n, duration)
            r = stats["result"]["results"][0]
            print(f"{blabel:<38} {stats['n']:>5} {stats['p50']:>6}ms {stats['p95']:>6}ms {stats['mean']:>6}ms  {stats['qps']:>5.1f}/s  {r['ratio']:.1%}  batch")

    print()


# ── 并发压测 ──────────────────────────────────────────────────────────────────────

def bench_concurrent(base: str, endpoint: str, payload: dict, concurrency: int, duration: float) -> dict:
    """N 个线程并发持续打 duration 秒，汇总所有请求的延迟和 QPS。"""
    all_times: list[float] = []
    last_result: dict = {}
    lock = threading.Lock()
    stop = threading.Event()

    def worker():
        nonlocal last_result
        with httpx.Client(base_url=base, timeout=30) as client:
            client.post(endpoint, json=payload)  # warmup
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    r = client.post(endpoint, json=payload)
                    elapsed = (time.perf_counter() - t0) * 1000
                    r.raise_for_status()
                    with lock:
                        all_times.append(elapsed)
                        last_result = r.json()
                except Exception:
                    pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    actual_s = time.perf_counter() - t_start

    times = sorted(all_times)
    n = len(times)
    return {
        "n":    n,
        "p50":  round(statistics.median(times), 1) if n else 0,
        "p95":  round(times[int(n * 0.95)], 1) if n > 1 else 0,
        "p99":  round(times[int(n * 0.99)], 1) if n > 1 else 0,
        "mean": round(statistics.mean(times), 1) if n else 0,
        "qps":  round(n / actual_s, 1) if actual_s > 0 else 0,
        "result": last_result,
    }


def run_load(base: str, max_concurrency: int, duration: float) -> None:
    """并发扫描：从 1 扫到 max_concurrency（翻倍步进），找峰值 QPS 和延迟拐点。"""
    levels = []
    c = 1
    while c <= max_concurrency:
        levels.append(c)
        c *= 2
    if levels[-1] != max_concurrency:
        levels.append(max_concurrency)

    image_b64_load = _make_image_b64(1536, 1024)
    load_scenarios = [
        ("JSON-100",      "/compress",       {"content": JSON_100,            "context": "find errors"}),
        ("JSON-500",      "/compress",       {"content": JSON_500,            "context": "find errors"}),
        ("JSON-1000",     "/compress",       {"content": JSON_1000,           "context": "find errors"}),
        ("日志-500行",    "/compress",       {"content": LOG_500,             "context": "connection error"}),
        ("日志-1000行",   "/compress",       {"content": LOG_1000,            "context": "connection error"}),
        ("日志-2000行",   "/compress",       {"content": LOG_2000,            "context": "connection error"}),
        ("搜索-200行",    "/compress",       {"content": SEARCH_RESULTS,      "context": "ValueError"}),
        ("搜索-1000行",   "/compress",       {"content": SEARCH_RESULTS_1000, "context": "ValueError"}),
        ("代码-200行",    "/compress",       {"content": CODE_200,            "context": "process function"}),
        ("代码-1000行",   "/compress",       {"content": CODE_1000,           "context": "process function"}),
        ("图片 1536×1024","/compress/image", {"image": image_b64_load}),
        ("Batch 8×JSON",  "/compress/batch", {"items": [{"content": JSON_100, "context": "find errors"}] * 8}),
    ]

    print(f"\n{'='*100}")
    print(f"并发压测  {base}  (每级 {duration:.0f}s，并发: {' → '.join(str(l) for l in levels)})")
    print(f"{'='*100}")
    print(f"{'场景':<14} {'并发':>4} {'n':>6} {'p50':>7} {'p95':>7} {'p99':>7} {'mean':>7}  {'qps':>8}  压缩率")
    print("-" * 100)

    for name, endpoint, payload in load_scenarios:
        prev_qps = 0.0
        for concurrency in levels:
            stats = bench_concurrent(base, endpoint, payload, concurrency, duration)
            r = stats["result"]
            if "results" in r:       # batch
                ratio = r["results"][0].get("ratio") if r["results"] else None
            else:
                ratio = r.get("ratio")
            ratio_str = f"{ratio:.1%}" if ratio is not None else "—"
            peak_mark = " ◀峰值" if stats["qps"] < prev_qps * 0.95 and prev_qps > 0 else ""
            print(
                f"{name:<14} {concurrency:>4} {stats['n']:>6} "
                f"{stats['p50']:>6}ms {stats['p95']:>6}ms {stats['p99']:>6}ms {stats['mean']:>6}ms  "
                f"{stats['qps']:>7.1f}/s  {ratio_str}{peak_mark}"
            )
            prev_qps = stats["qps"]
        print()

    print()


# ── 效果测试（HTTP）──────────────────────────────────────────────────────────────

def run_quality(base: str) -> None:
    """效果测试：调服务检验 bigram 预处理对中文关键词保留率的提升。

    对比：原始 context vs bigram 展开后的 context，看关键词保留率差异。
    只需要 httpx，服务跑着就行。
    """
    def compress(client: httpx.Client, content: str, context: str) -> str:
        r = client.post("/compress", json={"content": content, "context": context})
        r.raise_for_status()
        return r.json()["compressed"]

    def hit_rate(compressed: str, keyword: str) -> tuple[int, int]:
        lines = [l for l in compressed.splitlines() if l.strip()]
        hits  = sum(1 for l in lines if keyword in l)
        return hits, len(lines)

    sep = '=' * 90
    print(f"\n{sep}")
    print(f"效果测试  {base}")
    print(sep)

    cases = [
        ("中文搜索结果 × 认证失败",      ZH_SEARCH,         "查找认证失败",       "认证失败"),
        ("中英混写搜索 × 认证失败",      MIXED_SEARCH,      "认证失败错误",       "认证失败"),
        ("中文日志(有level) × 认证",     ZH_LOG_WITH_LEVEL, "用户认证失败",       "认证失败"),
        ("纯中文日志 × 连接超时",        ZH_LOG_PURE,       "连接超时错误",       "连接超时"),
        ("中文 JSON × 错误状态",         ZH_JSON,           "查找错误状态",       "错误"),
        ("中文代码 × 处理请求",          ZH_CODE,           "处理用户请求函数",   "处理用户请求"),
        ("英文搜索 × ValueError",        SEARCH_RESULTS,    "ValueError错误",     "ValueError"),
        ("英文日志 × connection error",  LOG_500,           "connection error",   "connection failed"),
        ("Git diff × auth",              GIT_DIFF,          "auth logic change",  "new_logic"),
    ]

    header = f"{'场景':<32} {'原始 命中/保留':>18} {'bigram 命中/保留':>18} {'提升':>8}"
    print("\n" + header)
    print("-" * 84)

    with httpx.Client(base_url=base, timeout=30) as client:
        for name, content, context, keyword in cases:
            raw    = compress(client, content, context)
            bigram = compress(client, content, cjk_bigrams(context))

            raw_hits,    raw_total    = hit_rate(raw,    keyword)
            bigram_hits, bigram_total = hit_rate(bigram, keyword)
            raw_rate    = raw_hits    / max(raw_total, 1)
            bigram_rate = bigram_hits / max(bigram_total, 1)
            delta       = bigram_rate - raw_rate

            delta_str = f"+{delta:.1%}" if delta > 0.001 else (f"{delta:.1%}" if delta < -0.001 else "  持平")
            print(
                f"{name:<32} "
                f"{raw_hits:>3}/{raw_total:<4}({raw_rate:.0%})  "
                f"{bigram_hits:>3}/{bigram_total:<4}({bigram_rate:.0%})  "
                f"{delta_str:>8}"
            )

        # 详细示例
        sep2 = '─' * 90
        print(f"\n{sep2}")
        print("详细：中文搜索 × '查找认证失败'（各展示前 5 行）")
        print(sep2)
        raw    = compress(client, ZH_SEARCH, "查找认证失败")
        bigram = compress(client, ZH_SEARCH, cjk_bigrams("查找认证失败"))

        print("\n原始 context 保留的前5行：")
        for l in [l for l in raw.splitlines() if l.strip()][:5]:
            mark = '✓' if '认证失败' in l else ' '
            print(f"  [{mark}] {l[:80]}")

        print("\nbigram 预处理保留的前5行：")
        for l in [l for l in bigram.splitlines() if l.strip()][:5]:
            mark = '✓' if '认证失败' in l else ' '
            print(f"  [{mark}] {l[:80]}")

    print()


# ── LLM 效果测试（OpenAI 协议）────────────────────────────────────────────────

def run_eval_llm() -> None:
    """LLM 效果测试：压缩后内容是否仍能正确回答问题。

    使用 OpenAI 协议，支持任意兼容服务（OpenAI、DeepSeek、Azure、本地 vLLM 等）。

    需要：
      export OPENAI_API_KEY=sk-...
      export OPENAI_BASE_URL=https://api.deepseek.com   # 可选，默认 OpenAI
      export EVAL_MODEL=deepseek-v4-flash               # 可选，默认 gpt-4o-mini
      pip install openai headroom-ai[evals]
    """
    api_key  = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model    = os.environ.get("EVAL_MODEL", "gpt-4o-mini")

    if not api_key:
        print("\n[eval] ⚠️  跳过 —— 未设置 OPENAI_API_KEY，请先：")
        print("       export OPENAI_API_KEY=sk-...")
        print("       export OPENAI_BASE_URL=https://api.deepseek.com  # DeepSeek")
        print("       export EVAL_MODEL=deepseek-v4-flash")
        raise SystemExit(1)

    try:
        from headroom.evals import run_quick_eval
    except ImportError as e:
        print(f"\n[eval] ⚠️  跳过 —— 缺少依赖 ({e})")
        print("       pip install openai headroom-ai[evals]")
        raise SystemExit(1)

    print(f"\n{'='*90}")
    print(f"LLM 效果测试  model={model}  base_url={base_url or '(openai default)'}")
    print(f"{'='*90}")

    result = run_quick_eval(n_samples=20, provider="openai", model=model)

    print(f"\n{'指标':<30} {'值':>10}")
    print("-" * 45)
    print(f"{'测试样本数':<30} {result.total_cases:>10}")
    print(f"{'通过（信息保留）':<30} {result.passed_cases:>10}")
    print(f"{'失败':<30} {result.failed_cases:>10}")
    print(f"{'信息保留率':<30} {result.accuracy_preservation_rate:>9.1%}")
    print(f"{'平均 F1':<30} {result.avg_f1_score:>9.3f}")
    print(f"{'平均压缩率':<30} {result.avg_compression_ratio:>9.1%}")
    print(f"{'节省 token':<30} {result.total_tokens_saved:>10,}")
    print()


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="tool-compress 基准测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
模式说明：
  默认         性能测试（HTTP 延迟 + 压缩率），需要服务在运行
  --quality    效果测试（本地关键词保留率），不需要服务，也不需要 API Key
  --eval       LLM 效果测试，需要 export ANTHROPIC_API_KEY=sk-ant-...
  --all        性能 + 效果（--quality 版）
        """.strip(),
    )
    parser.add_argument("--url",         default=DEFAULT_URL, help="服务地址")
    parser.add_argument("--iterations",  type=int, default=DEFAULT_N, help="性能测试迭代次数（duration=0时生效）")
    parser.add_argument("--duration",    type=float, default=DEFAULT_DURATION, help="每个场景至少跑多少秒（0=只跑 --iterations 次）")
    parser.add_argument("--load",        action="store_true", help="并发压测：扫描不同并发数找峰值 QPS")
    parser.add_argument("--concurrency", type=int, default=16, help="并发压测最大并发数（默认 16，从 1 翻倍扫到此值）")
    parser.add_argument("--quality",     action="store_true", help="效果测试（本地，无需 HTTP）")
    parser.add_argument("--eval",        action="store_true", help="LLM 效果测试（需 ANTHROPIC_API_KEY）")
    parser.add_argument("--all",         action="store_true", help="性能 + 效果都跑")
    parser.add_argument("--zh",          action="store_true", help="同 --quality（兼容旧写法）")
    args = parser.parse_args()

    if args.eval:
        run_eval_llm()
    elif args.load:
        run_load(args.url, args.concurrency, args.duration)
    elif args.quality or args.zh:
        run_quality(args.url)
    elif args.all:
        run_perf(args.url, args.iterations, args.duration)
        run_quality(args.url)
    else:
        run_perf(args.url, args.iterations, args.duration)
