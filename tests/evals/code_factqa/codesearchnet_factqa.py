#!/usr/bin/env python3
from __future__ import annotations

import ast
import re


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def norm_literal(value: str) -> str:
    value = norm(value)
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        value = value[1:-1].strip()
    return value


def literal(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return repr(ast.literal_eval(node))
    except Exception:
        try:
            return ast.unparse(node)
        except Exception:
            return ""


def first_function(code: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def build_case(item: dict) -> dict | None:
    fn = first_function(item["context"])
    if fn is None:
        return None

    args = [arg.arg for arg in fn.args.args]
    defaults = [literal(node) for node in fn.args.defaults]
    default_map = dict(zip(args[-len(defaults):], defaults)) if defaults else {}
    doc = ast.get_docstring(fn) or ""
    doc_first = doc.strip().splitlines()[0].strip() if doc.strip() else ""

    checks: list[dict] = [
        {
            "key": "function_name",
            "question": "What is the function name?",
            "answer": fn.name,
            "type": "exact",
        },
        {
            "key": "parameters",
            "question": "List the function parameters in order, comma-separated.",
            "answer": ", ".join(args),
            "type": "list",
        },
    ]
    if default_map:
        name, value = next(iter(default_map.items()))
        checks.append({
            "key": f"default_{name}",
            "question": f"What is the default value of parameter `{name}`?",
            "answer": value,
            "type": "contains",
        })
    if doc_first:
        checks.append({
            "key": "docstring_first_line",
            "question": "What is the first line of the function docstring?",
            "answer": doc_first,
            "type": "contains",
        })

    return {
        "id": item["id"],
        "code": item["context"],
        "checks": checks[:5],
    }


def grade(answer: str, check: dict) -> bool:
    got = norm(answer)
    expected = norm(check["answer"])
    if check["type"] == "exact":
        return got == expected or expected in got
    if check["type"] == "list":
        expected_parts = [norm(part) for part in check["answer"].split(",")]
        return all(part and part in got for part in expected_parts)
    return expected in got or norm_literal(expected) == norm_literal(got)
