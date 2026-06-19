#!/usr/bin/env python3
"""Evaluate image compression on the fixed TextVQA baseline20 dataset.

Two supported modes:
- `full_low`:
  Default product mode. Prioritizes token reduction.
- `preserve`:
  Compatibility / comparison mode. Use only when you explicitly want a more
  conservative image path.

Dataset:
- `data/`
  The formal image benchmark. Contains only 20 images plus `meta.jsonl`.
  It was selected so the original images form a high-confidence baseline for
  the chosen vision model.

Metric:
- `compressed_acc`: final product metric
- `preserved`: among baseline-correct items, how many remain correct after
  compression
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
import openai


DATA_DIR = Path(__file__).parent / "data"
META = DATA_DIR / "meta.jsonl"
IMAGES = DATA_DIR / "images"
DEFAULT_URL = "http://localhost:8010"
DEFAULT_MODEL = "MiniMax-M3"
REQUEST_TIMEOUT = 60


def normalize(text: str) -> str:
    text = (text or "").strip().lower()
    text = text.replace("$", "").replace("%", "")
    text = re.sub(r"[^a-z0-9:.' ]+", " ", text)
    return " ".join(text.split())


def is_correct(answer: str, item: dict) -> bool:
    got = normalize(answer)
    golds = {normalize(item["ground_truth"])}
    golds.update(normalize(candidate) for candidate in item.get("answers", []) if candidate)
    golds.discard("")
    if got in golds:
        return True
    return any(gold and (gold in got or got in gold) for gold in golds)


def ask_vision(client: openai.OpenAI, model: str, image_b64: str, question: str) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=80,
        timeout=REQUEST_TIMEOUT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Answer briefly.\nQuestion: {question}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
    )
    return (response.choices[0].message.content or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate image compression accuracy on TextVQA")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mode", choices=["preserve", "full_low"], default="full_low")
    parser.add_argument("--data", type=Path, default=META)
    parser.add_argument("--out", type=Path, default=Path("/tmp/toolcompress-eval-textvqa-image.jsonl"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    minimax_key = os.environ.get("MINIMAX_API_KEY")
    if not minimax_key:
        raise SystemExit("需要 MINIMAX_API_KEY")

    llm = openai.OpenAI(api_key=minimax_key, base_url="https://api.minimaxi.com/v1")
    http = httpx.Client(base_url=args.url, timeout=REQUEST_TIMEOUT)

    items = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()]
    if args.limit:
        items = items[:args.limit]

    rows: list[dict] = []
    errors: list[dict] = []
    lock = threading.Lock()

    def flush() -> None:
        args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows + errors) + "\n")

    def run_item(item: dict) -> dict:
        image_path = args.data.parent / "images" / item["image_file"]
        original_b64 = base64.b64encode(image_path.read_bytes()).decode()
        response = http.post("/compress/image", json={"image": original_b64, "mode": args.mode})
        response.raise_for_status()
        compressed = response.json()

        original_answer = ask_vision(llm, args.model, original_b64, item["question"])
        compressed_answer = ask_vision(llm, args.model, compressed["compressed"], item["question"])

        return {
            "id": item["id"],
            "question": item["question"],
            "ground_truth": item["ground_truth"],
            "answers": item.get("answers", []),
            "mode": compressed.get("mode", args.mode),
            "original_tokens": compressed.get("original_tokens", 0),
            "compressed_tokens": compressed.get("compressed_tokens", 0),
            "original_answer": original_answer,
            "compressed_answer": compressed_answer,
            "original_ok": is_correct(original_answer, item),
            "compressed_ok": is_correct(compressed_answer, item),
        }

    print(
        f"textvqa image eval start n={len(items)} model={args.model} mode={args.mode} concurrency={args.concurrency}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_item, item): item for item in items}
        for idx, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                row = future.result()
                with lock:
                    rows.append(row)
            except Exception as exc:
                with lock:
                    errors.append({"id": item["id"], "error": repr(exc)})
            if idx % 5 == 0 or idx == len(items):
                with lock:
                    flush()
                print(f"progress {idx}/{len(items)} errors={len(errors)}", flush=True)

    flush()

    avg_in = statistics.mean(row["original_tokens"] for row in rows)
    avg_out = statistics.mean(row["compressed_tokens"] for row in rows)
    reduction = statistics.mean(
        (row["original_tokens"] - row["compressed_tokens"]) / row["original_tokens"]
        for row in rows if row["original_tokens"]
    )
    original_acc = sum(row["original_ok"] for row in rows) / len(rows)
    compressed_acc = sum(row["compressed_ok"] for row in rows) / len(rows)
    preserved = sum(row["compressed_ok"] for row in rows if row["original_ok"])
    original_correct = sum(row["original_ok"] for row in rows)

    print(
        f"summary n={len(rows)} errors={len(errors)} avg_in={avg_in:.1f} avg_out={avg_out:.1f} "
        f"reduction={reduction:.1%} original_acc={original_acc:.1%} compressed_acc={compressed_acc:.1%} "
        f"preserved={preserved}/{max(original_correct,1)}",
        flush=True,
    )
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
