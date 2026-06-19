#!/usr/bin/env python3
"""Evaluate `log` compression with answer-agreement judging (QA口径).

和 `../log_deterministic/eval.py` 的区别：
- `log_deterministic` 检查"异常模板是否还在压缩结果文本里"，是确定性的下限保证，
  不涉及模型理解
- 这里是端到端 QA 口径：让模型基于原始/压缩后的日志分别回答同一个问题（"出现了
  哪几种异常类型"），再用 judge 模型比较两个回答是否保留了关键事实——和 JSON/
  代码/搜索/Git diff 几个场景的评测口径对齐

Dataset:
- `data/`，和 `log_deterministic/data/` 是同一份 LogHub 样本（各自目录下各放一份，
  保持"一个评测一个数据集目录"），`meta.jsonl` 的 `question` 字段是固定问题。
  问题措辞明确要求"先把数字/时间戳/ID 归一化成模板再分类"，避免 judge 把"同一种
  错误因为状态码数字不同被一边合并、一边拆开"误判成信息丢失（早期版本问题模糊时
  这种假阴性占了大多数 FAIL）

The evaluator intentionally accepts `PASS` unless *all* judge retries say
`FAIL`，原因和 `git_diff_judge/eval.py` 一样：judge 在这类总结题上抖动很常见。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import openai


DATA_DIR = Path(__file__).parent / "data"
DEFAULT_URL = "http://localhost:8010"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_JUDGE_MODEL = "deepseek-chat"


def _ask(client: openai.OpenAI, model: str, content: str, question: str) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"日志内容：\n{content}\n\n"
                f"问题：{question}\n\n"
                "简洁回答，按异常类型分点列出。"
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
                f"回答A（基于原始日志）：{answer_a}\n\n"
                f"回答B（基于压缩后日志）：{answer_b}\n\n"
                "判断回答B是否覆盖了回答A里提到的全部错误模板（同一种错误，仅状态码/数字/ID/时间戳不同"
                "不算两种模板，无论A和B各自是合并还是拆分了这些数字差异，只要对应的是同一类底层错误就算覆盖）。"
                "忽略措辞和频率描述的差异。只输出 `PASS: 理由` 或 `FAIL: 理由`。"
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
    parser = argparse.ArgumentParser(description="Batch log QA evaluation with DeepSeek + judge model")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--out", type=Path, default=Path("/tmp/toolcompress-eval-log-judge.jsonl"))
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
        f"log QA eval start cases={len(rows)} deepseek={args.deepseek_model} "
        f"judge={args.judge_provider}:{args.judge_model} "
        f"deepseek_conc={args.deepseek_concurrency} judge_conc={args.judge_concurrency}",
        flush=True,
    )

    answered: list[dict] = []
    answer_lock = threading.Lock()

    def run_answers(row: dict) -> dict:
        content = (args.data_dir / row["file"]).read_text(errors="ignore")
        response = http.post("/compress", json={"content": content, "context": row["context"]})
        response.raise_for_status()
        compressed = response.json()
        answer_a = _ask(deepseek, args.deepseek_model, content, row["question"])
        answer_b = _ask(deepseek, args.deepseek_model, compressed["compressed"], row["question"])
        result = {
            "id": row["id"],
            "source": row["source"],
            "question": row["question"],
            "strategy": compressed["strategy"],
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
