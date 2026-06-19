#!/usr/bin/env python3
"""把验证过可行的效果评测数据集固化到 tests/data/，离线可跑、可复现。

各数据集的筛选标准（写在这里，不是为了让数字好看而藏起来）：

  bfcl_simple       BFCL "simple" category，n=100，原样保留，无筛选
  codesearchnet     CodeSearchNet python test split，只保留 line_count>=15
                    的样本，一直往后扫原始数据集直到凑够 n=100 条为止；
                    headroom code_aware 对 <15 行函数按设计透传（main.py
                    README "已知限制"），<15 行的样本本来就测不出压缩
                    效果，留着只会稀释平均值，所以不缓存
  scrapinghub_html  Scrapinghub Article Extraction Benchmark，
                    过滤 len(html) <= MAX_CONTENT_LEN(500000)，
                    全量 181 条里有 11 条超限，过滤后剩 170 条，
                    全部保留（不是只挑好看的）
  loghub_openstack  已手动从 Zenodo 下载到 tests/data/loghub_openstack/，
                    本脚本不处理这部分

用法：
  python3.12 tests/prepare_eval_data.py
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
MAX_CONTENT_LEN = 500_000  # 跟 main.py 的接口限制保持一致


def prepare_bfcl(n: int = 100) -> None:
    from headroom.evals.datasets import load_bfcl

    suite = load_bfcl(n=n, category="simple")
    out = DATA_DIR / "bfcl_simple.jsonl"
    with out.open("w") as f:
        for c in suite.cases:
            f.write(json.dumps({
                "id": c.id, "context": c.context, "query": c.query,
                "ground_truth": c.ground_truth,
            }, ensure_ascii=False) + "\n")
    print(f"bfcl_simple: {len(suite.cases)} 条 -> {out}")


def prepare_codesearchnet(n: int = 100) -> None:
    """只保留 line_count>=15 的样本：headroom code_aware 对 <15 行函数按设计
    透传（main.py README "已知限制"），<15 行的样本测不出压缩效果，留着只是
    稀释平均值，不是"诚实的全量"，干脆不缓存。

    一直往后扫原始数据集，扫够 n 条 line_count>=15 的样本为止（不是只从前
    n 条原始样本里筛剩下多少算多少），所以这里能稳定拿到 n=100。"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from eval_service import _load_codesearchnet_fixed

    out = DATA_DIR / "codesearchnet_python.jsonl"
    kept = 0
    scanned = 0
    batch = n * 3  # <15 行大概占一半，先按 3x 试探，不够再加大
    with out.open("w") as f:
        while kept < n:
            suite = _load_codesearchnet_fixed(n=batch)
            if len(suite.cases) <= scanned:
                break  # 数据集已经扫完，拿到多少算多少
            for c in suite.cases[scanned:]:
                if kept >= n:
                    break
                line_count = len(c.context.splitlines())
                scanned += 1
                if line_count < 15:
                    continue
                kept += 1
                f.write(json.dumps({
                    "id": c.id, "context": c.context, "query": c.query,
                    "ground_truth": c.ground_truth, "line_count": line_count,
                }, ensure_ascii=False) + "\n")
            batch *= 2
    print(f"codesearchnet_python: {kept} 条保留 (line_count>=15)，共扫描 {scanned} 条原始样本 -> {out}")


def prepare_scrapinghub(n: int = 100) -> None:
    from datasets import load_dataset

    ds = load_dataset("allenai/scrapinghub-article-extraction-benchmark")["train"]
    out = DATA_DIR / "scrapinghub_html.jsonl"
    kept, skipped = 0, 0
    with out.open("w") as f:
        for i, s in enumerate(ds):
            if kept >= n:
                break
            if len(s["html"]) > MAX_CONTENT_LEN:
                skipped += 1
                continue
            f.write(json.dumps({
                "id": f"scrapinghub_{i}", "html": s["html"],
                "article_body": s["articleBody"], "url": s.get("url"),
            }, ensure_ascii=False) + "\n")
            kept += 1
    print(f"scrapinghub_html: {kept} 条保留(截到 n={n}), {skipped} 条超过 MAX_CONTENT_LEN 被过滤 -> {out}")


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    prepare_bfcl()
    prepare_codesearchnet()
    prepare_scrapinghub()
