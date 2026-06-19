#!/usr/bin/env python3
"""Deterministic search benchmark.

Why deterministic:
- Raw grep/ripgrep output is a poor prompt format for current chat models; in
  practice both `deepseek-v4-flash` and `deepseek-v4-pro` often returned empty
  answers on these payloads.
- For `search`, the compressor's job is to keep the best retrieval clues, not
  to answer the final question itself, so a deterministic fidelity check is a
  better fit than LLM-as-judge.

Dataset:
- Default input is `data/search_factqa_headroom.jsonl`
- The current dataset is intentionally biased toward "official-like" grep
  payloads: long windows with repeated matches concentrated in a few files.

Scoring:
- `top1`: the highest-frequency file path must still appear after compression
- `top3`: at least 2 of the top-3 highest-frequency file paths must still appear
- `ok`: both `top1` and `top3` hold

This is the number that should be used for the `search` row in benchmark
tables. The raw `preserved_ratio` is still recorded for debugging, but it is
not the primary product metric anymore.
"""
from __future__ import annotations

import argparse
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx


DATA = Path(__file__).parent / "data" / "search_factqa_headroom.jsonl"
OUT = Path("/tmp/toolcompress-eval-search-deterministic.jsonl")
URL = "http://localhost:8010"


def extract_files(content: str) -> list[str]:
    files = []
    seen = set()
    for line in content.splitlines():
        if ":" not in line:
            continue
        path = line.split(":", 1)[0]
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return files


def extract_file_counts(content: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        path = line.split(":", 1)[0]
        if path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic search compression evaluation")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args()

    http = httpx.Client(base_url=args.url, timeout=120)
    items = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()]
    if args.limit:
        items = items[:args.limit]

    def run_item(item: dict) -> dict:
        response = http.post("/compress", json={"content": item["content"], "context": item["question"]})
        response.raise_for_status()
        compressed = response.json()
        text = compressed["compressed"]
        files = extract_files(item["content"])
        counts = extract_file_counts(item["content"])
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top_file = item["ground_truth"]["top_file"]
        top3_files = [path for path, _ in ranked[:3]]
        preserved_files = sum(1 for path in files if path in text)
        top3_kept = sum(1 for path in top3_files if path in text)
        top1_ok = top_file in text
        top3_ok = top3_kept >= max(1, len(top3_files) - 1)
        return {
            "id": item["id"],
            "kind": item["kind"],
            "strategy": compressed["strategy"],
            "original_tokens": compressed.get("original_tokens", 0),
            "compressed_tokens": compressed.get("compressed_tokens", 0),
            "top_file_ok": top1_ok,
            "top3_files": top3_files,
            "top3_kept": top3_kept,
            "top3_ok": top3_ok,
            "preserved_files": preserved_files,
            "total_files": len(files),
            "preserved_ratio": preserved_files / max(len(files), 1),
            "ok": top1_ok and top3_ok,
        }

    print(f"search deterministic eval start n={len(items)} concurrency={args.concurrency}", flush=True)
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_item, item): item for item in items}
        for idx, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"id": item["id"], "error": repr(exc)})
            if idx % 10 == 0 or idx == len(items):
                print(f"progress {idx}/{len(items)} errors={len(errors)}", flush=True)

    args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows + errors) + "\n")
    ok = sum(row["ok"] for row in rows)
    top1 = sum(row["top_file_ok"] for row in rows)
    top3 = sum(row["top3_ok"] for row in rows)
    avg_in = statistics.mean(row["original_tokens"] for row in rows)
    avg_out = statistics.mean(row["compressed_tokens"] for row in rows)
    reduction = statistics.mean(
        (row["original_tokens"] - row["compressed_tokens"]) / row["original_tokens"]
        for row in rows if row["original_tokens"]
    )
    avg_preserved = statistics.mean(row["preserved_ratio"] for row in rows)
    print(
        f"summary n={len(rows)} ok={ok}/{len(rows)} top1={top1}/{len(rows)} top3={top3}/{len(rows)} "
        f"avg_in={avg_in:.1f} avg_out={avg_out:.1f} reduction={reduction:.1%} "
        f"avg_preserved_files={avg_preserved:.1%}",
        flush=True,
    )
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
