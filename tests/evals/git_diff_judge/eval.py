#!/usr/bin/env python3
"""Evaluate `diff` compression with answer-agreement judging.

Workflow:
1. Read `data/`（50 条 headroom 真实 commit，按压缩率筛选出 ≥18% 的子集，
   原始 commit 历史里 >50% 的 commit 压缩率 <5%，在那些 case 上测准确率没有
   意义，见 README "效果测评" 小节说明）
2. Ask a model to answer the same task on:
   - the original diff
   - the compressed diff
3. Ask a judge model whether the compressed answer preserved the important facts

The evaluator intentionally accepts `PASS` unless *all* judge retries say
`FAIL`, because judge jitter is common on diff summaries.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import openai


DATA_DIR = Path(__file__).parent / "data"
META = DATA_DIR / "meta.jsonl"
DEFAULT_URL = "http://localhost:8010"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_JUDGE_MODEL = "deepseek-v4-pro"


def _ask(client: openai.OpenAI, model: str, content: str, question: str) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"内容：\n{content}\n\n"
                f"问题：{question}\n\n"
                "简洁回答，列出关键事实（文件名、模块、主要改动点）。"
            ),
        }],
    )
    return (response.choices[0].message.content or "").strip()


def _judge_once(client: openai.OpenAI, model: str, question: str, answer_a: str, answer_b: str) -> tuple[str, str]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=120,
        messages=[{
            "role": "user",
            "content": (
                f"问题：{question}\n\n"
                f"回答A（基于原始内容）：{answer_a}\n\n"
                f"回答B（基于压缩后内容）：{answer_b}\n\n"
                "判断回答B是否保留了回答A里的关键事实，忽略措辞差异。"
                "只输出 `PASS: 理由` 或 `FAIL: 理由`。"
            ),
        }],
    )
    raw = (response.choices[0].message.content or "").strip()
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if text.upper().startswith("PASS"):
        return "PASS", text
    if text.upper().startswith("FAIL"):
        return "FAIL", text
    return "UNKNOWN", text or raw


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch git diff evaluation with DeepSeek + judge model")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--out", type=Path, default=Path("/tmp/toolcompress-eval-git-diff-headroom.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--judge-provider", choices=["deepseek", "minimax"], default="deepseek")
    parser.add_argument("--deepseek-concurrency", type=int, default=12)
    parser.add_argument("--judge-concurrency", type=int, default=8)
    parser.add_argument("--judge-retries", type=int, default=3)
    args = parser.parse_args()

    deepseek_key = os.environ.get("OPENAI_API_KEY")
    deepseek_base = os.environ.get("OPENAI_BASE_URL")
    minimax_key = os.environ.get("MINIMAX_API_KEY")
    if not deepseek_key:
        raise SystemExit("需要 OPENAI_API_KEY / OPENAI_BASE_URL（DeepSeek）")
    if args.judge_provider == "minimax" and not minimax_key:
        raise SystemExit("judge-provider=minimax 时需要 MINIMAX_API_KEY")

    deepseek = openai.OpenAI(api_key=deepseek_key, base_url=deepseek_base)
    if args.judge_provider == "minimax":
        judge = openai.OpenAI(api_key=minimax_key, base_url="https://api.minimaxi.com/v1")
    else:
        judge = deepseek
    http = httpx.Client(base_url=args.url, timeout=120)

    meta = args.data_dir / "meta.jsonl"
    rows = [json.loads(line) for line in meta.read_text().splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]

    print(
        f"git diff eval start cases={len(rows)} deepseek={args.deepseek_model} "
        f"judge={args.judge_provider}:{args.judge_model} "
        f"deepseek_conc={args.deepseek_concurrency} judge_conc={args.judge_concurrency}",
        flush=True,
    )

    answered: list[dict] = []
    answer_lock = threading.Lock()

    def run_answers(row: dict) -> dict:
        diff = (args.data_dir / row["file"]).read_text(errors="ignore")
        response = http.post("/compress", json={"content": diff, "context": row["question"]})
        response.raise_for_status()
        compressed = response.json()
        answer_a = _ask(deepseek, args.deepseek_model, diff, row["question"])
        answer_b = _ask(deepseek, args.deepseek_model, compressed["compressed"], row["question"])
        result = {
            "id": row["id"],
            "sha": row["sha"],
            "file": row["file"],
            "question": row["question"],
            "strategy": compressed["strategy"],
            "ratio": compressed["ratio"],
            "original_tokens": compressed.get("original_tokens", 0),
            "compressed_tokens": compressed.get("compressed_tokens", 0),
            "answer_a": answer_a,
            "answer_b": answer_b,
        }
        with answer_lock:
            answered.append(result)
            if len(answered) % 10 == 0 or len(answered) == len(rows):
                print(f"answers {len(answered)}/{len(rows)}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.deepseek_concurrency) as pool:
        futures = [pool.submit(run_answers, row) for row in rows]
        for future in as_completed(futures):
            future.result()

    judged: list[dict] = []
    judge_lock = threading.Lock()

    def run_judge(result: dict) -> dict:
        verdicts = [
            _judge_once(judge, args.judge_model, result["question"], result["answer_a"], result["answer_b"])
            for _ in range(args.judge_retries)
        ]
        votes = [vote for vote, _ in verdicts]
        final_verdict = "FAIL" if all(vote == "FAIL" for vote in votes) else "PASS"
        judged_row = dict(result)
        judged_row["judge_votes"] = votes
        judged_row["judge_reason"] = verdicts[-1][1]
        judged_row["final_verdict"] = final_verdict
        with judge_lock:
            judged.append(judged_row)
            if len(judged) % 10 == 0 or len(judged) == len(rows):
                print(f"judge {len(judged)}/{len(rows)}", flush=True)
        return judged_row

    with ThreadPoolExecutor(max_workers=args.judge_concurrency) as pool:
        futures = [pool.submit(run_judge, result) for result in answered]
        for future in as_completed(futures):
            future.result()

    judged.sort(key=lambda row: row["id"])
    args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in judged) + "\n")

    passed = [row for row in judged if row["final_verdict"] == "PASS"]
    avg_in = statistics.mean(row["original_tokens"] for row in judged)
    avg_out = statistics.mean(row["compressed_tokens"] for row in judged)
    reduction = statistics.mean(
        (row["original_tokens"] - row["compressed_tokens"]) / row["original_tokens"]
        for row in judged
        if row["original_tokens"]
    )
    print(
        f"summary n={len(judged)} pass={len(passed)}/{len(judged)} "
        f"avg_in={avg_in:.1f} avg_out={avg_out:.1f} reduction={reduction:.1%}",
        flush=True,
    )
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
