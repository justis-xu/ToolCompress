#!/usr/bin/env python3
"""端到端效果评测：通过 /compress HTTP 接口压缩，再用 LLM 验证信息保留率。

流程（before_after）：
  原始 context + question → LLM → baseline 答案
  压缩后 context + question → LLM → compressed 答案
  对比两个答案 vs ground_truth，计算 F1 / exact_match / 保留率

用法：
  export OPENAI_API_KEY=sk-...
  export OPENAI_BASE_URL=https://api.deepseek.com   # DeepSeek
  export EVAL_MODEL=deepseek-v4-flash

  python3.12 tests/eval_service.py --url http://localhost:8010
  python3.12 tests/eval_service.py --url http://localhost:8010 --datasets squad bfcl hotpotqa
  python3.12 tests/eval_service.py --url http://localhost:8010 -n 200
  python3.12 tests/eval_service.py --url http://localhost:8010 --no-baseline   # 跳过 baseline，只测压缩后
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_URL      = "http://localhost:8010"
DEFAULT_N        = 100
DEFAULT_DATASETS = ["tool_outputs", "squad", "bfcl"]
ALL_DATASETS     = ["tool_outputs", "squad", "bfcl", "hotpotqa", "msmarco", "codesearchnet"]


# ── 指标函数 ──────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def f1_score(pred: str, gold: str) -> float:
    p_tokens = _normalize(pred).split()
    g_tokens = _normalize(gold).split()
    if not p_tokens or not g_tokens:
        return float(p_tokens == g_tokens)
    common = set(p_tokens) & set(g_tokens)
    if not common:
        return 0.0
    precision = len(common) / len(p_tokens)
    recall    = len(common) / len(g_tokens)
    return 2 * precision * recall / (precision + recall)


def exact_match(pred: str, gold: str) -> bool:
    return _normalize(pred) == _normalize(gold)


# ── LLM 调用 ──────────────────────────────────────────────────────────────────

def _llm_answer(client: "openai.OpenAI", model: str, context: str, question: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        max_tokens=256,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": (
                f"Answer the question based on the context below. "
                f"Be concise — 1-3 words when possible.\n\n"
                f"Context:\n{context}\n\n"
                f"Question: {question}\n\nAnswer:"
            ),
        }],
    )
    return (resp.choices[0].message.content or "").strip()


# ── 压缩 ──────────────────────────────────────────────────────────────────────

@dataclass
class CompressResult:
    compressed: str
    strategy: str
    original_chars: int
    compressed_chars: int
    ratio: float
    latency_ms: float


def _compress(http: httpx.Client, content: str, context: str = "") -> CompressResult:
    t0 = time.perf_counter()
    r = http.post("/compress", json={"content": content, "context": context})
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    d = r.json()
    return CompressResult(
        compressed=d["compressed"],
        strategy=d["strategy"],
        original_chars=d["original_chars"],
        compressed_chars=d["compressed_chars"],
        ratio=d["ratio"],
        latency_ms=ms,
    )


# ── 单数据集评测 ──────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    case_id: str
    baseline_f1: float
    compressed_f1: float
    baseline_em: bool
    compressed_em: bool
    compression_ratio: float
    strategy: str
    latency_ms: float


@dataclass
class DatasetResult:
    dataset: str
    n: int
    baseline_f1: float
    compressed_f1: float
    baseline_em: float
    compressed_em: float
    accuracy_preservation: float   # compressed_f1 / baseline_f1（保留率）
    avg_compression_ratio: float
    avg_latency_ms: float
    cases: list[CaseResult] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.dataset:<20} n={self.n:<4} "
            f"baseline_F1={self.baseline_f1:.3f}  "
            f"compressed_F1={self.compressed_f1:.3f}  "
            f"保留率={self.accuracy_preservation:.1%}  "
            f"压缩率={self.avg_compression_ratio:.1%}  "
            f"压缩耗时={self.avg_latency_ms:.0f}ms"
        )


def run_dataset(
    dataset_name: str,
    n: int,
    http: httpx.Client,
    llm: "openai.OpenAI",
    model: str,
    baseline: bool,
) -> DatasetResult:
    from headroom.evals.datasets import load_tool_output_samples, load_dataset_by_name

    print(f"\n  加载 {dataset_name}...")
    if dataset_name == "tool_outputs":
        suite = load_tool_output_samples()
    else:
        suite = load_dataset_by_name(dataset_name, n=n)
    cases = suite.cases[:n]
    print(f"  共 {len(cases)} 条样本")

    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case.id}", end="", flush=True)

        question     = getattr(case, "query", "")
        ground_truth = str(getattr(case, "ground_truth", ""))

        # 压缩
        try:
            cr = _compress(http, case.context, question)
        except Exception as e:
            print(f"  ✗ compress error: {e}")
            continue

        # baseline：原始 context → LLM
        if baseline:
            try:
                base_ans = _llm_answer(llm, model, case.context, question)
                base_f1  = f1_score(base_ans, ground_truth)
                base_em  = exact_match(base_ans, ground_truth)
            except Exception as e:
                print(f"  ✗ baseline LLM error: {e}")
                base_f1, base_em = 0.0, False
        else:
            base_f1, base_em = 1.0, True  # 假设 baseline 完美，只测压缩效果

        # compressed：压缩后 context → LLM
        try:
            comp_ans = _llm_answer(llm, model, cr.compressed, question)
            comp_f1  = f1_score(comp_ans, ground_truth)
            comp_em  = exact_match(comp_ans, ground_truth)
        except Exception as e:
            print(f"  ✗ compressed LLM error: {e}")
            comp_f1, comp_em = 0.0, False

        results.append(CaseResult(
            case_id=case.id,
            baseline_f1=base_f1,
            compressed_f1=comp_f1,
            baseline_em=base_em,
            compressed_em=comp_em,
            compression_ratio=cr.ratio,
            strategy=cr.strategy,
            latency_ms=cr.latency_ms,
        ))

        mark = "✓" if comp_f1 >= base_f1 * 0.9 else "△" if comp_f1 >= base_f1 * 0.7 else "✗"
        print(f"  {mark} F1={comp_f1:.2f}(base={base_f1:.2f}) ratio={cr.ratio:.1%} [{cr.strategy}]")

    if not results:
        return DatasetResult(dataset_name, 0, 0, 0, 0, 0, 0, 0, 0)

    avg_base_f1  = statistics.mean(r.baseline_f1 for r in results)
    avg_comp_f1  = statistics.mean(r.compressed_f1 for r in results)
    avg_base_em  = sum(r.baseline_em for r in results) / len(results)
    avg_comp_em  = sum(r.compressed_em for r in results) / len(results)
    preservation = avg_comp_f1 / max(avg_base_f1, 1e-6)
    avg_ratio    = statistics.mean(r.compression_ratio for r in results)
    avg_latency  = statistics.mean(r.latency_ms for r in results)

    return DatasetResult(
        dataset=dataset_name,
        n=len(results),
        baseline_f1=avg_base_f1,
        compressed_f1=avg_comp_f1,
        baseline_em=avg_base_em,
        compressed_em=avg_comp_em,
        accuracy_preservation=preservation,
        avg_compression_ratio=avg_ratio,
        avg_latency_ms=avg_latency,
        cases=results,
    )


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="端到端效果评测：通过 /compress 接口压缩，DeepSeek 验证信息保留率",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
数据集（--datasets）：
  tool_outputs  内置工具调用样本（8条）
  squad         阅读理解 QA（100条）
  bfcl          工具调用 API schema（100条）
  hotpotqa      多跳推理 QA（50条）
  msmarco       Bing 搜索 + 段落（50条）
  codesearchnet 代码搜索（50条）

默认跑: {' '.join(DEFAULT_DATASETS)}
全部跑: --datasets {' '.join(ALL_DATASETS)}
        """.strip(),
    )
    parser.add_argument("--url",         default=DEFAULT_URL, help="服务地址")
    parser.add_argument("-n",            type=int, default=DEFAULT_N, help="每个数据集取前 N 条（默认 100）")
    parser.add_argument("--datasets",    nargs="+", default=DEFAULT_DATASETS,
                        choices=ALL_DATASETS + ["all"], metavar="DATASET",
                        help="要评测的数据集，空格分隔")
    parser.add_argument("--no-baseline", action="store_true",
                        help="跳过 baseline LLM 调用（节省费用，只看压缩后 F1）")
    args = parser.parse_args()

    if "all" in args.datasets:
        args.datasets = ALL_DATASETS

    api_key  = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model    = os.environ.get("EVAL_MODEL", "deepseek-v4-flash")

    if not api_key:
        print("⚠️  未设置 OPENAI_API_KEY，请先：")
        print("   export OPENAI_API_KEY=sk-...")
        print("   export OPENAI_BASE_URL=https://api.deepseek.com")
        print("   export EVAL_MODEL=deepseek-v4-flash")
        sys.exit(1)

    try:
        import openai
    except ImportError:
        print("⚠️  缺少依赖：pip install openai headroom-ai[evals]")
        sys.exit(1)

    llm_kwargs: dict = {"api_key": api_key}
    if base_url:
        llm_kwargs["base_url"] = base_url
    llm = openai.OpenAI(**llm_kwargs)

    # 健康检查
    with httpx.Client(base_url=args.url, timeout=10) as http:
        try:
            http.get("/health").raise_for_status()
        except Exception as e:
            print(f"⚠️  服务不可达 {args.url}: {e}")
            sys.exit(1)

    print(f"\n{'='*80}")
    print(f"端到端效果评测")
    print(f"  服务:    {args.url}")
    print(f"  模型:    {model}  base_url={base_url or '(openai默认)'}")
    print(f"  数据集:  {' '.join(args.datasets)}")
    print(f"  样本数:  每集最多 {args.n} 条")
    print(f"  Baseline: {'跳过' if args.no_baseline else '开启（2x LLM 调用）'}")
    print(f"{'='*80}")

    all_results: list[DatasetResult] = []

    with httpx.Client(base_url=args.url, timeout=60) as http:
        for ds in args.datasets:
            print(f"\n{'─'*60}")
            print(f"数据集: {ds}")
            print(f"{'─'*60}")
            try:
                result = run_dataset(ds, args.n, http, llm, model, baseline=not args.no_baseline)
                all_results.append(result)
            except Exception as e:
                print(f"  ✗ 数据集加载失败: {e}")
                continue

    # 汇总
    print(f"\n{'='*80}")
    print("汇总结果")
    print(f"{'='*80}")
    print(f"{'数据集':<20} {'n':>4}  {'基线F1':>8}  {'压缩F1':>8}  {'保留率':>7}  {'压缩率':>7}  {'压缩耗时':>8}")
    print("-" * 80)
    for r in all_results:
        print(
            f"{r.dataset:<20} {r.n:>4}  "
            f"{r.baseline_f1:>8.3f}  {r.compressed_f1:>8.3f}  "
            f"{r.accuracy_preservation:>7.1%}  {r.avg_compression_ratio:>7.1%}  "
            f"{r.avg_latency_ms:>7.0f}ms"
        )

    valid = [r for r in all_results if r.n > 0]
    if valid:
        total_n       = sum(r.n for r in valid)
        total_base_f1 = statistics.mean(r.baseline_f1 for r in valid)
        total_comp_f1 = statistics.mean(r.compressed_f1 for r in valid)
        total_pres    = statistics.mean(r.accuracy_preservation for r in valid)
        total_ratio   = statistics.mean(r.avg_compression_ratio for r in valid)
        print("-" * 80)
        print(
            f"{'总计/均值':<20} {total_n:>4}  "
            f"{total_base_f1:>8.3f}  {total_comp_f1:>8.3f}  "
            f"{total_pres:>7.1%}  {total_ratio:>7.1%}"
        )

    print()


if __name__ == "__main__":
    main()
