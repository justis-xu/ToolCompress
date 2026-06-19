#!/usr/bin/env python3
"""Evaluate `code_aware` on the fixed CodeSearchNet fact-QA dataset.

Why this benchmark exists:
- Free-form "describe this code" scoring against docstrings is too noisy.
- The product question for `code_aware` is simpler: after compression, does the
  code still preserve the concrete facts an LLM needs?

Dataset:
- `data/codesearchnet_factqa_python.jsonl`
- Each item is a fixed fact-QA case derived from CodeSearchNet Python samples

Scoring:
- exact / contains checks over function name, parameter order, default values,
  and docstring first line
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import time
from pathlib import Path

import httpx
import openai

from codesearchnet_factqa import build_case, grade


DATA = Path(__file__).parent / "data" / "codesearchnet_factqa_python.jsonl"
OUT = Path("/tmp/toolcompress-eval-codesearchnet-factqa.jsonl")
DEFAULT_URL = "http://localhost:8010"


def parse_json_answer(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text.removeprefix("```json").removesuffix("```").strip()
    elif text.startswith("```"):
        text = text.removeprefix("```").removesuffix("```").strip()
    if "{" in text and "}" in text:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            text = match.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate code_aware compression on CodeSearchNet fact-QA")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    model = os.environ.get("EVAL_MODEL", "deepseek-v4-flash")
    llm = openai.OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    http = httpx.Client(base_url=args.url, timeout=60)
    items = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
    cases = [case for item in items if (case := build_case(item)) is not None]

    def ask(code: str, checks: list[dict]) -> str:
        questions = "\n".join(
            f"{idx + 1}. {check['question']}" for idx, check in enumerate(checks)
        )
        response = llm.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    "Answer factual questions about the Python function below. "
                    "Return JSON only, with keys q1, q2, ... and short string answers.\n\n"
                    f"Code:\n{code}\n\n"
                    f"Questions:\n{questions}\n"
                ),
            }],
        )
        return (response.choices[0].message.content or "").strip()

    def grade_payload(payload: dict, checks: list[dict]) -> list[dict]:
        results = []
        for idx, check in enumerate(checks):
            answer = str(payload.get(f"q{idx + 1}", ""))
            results.append({
                "key": check["key"],
                "expected": check["answer"],
                "answer": answer,
                "ok": grade(answer, check),
            })
        return results

    def run_case(case: dict) -> dict:
        compressed_response = http.post(
            "/compress",
            json={"content": case["code"], "context": " ".join(c["question"] for c in case["checks"])},
        )
        compressed_response.raise_for_status()
        compressed = compressed_response.json()

        baseline_raw = ask(case["code"], case["checks"])
        compressed_raw = ask(compressed["compressed"], case["checks"])
        baseline_results = grade_payload(parse_json_answer(baseline_raw), case["checks"])
        compressed_results = grade_payload(parse_json_answer(compressed_raw), case["checks"])

        return {
            "id": case["id"],
            "original_tokens": compressed.get("original_tokens", 0),
            "compressed_tokens": compressed.get("compressed_tokens", 0),
            "strategy": compressed.get("strategy"),
            "baseline_results": baseline_results,
            "compressed_results": compressed_results,
            "baseline_raw": baseline_raw,
            "compressed_raw": compressed_raw,
        }

    print(
        f"CodeSearchNet fact-QA eval start cases={len(cases)} "
        f"concurrency={args.concurrency} model={model}",
        flush=True,
    )
    started = time.time()
    rows: list[dict] = []
    errors: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_case, case): case for case in cases}
        for idx, future in enumerate(cf.as_completed(futures), 1):
            case = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"id": case["id"], "error": repr(exc)})
            if idx % 10 == 0 or errors:
                print(f"progress {idx}/{len(cases)} errors={len(errors)}", flush=True)

    args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows + errors) + "\n")

    baseline_facts = [result["ok"] for row in rows for result in row["baseline_results"]]
    compressed_facts = [result["ok"] for row in rows for result in row["compressed_results"]]
    baseline_cases = [all(result["ok"] for result in row["baseline_results"]) for row in rows]
    compressed_cases = [all(result["ok"] for result in row["compressed_results"]) for row in rows]

    avg_in = statistics.mean(row["original_tokens"] for row in rows)
    avg_out = statistics.mean(row["compressed_tokens"] for row in rows)
    reduction = statistics.mean(
        (row["original_tokens"] - row["compressed_tokens"]) / row["original_tokens"]
        for row in rows
        if row["original_tokens"]
    )

    print(f"wrote {args.out}", flush=True)
    print(
        f"summary n={len(rows)} errors={len(errors)} "
        f"avg_in={avg_in:.1f} avg_out={avg_out:.1f} reduction={reduction:.1%} "
        f"baseline_fact_acc={sum(baseline_facts)/len(baseline_facts):.1%} "
        f"compressed_fact_acc={sum(compressed_facts)/len(compressed_facts):.1%} "
        f"baseline_case_acc={sum(baseline_cases)}/{len(baseline_cases)} "
        f"compressed_case_acc={sum(compressed_cases)}/{len(compressed_cases)}",
        flush=True,
    )
    print(f"elapsed={time.time() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
