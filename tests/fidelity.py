#!/usr/bin/env python3
"""Strategy fidelity smoke test.

Measures the three benchmark dimensions used in the docs-style evaluation:
compression ratio, factual preservation, and latency. This script does not use
an LLM judge; each case has deterministic ground-truth checks.

Usage:
  python3.12 tests/fidelity.py --url http://localhost:8010
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass

import httpx

DEFAULT_URL = "http://localhost:8010"


@dataclass
class Case:
    name: str
    expected_strategy: str
    content: str
    context: str
    checks: dict[str, str]
    max_ratio: float | None = None


def make_json_fixture() -> str:
    rows = []
    for i in range(100):
        rows.append({
            "timestamp": f"2026-06-19T10:{i:02d}:00Z",
            "service": "payment-api",
            "level": "INFO",
            "event": "request_complete",
            "status": 200,
            "request_id": f"req-{1000 + i}",
            "latency_ms": 20 + i,
        })
    rows[66] = {
        "timestamp": "2026-06-19T10:66:00Z",
        "service": "payment-api",
        "level": "CRITICAL",
        "event": "database_pool_exhausted",
        "error_code": "ERR_DB_POOL_EXHAUSTED",
        "resolution": "increase max_connections to 240",
        "affected_count": 1847,
        "request_id": "req-critical-67",
    }
    return json.dumps(rows)


def make_log_fixture() -> str:
    lines = []
    for i in range(500):
        level = "INFO"
        message = f"GET /api/orders 200 trace_id=tr-{i:04d} latency={30 + i % 70}ms"
        if i == 337:
            level = "ERROR"
            message = "POST /api/charge 500 trace_id=tr-deadbeef error=payment_gateway_timeout"
        lines.append(f"2026-06-19 10:{i//60:02d}:{i%60:02d} {level} [checkout] {message}")
    return "\n".join(lines)


def make_search_fixture() -> str:
    lines = []
    for i in range(300):
        if i == 172:
            lines.append("src/payments/refund.py:418:    raise ValueError('refund amount exceeds capture')")
        else:
            lines.append(f"src/module_{i % 9}/handler.py:{20 + i}:    return handle_event(event_{i})")
    return "\n".join(lines)


def make_code_fixture() -> str:
    methods = []
    for i in range(25):
        methods.append(f"""
    def helper_{i}(self, data: dict) -> dict:
        if not data:
            raise ValueError("missing data")
        result = {{}}
        for key, value in data.items():
            result[key] = value
        return result
""")
    return f"""
class PaymentService:
    def charge(self, user_id: int, amount: int) -> dict:
        if amount <= 0:
            raise ValueError("amount must be positive")
        return {{"status": "charged", "user_id": user_id}}

    def refund(self, tx_id: str, reason: str) -> dict:
        if not tx_id:
            raise ValueError("transaction id required")
        if reason == "fraud":
            return {{"status": "manual_review", "tx_id": tx_id}}
        return {{"status": "refunded", "tx_id": tx_id}}
{''.join(methods)}
"""


def make_diff_fixture() -> str:
    hunks = []
    for i in range(20):
        hunks.append(f"""@@ -{i * 10},6 +{i * 10},8 @@
 def existing_{i}():
     return True
-    old_timeout = 30
+    new_timeout = 45
+    audit_event("timeout_changed")
""")
    return "\n".join([
        "diff --git a/src/auth.py b/src/auth.py",
        "index 1111111..2222222 100644",
        "--- a/src/auth.py",
        "+++ b/src/auth.py",
        "diff --git a/src/payments.py b/src/payments.py",
        "index 3333333..4444444 100644",
        "--- a/src/payments.py",
        "+++ b/src/payments.py",
        *hunks,
    ])


def make_html_fixture() -> str:
    noisy_links = "\n".join(
        f"<a href='/promo/{i}'>Sponsored navigation link {i} pricing login cookie banner</a>"
        for i in range(80)
    )
    return """
<!doctype html>
<html>
  <head><title>Quarterly Reliability Report</title><script>window.ads=true</script></head>
  <body>
    <nav>Home Pricing Login Ads """ + noisy_links + """</nav>
    <aside>
      <p>Advertisement cloud savings webinar subscribe now.</p>
      <p>Cookie settings privacy policy newsletter signup social links.</p>
    </aside>
    <article>
      <h1>Quarterly Reliability Report</h1>
      <p>The payment API reached 99.97 percent availability in Q2.</p>
      <p>The only critical incident was caused by database pool exhaustion.</p>
      <p>The remediation was increasing max_connections to 240 and adding pool alerts.</p>
    </article>
    <footer>Subscribe and cookie settings</footer>
  </body>
