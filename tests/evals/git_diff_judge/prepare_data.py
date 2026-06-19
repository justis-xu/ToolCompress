#!/usr/bin/env python3
"""Generate `data/`: 50 real headroom commits selected by actual compression ratio.

Why this selection method:
- headroom 仓库全部 1634 个 commit 里，超过一半压缩率 <5%（DiffCompressor 只裁剪
  context 行，增删行始终保留，压缩空间天然受 context 行占比限制），在这些 case 上
  测准确率没有意义。
- 不按 diffstat/文件数等代理指标筛选，而是真的把每个 commit 的 diff 发到本地
  /compress 接口跑一遍，按实测压缩率排序取前 50，门槛落在约 18%。

用法（需要本地 ToolCompress 服务已启动）：
  python3 prepare_data.py --repo /path/to/headroom --url http://localhost:8010
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path


DEFAULT_REPO = Path("/Users/xu/git/headroom")
OUT_DIR = Path(__file__).parent / "data"
MAX_DIFF_BYTES = 500_000  # /compress 接口的硬性上限
QUESTION = "列出这次 commit 改动的文件或模块，并用 2-4 条总结核心改动点。忽略格式、注释和无关细节。"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode(errors="ignore")


def compress_ratio(url: str, content: str) -> tuple[float, str] | None:
    data = json.dumps({"content": content, "context": "git diff"}).encode()
    req = urllib.request.Request(f"{url}/compress", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read())
    except Exception:
        return None
    orig = j.get("original_tokens") or 1
    comp = j.get("compressed_tokens") or 0
    return 1 - comp / orig, j.get("strategy", "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the git-diff judge eval dataset from real commit history")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--url", default="http://localhost:8010")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("-n", type=int, default=50, help="保留多少条")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    shas = git(args.repo, "log", "--format=%H").splitlines()
    print(f"scanning {len(shas)} commits in {args.repo}")

    candidates = []
    for i, sha in enumerate(shas, 1):
        diff = git(args.repo, "show", "--no-color", sha)
        if not diff or len(diff) >= MAX_DIFF_BYTES:
            continue
        result = compress_ratio(args.url, diff)
        if result is None:
            continue
        ratio, strategy = result
        if strategy != "diff":
            continue
        candidates.append((sha, ratio, diff))
        if i % 200 == 0:
            print(f"  scanned {i}/{len(shas)}, candidates so far: {len(candidates)}")

    candidates.sort(key=lambda c: -c[1])
    top = candidates[:args.n]
    print(f"selected top {len(top)} by ratio, range {top[-1][1]:.1%}..{top[0][1]:.1%}")

    rows = []
    for sha, ratio, diff in top:
        short = sha[:8]
        filename = f"diff_{short}.diff"
        (args.out / filename).write_text(diff)
        files_changed = [
            line.split(" ")[2][2:]
            for line in diff.splitlines()
            if line.startswith("diff --git a/")
        ]
        rows.append({
            "id": short,
            "sha": sha,
            "file": filename,
            "bytes": len(diff.encode()),
            "files_changed": files_changed,
            "question": QUESTION,
            "ratio_at_build_time": ratio,
        })

    rows.sort(key=lambda r: r["id"])
    (args.out / "meta.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    )
    print(f"wrote {len(rows)} cases -> {args.out}")


if __name__ == "__main__":
    main()
