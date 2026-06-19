#!/usr/bin/env python3
"""Deterministic log benchmark.

Why deterministic:
- `log` 策略的职责是保留 ERROR/WARNING/CRITICAL 等异常行，不是回答问题，且压缩后
  异常行是逐字保留（不会被模型改写），所以用确定性的子串匹配检查比 LLM judge 更
  适合，也更稳定。

Dataset:
- `data/`，来自 LogHub 公开数据集（OpenStack/Apache/Hadoop/HDFS/Linux/
  Zookeeper/Mac 共 7 个来源的 *_2k.log 样本）
- 每条样本是一段 80 行的真实日志窗口，且至少包含 1 条 ERROR/WARNING/CRITICAL/
  FATAL/FAIL 行（`meta.jsonl` 的 `anchor_lines` 字段是 ground truth）
- 由 `prepare_data.py` 生成（窗口按数据源非重叠切分，过滤掉不含异常行的窗口后
  随机抽样到 100 条）

Scoring:
- LogCompressor 会对重复出现的同一异常模板去重（同一条错误反复出现几十次时只保留
  代表性的几条），这是设计行为，不是丢信息——按"原始异常行"逐条比对子串会把这种
  去重错误地记成"丢失"。因此按"异常模板"（把数字/时间戳归一化后的行特征）去重后
  再比对：只要该模板在压缩结果里还有至少一条代表行存活，就算保留。
- `anchor_template_preserved`: 该窗口里有多少种异常模板在压缩后仍有代表行存活
- `ok`: 全部异常模板都保留（这是 log 策略最基本的承诺：不能整类错误信息消失）
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx


_NUM_RE = re.compile(r"\d+")


def _template(line: str) -> str:
    return _NUM_RE.sub("#", line)


DATA_DIR = Path(__file__).parent / "data"
META = DATA_DIR / "meta.jsonl"
OUT = Path("/tmp/toolcompress-eval-log-deterministic.jsonl")
URL = "http://localhost:8010"


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic log compression evaluation")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args()

    http = httpx.Client(base_url=args.url, timeout=120)
    meta = args.data_dir / "meta.jsonl"
    rows = [json.loads(line) for line in meta.read_text().splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]

    def run_item(row: dict) -> dict:
        content = (args.data_dir / row["file"]).read_text(errors="ignore")
        response = http.post("/compress", json={"content": content, "context": row["context"]})
        response.raise_for_status()
        compressed = response.json()
        text = compressed["compressed"]
        text_templates = {_template(line) for line in text.splitlines()}
        anchor_templates = {_template(line) for line in row["anchor_lines"]}
        preserved = sum(1 for t in anchor_templates if t in text_templates)
        return {
            "id": row["id"],
            "source": row["source"],
            "strategy": compressed["strategy"],
            "original_tokens": compressed.get("original_tokens", 0),
            "compressed_tokens": compressed.get("compressed_tokens", 0),
            "anchor_template_total": len(anchor_templates),
            "anchor_template_preserved": preserved,
            "ok": preserved == len(anchor_templates),
        }

    print(f"log deterministic eval start n={len(rows)} concurrency={args.concurrency}", flush=True)
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_item, row): row for row in rows}
        for idx, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({"id": row["id"], "error": repr(exc)})
            if idx % 10 == 0 or idx == len(rows):
                print(f"progress {idx}/{len(rows)} errors={len(errors)}", flush=True)

    args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in results + errors) + "\n")
    ok = sum(row["ok"] for row in results)
    avg_in = statistics.mean(row["original_tokens"] for row in results)
    avg_out = statistics.mean(row["compressed_tokens"] for row in results)
    reduction = statistics.mean(
        (row["original_tokens"] - row["compressed_tokens"]) / row["original_tokens"]
        for row in results if row["original_tokens"]
    )
    avg_anchor = statistics.mean(
        row["anchor_template_preserved"] / row["anchor_template_total"]
        for row in results if row["anchor_template_total"]
    )
    print(
        f"summary n={len(results)} ok={ok}/{len(results)} "
        f"avg_in={avg_in:.1f} avg_out={avg_out:.1f} reduction={reduction:.1%} "
        f"avg_anchor_template_preserved={avg_anchor:.1%}",
        flush=True,
    )
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