</html>
"""


def build_cases() -> list[Case]:
    return [
        Case(
            name="smart_crusher_json_error",
            expected_strategy="smart_crusher",
            content=make_json_fixture(),
            context="find critical error code resolution affected count",
            checks={
                "error_code": "ERR_DB_POOL_EXHAUSTED",
                "resolution": "increase max_connections to 240",
                "affected_count": "1847",
                "request_id": "req-critical-67",
            },
            max_ratio=0.25,
        ),
        Case(
            name="log_error_line",
            expected_strategy="log",
            content=make_log_fixture(),
            context="payment gateway timeout trace id",
            checks={
                "endpoint": "/api/charge",
                "status": "500",
                "trace_id": "tr-deadbeef",
                "error": "payment_gateway_timeout",
            },
            max_ratio=0.50,
        ),
        Case(
            name="search_result_hit",
            expected_strategy="search",
            content=make_search_fixture(),
            context="refund amount exceeds capture",
            checks={
                "file": "src/payments/refund.py",
                "line": "418",
                "exception": "ValueError",
                "message": "refund amount exceeds capture",
            },
            max_ratio=0.40,
        ),
        Case(
            name="code_aware_signatures",
            expected_strategy="code_aware",
            content=make_code_fixture(),
            context="refund charge PaymentService",
            checks={
                "class": "PaymentService",
                "charge": "def charge",
                "refund": "def refund",
                "manual_review": "manual_review",
            },
            max_ratio=1.0,
        ),
        Case(
            name="diff_changed_lines",
            expected_strategy="diff",
            content=make_diff_fixture(),
            context="timeout changed audit_event",
            checks={
                "file": "src/payments.py",
                "new_timeout": "new_timeout = 45",
                "audit": "audit_event",
                "old_timeout": "old_timeout = 30",
            },
            max_ratio=0.75,
        ),
        Case(
            name="html_article_extraction",
            expected_strategy="html",
            content=make_html_fixture(),
            context="payment API reliability incident remediation",
            checks={
                "title": "Quarterly Reliability Report",
                "availability": "99.97 percent availability",
                "incident": "database pool exhaustion",
                "remediation": "max_connections to 240",
            },
            max_ratio=0.90,
        ),
        Case(
            name="text_passthrough",
            expected_strategy="text",
            content="Plain note: the deployment window is 02:00 UTC and owner is platform-team.",
            context="deployment owner",
            checks={
                "window": "02:00 UTC",
                "owner": "platform-team",
            },
            max_ratio=1.0,
        ),
    ]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def run_case(client: httpx.Client, case: Case) -> dict:
    started = time.perf_counter()
    response = client.post("/compress", json={"content": case.content, "context": case.context})
    latency_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    result = response.json()
    compressed = result["compressed"]
    haystack = normalize(compressed)

    check_results = []
    for label, expected in case.checks.items():
        ok = normalize(expected) in haystack
        check_results.append((label, ok))

    strategy_ok = result["strategy"] == case.expected_strategy
    ratio_ok = case.max_ratio is None or result["ratio"] <= case.max_ratio
    passed = sum(1 for _, ok in check_results if ok)
    total = len(check_results)
    return {
        "case": case.name,
        "expected_strategy": case.expected_strategy,
        "strategy": result["strategy"],
        "strategy_ok": strategy_ok,
        "ratio": result["ratio"],
        "ratio_ok": ratio_ok,
        "checks_passed": passed,
        "checks_total": total,
        "latency_ms": latency_ms,
        "failed_checks": [label for label, ok in check_results if not ok],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy fidelity smoke test")
    parser.add_argument("--url", default=DEFAULT_URL, help="service URL")
    args = parser.parse_args()

    rows = []
    with httpx.Client(base_url=args.url, timeout=30) as client:
        client.get("/health").raise_for_status()
        for case in build_cases():
            rows.append(run_case(client, case))

    print(f"\n{'=' * 108}")
    print(f"Strategy fidelity smoke test  {args.url}")
    print(f"{'=' * 108}")
    print(f"{'case':<28} {'strategy':<15} {'checks':>8} {'ratio':>8} {'latency':>10}  result")
    print("-" * 108)
    failures = 0
    for row in rows:
        ok = (
            row["strategy_ok"]
            and row["ratio_ok"]
            and row["checks_passed"] == row["checks_total"]
        )
        failures += 0 if ok else 1
        result = "PASS" if ok else "FAIL"
        if row["failed_checks"]:
            result += " missing=" + ",".join(row["failed_checks"])
        if not row["strategy_ok"]:
            result += f" strategy_expected={row['expected_strategy']}"
        if not row["ratio_ok"]:
            result += " ratio_above_threshold"
        print(
            f"{row['case']:<28} {row['strategy']:<15} "
            f"{row['checks_passed']}/{row['checks_total']:>5} "
            f"{row['ratio']:>7.1%} {row['latency_ms']:>8.1f}ms  {result}"
        )

    print("-" * 108)
    print(f"passed={len(rows) - failures}/{len(rows)}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
