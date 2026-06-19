#!/usr/bin/env python3
"""Evaluate `smart_crusher` on a fixed BFCL (Berkeley Function Calling
Leaderboard) "simple" sample.

Why this benchmark exists:
- BFCL 的 `context` 字段是真实工具调用场景下的函数 schema（每条 query 对应
  1-3 个结构不同的函数定义），是 SmartCrusher 列式压缩在异构小数组场景下的代表
  性输入——压缩率天然低于"100 条同构记录"的合成 benchmark（参考
  `tests/benchmark.py` 的 JSON_100），因为列式压缩靠的是同构记录间的列名复用，
  异构 schema 没有这种冗余。

Dataset:
- `data/bfcl_simple.jsonl`（100 条，固定快照，避免每次联网下载导致结果不可复现）

Scoring:
- before/after 对比：用同一个 LLM 分别基于原始 context 和压缩后 context 回答
  query，和 ground_truth 算 F1 / exact_match，报告 compressed_f1 / baseline_f1
  作为"准确率保持率"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx
import openai


DATA = Path(__file__).parent / "data" / "bfcl_simple.jsonl"
URL = "http://localhost:8010"


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
    recall = len(common) / len(g_tokens)
    return 2 * precision * recall / (precision + recall)


def exact_match(pred: str, gold: str) -> bool:
    return _normalize(pred) == _normalize(gold)


def _llm_answer(client: openai.OpenAI, model: str, context: str, question: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        max_tokens=256,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": (
                f"Answer the question based on the context below. "
                f"Be concise — 1-3 words when possible.\n\n"
                f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
            ),
        }],
    )
    return (resp.choices[0].message.content or "").strip()


@dataclass
class CaseResult:
    case_id: str
    baseline_f1: float
    compressed_f1: float
    compression_ratio: float
    strategy: str


def main() -> None:
    parser = argparse.ArgumentParser(description="BFCL simple JSON tool-calling evaluation")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=Path("/tmp/toolcompress-eval-json-tool-calling.jsonl"))
    parser.add_argument("--model", default=os.environ.get("EVAL_MODEL", "deepseek-chat"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("需要 OPENAI_API_KEY（和可选 OPENAI_BASE_URL）")
    llm = openai.OpenAI(api_key=api_key, base_url=os.environ.get("OPENAI_BASE_URL"))
    http = httpx.Client(base_url=args.url, timeout=120)

    cases = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()]
    if args.limit:
        cases = cases[:args.limit]

    def run_case(case: dict) -> CaseResult:
        response = http.post("/compress", json={"content": case["context"], "context": case["query"]})
        response.raise_for_status()
        compressed = response.json()
        base_ans = _llm_answer(llm, args.model, case["context"], case["query"])
        comp_ans = _llm_answer(llm, args.model, compressed["compressed"], case["query"])
        return CaseResult(
            case_id=case["id"],
            baseline_f1=f1_score(base_ans, case["ground_truth"]),
            compressed_f1=f1_score(comp_ans, case["ground_truth"]),
            compression_ratio=compressed["ratio"],
            strategy=compressed["strategy"],
        )

    print(f"json tool-calling eval start n={len(cases)} model={args.model} concurrency={args.concurrency}", flush=True)
    results: list[CaseResult] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_case, case): case for case in cases}
        for idx, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if idx % 10 == 0 or idx == len(cases):
                print(f"progress {idx}/{len(cases)}", flush=True)

    args.out.write_text("\n".join(json.dumps(r.__dict__, ensure_ascii=False) for r in results) + "\n")

    avg_base_f1 = statistics.mean(r.baseline_f1 for r in results)
    avg_comp_f1 = statistics.mean(r.compressed_f1 for r in results)
    avg_ratio = statistics.mean(r.compression_ratio for r in results)
    ok = sum(1 for r in results if r.compressed_f1 >= r.baseline_f1 * 0.9)
    print(
        f"summary n={len(results)} ok={ok}/{len(results)} "
        f"baseline_f1={avg_base_f1:.3f} compressed_f1={avg_comp_f1:.3f} "
        f"preservation={avg_comp_f1 / max(avg_base_f1, 1e-6):.1%} avg_ratio={avg_ratio:.1%}",
        flush=True,
    )
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
