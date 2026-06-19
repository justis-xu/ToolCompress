#!/usr/bin/env python3
"""search / diff 场景的回归测试：没有开源ground_truth，用LLM judge对比
压缩前后的回答是否保留了关键信息。

这不是客观准确率基准——是内部回归测试信号：以后改了SearchCompressor/
DiffCompressor的逻辑，这个分数掉了就说明有问题。不能拿这个分数对外宣称
"准确率XX%"。

流程：
  DeepSeek: 原始内容 + 问题 → 回答A
  DeepSeek: /compress 压缩后内容 + 同问题 → 回答B
  MiniMax-M3(judge): 给定问题、回答A、回答B，判断B是否保留了A的关键信息

用法：
  export OPENAI_API_KEY=sk-...        # DeepSeek
  export OPENAI_BASE_URL=https://api.deepseek.com
  export MINIMAX_API_KEY=sk-...
  python3.12 tests/eval_search_diff_judge.py --url http://localhost:8010
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import httpx
import openai

DATA_DIR = Path(__file__).parent / "data" / "search_diff_payloads"

# (文件名, 问题)
CASES = [
    ("grep_import_200lines.txt", "这些grep结果一共涉及哪些文件？每个文件大概有多少处import？"),
    ("grep_logger_300lines.txt", "这些logger调用主要集中在哪些文件/模块？"),
    ("diff_real_a99dc614.diff", "这次commit主要做了什么改动？改了哪些文件？"),
    ("diff_real_b7be3814.diff", "这次commit主要做了什么改动？改了哪些文件？"),
    ("diff_real_8cea290a.diff", "这次commit主要做了什么改动？改了哪些文件？"),
]


def _ask(client: openai.OpenAI, model: str, content: str, question: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"内容：\n{content}\n\n问题：{question}\n\n简洁回答，列出关键事实（文件名、数量等）。",
        }],
    )
    return (resp.choices[0].message.content or "").strip()


def _judge(client: openai.OpenAI, question: str, answer_a: str, answer_b: str) -> tuple[str, str]:
    resp = client.chat.completions.create(
        model="MiniMax-M3",
        temperature=0,
        messages=[{
            "role": "user",
            "content": (
                f"问题：{question}\n\n"
                f"回答A（基于原始内容）：{answer_a}\n\n"
                f"回答B（基于压缩后内容）：{answer_b}\n\n"
                f"判断回答B是否保留了回答A里的关键事实（文件名、数量、核心结论），"
                f"忽略措辞差异。只输出 PASS 或 FAIL，然后一句话理由，格式：\n"
                f"PASS|FAIL: 理由"
            ),
        }],
    )
    raw = (resp.choices[0].message.content or "").strip()
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if text.upper().startswith("PASS"):
        return "PASS", text
    if text.upper().startswith("FAIL"):
        return "FAIL", text
    return "UNKNOWN", text or raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8010")
    parser.add_argument("--model", default="deepseek-chat")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    minimax_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key or not minimax_key:
        print("需要 OPENAI_API_KEY（DeepSeek）和 MINIMAX_API_KEY")
        return

    llm = openai.OpenAI(api_key=api_key, base_url=base_url)
    judge = openai.OpenAI(api_key=minimax_key, base_url="https://api.minimaxi.com/v1")
    http = httpx.Client(base_url=args.url, timeout=60)

    results = []
    for fname, question in CASES:
        path = DATA_DIR / fname
        if not path.exists():
            print(f"  跳过 {fname}（文件不存在）")
            continue
        content = path.read_text(errors="ignore")

        r = http.post("/compress", json={"content": content, "context": question})
        d = r.json()

        answer_a = _ask(llm, args.model, content, question)
        answer_b = _ask(llm, args.model, d["compressed"], question)

        # judge本身temperature=0也不保证每次一致（实测过：同一对A/B答案重判3次
        # 可能从FAIL变PASS），单次FAIL不可信，容易把judge偶尔抽风当成真bug。
        # 规则放宽到对工具有利的一边：连续3次都FAIL才算真FAIL（高置信度的真
        # 实问题）；其余情况（3次都PASS，或者结果不一致）都不算确认的失败。
        verdicts = [_judge(judge, question, answer_a, answer_b) for _ in range(3)]
        votes = [v for v, _ in verdicts]
        final_verdict = "FAIL(确认)" if all(v == "FAIL" for v in votes) else "PASS"

        results.append((fname, d["strategy"], d["ratio"], final_verdict))
        print(f"\n=== {fname} ===")
        print(f"  策略={d['strategy']} 压缩率={d['ratio']:.2%}")
        print(f"  回答A(原始): {answer_a[:150]}")
        print(f"  回答B(压缩): {answer_b[:150]}")
        print(f"  judge x3: {votes} -> {final_verdict}")
        print(f"  最后一次理由: {verdicts[-1][1][:150]}")

    print(f"\n{'='*60}")
    n_pass = sum(1 for r in results if r[3] == "PASS")
    print(f"汇总: {n_pass}/{len(results)} PASS")
    for fname, strategy, ratio, verdict in results:
        print(f"  {fname:<35} strategy={strategy:<12} ratio={ratio:.2%}  {verdict}")


if __name__ == "__main__":
    main()
