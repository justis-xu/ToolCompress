#!/usr/bin/env python3
"""功能验证脚本 — 覆盖所有端点、内容类型、边界情况。

用法：
  mise run verify
  python3.12 tests/verify/verify.py --url http://localhost:8010
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmark"))
from benchmark import DIFF_30 as BENCH_DIFF_30, DIFF_100 as BENCH_DIFF_100

DEFAULT_URL = "http://localhost:8010"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

_failures = 0
_total    = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _failures, _total
    _total += 1
    if ok:
        print(f"  {PASS} {name}")
    else:
        _failures += 1
        print(f"  {FAIL} {name}" + (f"  → {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def compress(client: httpx.Client, content: str, context: str = "") -> dict:
    r = client.post("/compress", json={"content": content, "context": context})
    r.raise_for_status()
    return r.json()


def compress_image(client: httpx.Client, b64: str, **kwargs) -> dict:
    r = client.post("/compress/image", json={"image": b64, **kwargs})
    r.raise_for_status()
    return r.json()


def make_image(w: int = 1024, h: int = 768, fmt: str = "PNG") -> bytes:
    img = Image.new("RGB", (w, h), color=(100, 149, 237))
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ── fixtures ──────────────────────────────────────────────────────────────────

# 500 条订单记录，结构相同 → SmartCrusher 高度压缩（预期 ratio < 5%）
JSON_EN = json.dumps([
    {
        "order_id": f"ORD-{1000+i}",
        "user_id": 100 + (i % 50),
        "product": ["laptop", "phone", "tablet", "monitor", "keyboard"][i % 5],
        "qty": 1 + i % 4,
        "price": round(299.99 + (i % 10) * 50, 2),
        "status": "shipped" if i % 7 != 0 else "failed",
        "created_at": f"2024-{(i % 12)+1:02d}-{(i % 28)+1:02d}T{(i % 24):02d}:00:00Z",
    }
    for i in range(500)
])

# 300 条告警记录，中文，结构相同 → 高压缩（预期 ratio < 40%）
JSON_ZH = json.dumps([
    {
        "告警id": f"ALT-{2000+i}",
        "服务": ["支付服务", "订单服务", "用户服务", "库存服务"][i % 4],
        "级别": "严重" if i % 15 == 0 else ("警告" if i % 5 == 0 else "正常"),
        "消息": f"数据库连接超时，重试第{i % 3 + 1}次" if i % 15 == 0 else (
            f"响应时间{200 + i % 300}ms 超过阈值" if i % 5 == 0 else f"请求处理完成，耗时{50 + i % 100}ms"
        ),
        "时间": f"2024-03-{(i % 28)+1:02d}T{(i % 24):02d}:{(i * 2 % 60):02d}:00Z",
    }
    for i in range(300)
], ensure_ascii=False)

# 1000 行应用日志，INFO/ERROR/WARN 格式，极重复 → 高压缩（预期 ratio < 10%）
_endpoints = ["/api/charge", "/api/orders", "/api/users", "/api/refund", "/api/health"]
LOG_EN = "\n".join(
    f'2024-03-{(i//86400)%28+1:02d} {(i//3600)%24:02d}:{(i//60)%60:02d}:{i%60:02d}.{i%1000:03d} '
    f'{"ERROR" if i%50==0 else "WARN" if i%15==0 else "INFO"} [payment-svc] '
    f'{"db connection timeout after 30s, retrying" if i%50==0 else ("slow query 850ms exceeds threshold 500ms" if i%15==0 else f"POST {_endpoints[i%5]} 200 {20+i%180}ms")}'
    for i in range(1000)
)

# 800 行应用日志，中文，含大量 ERROR → 高压缩（预期 ratio < 20%）
_services = ["支付", "订单", "用户", "库存", "通知"]
_errors = [
    "数据库连接池耗尽，等待超时",
    "Redis 写入失败：连接拒绝",
    "下游服务调用超时：payment-svc",
    "消息队列积压，消费者停止",
]
LOG_ZH = "\n".join(
    f"2024-03-01 {(i//3600)%24:02d}:{(i//60)%60:02d}:{i%60:02d}.{i%1000:03d} "
    f"{'ERROR' if i%20==0 else 'WARN' if i%7==0 else 'INFO'} "
    f"[{_services[i%5]}服务] "
    f"{_errors[i%4] if i%20==0 else (f'请求超时，重试中 attempt={i%3+1}' if i%7==0 else f'处理完成 traceId={hex(i)[2:].zfill(8)} cost={i%200}ms')}"
    for i in range(800)
)

# PaymentService：8 个方法，每个方法体 18+ 行 → CodeAwareCompressor（预期 ratio < 30%）
CODE_EN = '''
from typing import Optional, Dict, List
from decimal import Decimal

class PaymentService:
    def __init__(self, db, cache, fx_client, fee_engine, notifier, audit):
        self.db = db
        self.cache = cache
        self.fx = fx_client
        self.fee = fee_engine
        self.notify = notifier
        self.audit = audit

    def charge(self, user_id: int, amount: Decimal, currency: str, method: str) -> Dict:
        if amount <= 0:
            raise ValueError("amount must be positive")
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError(f"user {user_id} not found")
        rate = self.fx.rate(currency, "USD")
        usd = amount * Decimal(str(rate))
        fee = self.fee.calc(usd, method)
        if user["balance"] < amount + fee:
            raise ValueError("insufficient balance")
        tx_id = self.db.create_txn(user_id, amount, currency, fee, method)
        self.db.deduct_balance(user_id, amount + fee)
        self.db.complete_txn(tx_id)
        self.cache.delete(f"user:{user_id}:balance")
        self.notify.send(user_id, f"Payment {amount} {currency} processed, fee {fee}")
        self.audit.log("charge", user_id, tx_id, float(amount))
        return {"tx_id": tx_id, "amount": str(amount), "currency": currency, "fee": str(fee), "status": "completed"}

    def refund(self, tx_id: int, reason: str) -> Dict:
        tx = self.db.get_txn(tx_id)
        if not tx:
            raise ValueError(f"transaction {tx_id} not found")
        if tx["status"] != "completed":
            raise ValueError(f"cannot refund status={tx['status']}")
        if self.db.find_refund(tx_id):
            raise ValueError(f"transaction {tx_id} already refunded")
        refund_id = self.db.create_refund(tx_id, tx["amount"], reason)
        self.db.restore_balance(tx["user_id"], tx["amount"] + tx["fee"])
        self.db.complete_refund(refund_id)
        self.db.mark_txn_refunded(tx_id)
        self.cache.delete(f"user:{tx['user_id']}:balance")
        self.notify.send(tx["user_id"], f"Refund {tx['amount']} {tx['currency']} processed")
        self.audit.log("refund", tx["user_id"], tx_id, float(tx["amount"]))
        return {"refund_id": refund_id, "tx_id": tx_id, "amount": str(tx["amount"]), "status": "completed"}

    def get_history(self, user_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
        cache_key = f"txns:{user_id}:{limit}:{offset}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        rows = self.db.list_txns(user_id, limit, offset)
        result = [{"tx_id": r["id"], "amount": str(r["amount"]), "currency": r["currency"],
                   "fee": str(r["fee"]), "method": r["method"], "status": r["status"],
                   "created_at": r["created_at"].isoformat()} for r in rows]
        self.cache.set(cache_key, result, ttl=60)
        return result

    def get_balance(self, user_id: int, currency: str = "USD") -> Decimal:
        cache_key = f"user:{user_id}:balance:{currency}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return Decimal(str(cached))
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError(f"user {user_id} not found")
        balance = Decimal(str(user["balance"]))
        if user["base_currency"] != currency:
            rate = self.fx.rate(user["base_currency"], currency)
            balance = balance * Decimal(str(rate))
        self.cache.set(cache_key, str(balance), ttl=30)
        return balance
'''

# 订单服务，中文，6 个方法，每个方法体 15+ 行 → CodeAwareCompressor（预期 ratio < 40%）
CODE_ZH = '''
import logging
from typing import Optional, Dict, List
from decimal import Decimal

logger = logging.getLogger(__name__)

class 订单服务:
    """管理订单的创建、支付、取消、查询等操作。"""

    def __init__(self, 数据库, 缓存, 支付客户端, 库存客户端, 通知器):
        self.db = 数据库
        self.cache = 缓存
        self.pay = 支付客户端
        self.stock = 库存客户端
        self.notify = 通知器

    def 创建订单(self, 用户id: int, 商品列表: List[Dict], 收货地址: str) -> Dict:
        """创建新订单，检查库存并锁定。"""
        if not 商品列表:
            raise ValueError("商品列表不能为空")
        用户 = self.db.get("SELECT * FROM users WHERE id=%s", 用户id)
        if not 用户:
            raise ValueError(f"用户 {用户id} 不存在")
        总金额 = Decimal("0")
        for 商品 in 商品列表:
            库存 = self.stock.查询(商品["商品id"])
            if 库存["数量"] < 商品["数量"]:
                raise ValueError(f"商品 {商品['商品id']} 库存不足")
            总金额 += Decimal(str(商品["单价"])) * 商品["数量"]
        订单id = self.db.insert(
            "INSERT INTO orders(user_id,amount,address,status) VALUES(%s,%s,%s,'待支付')",
            用户id, 总金额, 收货地址,
        )
        for 商品 in 商品列表:
            self.stock.锁定(商品["商品id"], 商品["数量"], 订单id)
            self.db.insert(
                "INSERT INTO order_items(order_id,product_id,qty,price) VALUES(%s,%s,%s,%s)",
                订单id, 商品["商品id"], 商品["数量"], 商品["单价"],
            )
        self.cache.delete(f"user:{用户id}:orders")
        self.notify.发送(用户id, f"订单 {订单id} 创建成功，总金额 {总金额} 元")
        return {"订单id": 订单id, "总金额": str(总金额), "状态": "待支付"}

    def 支付订单(self, 订单id: int, 支付方式: str) -> Dict:
        """发起支付并更新订单状态。"""
        订单 = self.db.get("SELECT * FROM orders WHERE id=%s", 订单id)
        if not 订单:
            raise ValueError(f"订单 {订单id} 不存在")
        if 订单["status"] != "待支付":
            raise ValueError(f"订单状态 {订单['status']} 不可支付")
        结果 = self.pay.扣款(订单["user_id"], Decimal(str(订单["amount"])), "CNY", 支付方式)
        self.db.exec(
            "UPDATE orders SET status='已支付', tx_id=%s WHERE id=%s",
            结果["tx_id"], 订单id,
        )
        self.cache.delete(f"user:{订单['user_id']}:orders")
        self.cache.delete(f"order:{订单id}")
        self.notify.发送(订单["user_id"], f"订单 {订单id} 支付成功")
        return {"订单id": 订单id, "状态": "已支付", "交易id": 结果["tx_id"]}

    def 取消订单(self, 订单id: int, 原因: str) -> bool:
        """取消订单并释放库存。"""
        订单 = self.db.get("SELECT * FROM orders WHERE id=%s", 订单id)
        if not 订单:
            return False
        if 订单["status"] not in ("待支付", "已支付"):
            raise ValueError(f"订单状态 {订单['status']} 不可取消")
        if 订单["status"] == "已支付":
            self.pay.退款(订单["tx_id"], 原因)
        商品列表 = self.db.query("SELECT * FROM order_items WHERE order_id=%s", 订单id)
        for 商品 in 商品列表:
            self.stock.释放(商品["product_id"], 商品["qty"], 订单id)
        self.db.exec("UPDATE orders SET status='已取消' WHERE id=%s", 订单id)
        self.cache.delete(f"user:{订单['user_id']}:orders")
        self.cache.delete(f"order:{订单id}")
        self.notify.发送(订单["user_id"], f"订单 {订单id} 已取消")
        return True
'''

# 500 行 grep 搜索结果，含 ValueError / TypeError → 高压缩（预期 ratio < 15%）
_files = ["src/api/handler.py", "src/service/payment.py", "src/model/order.py",
          "src/utils/validator.py", "src/db/connection.py"]
_exc   = ["ValueError", "TypeError", "KeyError", "RuntimeError"]
SEARCH_EN = "\n".join(
    f"{_files[i%5]}:{10+i*3}:    raise {_exc[i%4]}(f'invalid input: {{req[{chr(97+i%5)}]}}')"
    if i % 6 == 0 else
    f"{_files[i%5]}:{10+i*3}:    result = db.query(sql, *params)"
    if i % 3 == 0 else
    f"{_files[i%5]}:{10+i*3}:    return jsonify(data, status={200+i%3*100})"
    for i in range(500)
)

# 400 行中文 grep 搜索结果，.py 路径格式，含认证/支付关键词 → 高压缩（预期 ratio < 20%）
_zh_files = ["src/auth/login.py", "src/pay/charge.py", "src/order/create.py", "src/user/profile.py"]
SEARCH_ZH = "\n".join(
    f"{_zh_files[i%4]}:{5+i*2}:    {'raise AuthError(\"认证失败：token 已过期，请重新登录\")' if i%8==0 else ('raise PaymentError(\"支付失败：余额不足\")' if i%5==0 else f'return Response(data[:{ i%100 }])')}"
    for i in range(400)
)

GIT_DIFF = "\n".join([
    "diff --git a/src/auth.py b/src/auth.py",
    "index 1234567..abcdefg 100644",
    "--- a/src/auth.py",
    "+++ b/src/auth.py",
] + [
    f"@@ -{i*10},6 +{i*10},7 @@\n"
    + " ".join(f"    process({j})" for j in range(5))
    + f"\n-    old_logic_{i}()\n+    new_logic_{i}()\n+    log_change_{i}()"
    for i in range(20)
])

HTML_CONTENT = """<!DOCTYPE html>
<html><head><title>Understanding Machine Learning</title>
<meta name="description" content="A comprehensive guide">
<style>body{font-family:sans-serif} nav{background:#333} .ad{display:none}</style>
</head>
<body>
<nav><a href="/">Home</a><a href="/ml">ML</a><a href="/ai">AI</a><a href="/about">About</a></nav>
<div class="ad">Buy our course! Limited time offer! Click here now!</div>
<article>
  <h1>Understanding Machine Learning: A Practical Guide</h1>
  <p>Machine learning is a subset of artificial intelligence that enables systems to learn and improve
  from experience without being explicitly programmed. It focuses on developing computer programs that
  can access data and use it to learn for themselves.</p>
  <p>The process begins with observations or data, such as examples, direct experience, or instruction.
  The goal is to allow computers to learn automatically without human intervention and adjust actions
  accordingly. Machine learning algorithms are trained on large datasets to make predictions or
  decisions without being explicitly programmed to perform the task.</p>
  <p>There are three main types of machine learning: supervised learning, unsupervised learning, and
  reinforcement learning. Supervised learning uses labeled training data. Unsupervised learning finds
  hidden patterns in unlabeled data. Reinforcement learning trains agents to make sequences of
  decisions by rewarding desired behaviors.</p>
  <p>Deep learning is a subset of machine learning that uses neural networks with many layers to
  progressively extract higher-level features from raw input data. For example, in image processing,
  lower layers may identify edges, while higher layers may identify concepts relevant to humans.</p>
</article>
<aside>Related: Deep Learning, Neural Networks, AI Ethics</aside>
<div class="ad">Subscribe to our newsletter! Get updates!</div>
<footer>Copyright 2024 ML Guide. All rights reserved. Privacy Policy. Terms of Service.</footer>
</body></html>"""

PLAIN_TEXT = "This is plain natural language text. " * 50


def run_verify(base: str) -> None:
    client = httpx.Client(base_url=base, timeout=30)

    # ── 1. Health ─────────────────────────────────────────────────────────────
    section("1. 健康检查")
    r = client.get("/health")
    check("GET /health → 200", r.status_code == 200)
    check("返回 {status: ok}", r.json().get("status") == "ok")

    # ── 2. 文本压缩 - 各内容类型 ────────────────────────────────────────────────
    section("2. 文本压缩 — 内容类型路由")

    cases = [
        # (名称, 内容, context, 期望策略, 最大ratio)
        ("JSON 英文 500条",   JSON_EN,      "failed orders",  "smart_crusher", 0.10),
        ("JSON 中文 300条",   JSON_ZH,      "严重告警",         "smart_crusher", 0.50),
        ("日志 英文 1000行",  LOG_EN,       "500 error",      "log",           0.15),
        ("日志 中文 800行",   LOG_ZH,       "数据库连接失败",   "log",           0.30),
        ("代码 英文",         CODE_EN,      "payment charge", None,            0.70),
        ("代码 中文",         CODE_ZH,      "创建订单",         None,            0.70),
        ("搜索 英文 500行",   SEARCH_EN,    "ValueError",     None,            0.20),
        ("搜索 中文 400行",   SEARCH_ZH,    "认证失败",         None,            0.30),
        ("Git diff",          GIT_DIFF,     "auth change",    "diff",          0.80),
        ("Benchmark diff 30", BENCH_DIFF_30,"auth change",    "diff",          0.90),
        ("Benchmark diff 100",BENCH_DIFF_100,"auth change",   "diff",          0.90),
        ("HTML",              HTML_CONTENT, "machine learning","html",         0.80),
    ]

    for name, content, context, expected_strategy, max_ratio in cases:
        try:
            d = compress(client, content, context)
            strategy_ok = expected_strategy is None or d["strategy"] == expected_strategy
            ratio_ok    = d["ratio"] <= max_ratio
            compressed_nonempty = len(d["compressed"]) > 0
            check(
                f"{name}: strategy={d['strategy']} ratio={d['ratio']:.1%}",
                strategy_ok and ratio_ok and compressed_nonempty,
                f"expected strategy={expected_strategy} ratio<={max_ratio:.0%}, got ratio={d['ratio']:.1%}",
            )
        except Exception as e:
            check(f"{name}", False, str(e))

    # ── 3. 文本压缩 - 响应字段完整性 ────────────────────────────────────────────
    section("3. 响应字段完整性")
    d = compress(client, JSON_EN, "find errors")
    check("有 compressed 字段",        "compressed" in d)
    check("有 strategy 字段",          "strategy" in d)
    check("有 original_chars 字段",    "original_chars" in d)
    check("有 compressed_chars 字段",  "compressed_chars" in d)
    check("有 original_tokens 字段",   "original_tokens" in d)
    check("有 compressed_tokens 字段", "compressed_tokens" in d)
    check("有 ratio 字段",             "ratio" in d)
    check("original_chars > 0",       d.get("original_chars", 0) > 0)
    check("compressed_chars > 0",     d.get("compressed_chars", 0) > 0)
    check("original_tokens > 0",      d.get("original_tokens", 0) > 0)
    check("compressed_tokens > 0",    d.get("compressed_tokens", 0) > 0)
    check("ratio 是 float",           isinstance(d.get("ratio"), float))
    check("ratio 在 (0, 1]",          0 < d.get("ratio", 0) <= 1.0,
          f"got {d.get('ratio')}")

    # ── 4. context 对压缩效果的影响 ──────────────────────────────────────────────
    section("4. context 影响（搜索/日志）")
    d_with    = compress(client, SEARCH_ZH, "认证失败")
    d_without = compress(client, SEARCH_ZH, "")
    check("有 context 时命中关键词更多",
          "认证失败" in d_with["compressed"],
          "compressed 里没找到关键词")
    check("有 context 时 ratio 更低",
          d_with["ratio"] <= d_without["ratio"] + 0.1,
          f"with={d_with['ratio']:.2%} without={d_without['ratio']:.2%}")

    # ── 5. 批量文本压缩 ───────────────────────────────────────────────────────
    section("5. 批量文本压缩 /compress/batch")
    batch_req = {"items": [
        {"content": JSON_EN,   "context": "find errors"},
        {"content": LOG_EN,    "context": "connection error"},
        {"content": SEARCH_EN, "context": "ValueError"},
        {"content": SEARCH_ZH, "context": "认证失败"},
    ]}
    r = client.post("/compress/batch", json=batch_req)
    check("batch → 200", r.status_code == 200)
    bd = r.json()
    check("results 数量正确", len(bd.get("results", [])) == 4)
    check("每条都有 compressed", all("compressed" in x for x in bd.get("results", [])))
    check("每条都有 ratio",      all("ratio" in x for x in bd.get("results", [])))
    ratios = [x["ratio"] for x in bd.get("results", [])]
    check("所有 ratio 在 (0,1]", all(0 < r <= 1.0 for r in ratios),
          f"ratios={ratios}")

    # ── 6. 批量边界 ───────────────────────────────────────────────────────────
    section("6. 批量边界情况")
    r1 = client.post("/compress/batch", json={"items": [{"content": "hello"}]})
    check("batch 单条 → 200", r1.status_code == 200)

    r2 = client.post("/compress/batch", json={"items": []})
    check("batch 空列表 → 422", r2.status_code == 422)

    # ── 7. 图片压缩 ───────────────────────────────────────────────────────────
    section("7. 图片压缩 /compress/image")
    img_data = make_image(1536, 1024)  # 3×2 tiles → resize to 768px → 2×1 tiles，token 减少
    img_b64  = b64(img_data)

    d = compress_image(client, img_b64)
    check("图片压缩 → 200",          True)
    check("默认 mode=full_low",      d.get("mode") == "full_low")
    check("media_type = image/jpeg", d.get("media_type") == "image/jpeg")
    check("compressed 非空",         len(d.get("compressed", "")) > 0)
    check("compressed_size < original_size",
          d.get("compressed_size", 0) < d.get("original_size", 1))
    check("ratio < 1.0",             d.get("ratio", 1.0) < 1.0,
          f"ratio={d.get('ratio')}")
    check("original_tokens > 0",     d.get("original_tokens", 0) > 0)
    check("compressed_tokens > 0",   d.get("compressed_tokens", 0) > 0)
    check("压缩后 token 减少",
          d.get("compressed_tokens", 999) < d.get("original_tokens", 0),
          f"orig={d.get('original_tokens')} comp={d.get('compressed_tokens')}")

    # max_dimension 参数
    d512 = compress_image(client, img_b64, max_dimension=512)
    d256 = compress_image(client, img_b64, max_dimension=256)
    check("max_dimension=512 与默认 full_low 一致",
          d512["compressed_size"] == d["compressed_size"],
          f"512={d512['compressed_size']} default={d['compressed_size']}")
    check("max_dimension=256 比512更小",
          d256["compressed_size"] < d512["compressed_size"])

    dkeep = compress_image(client, img_b64, mode="preserve", max_dimension=768, quality=85)
    check("显式 preserve mode 回显", dkeep.get("mode") == "preserve")
    check("默认 full_low 比 preserve token 更少",
          d["compressed_tokens"] < dkeep["compressed_tokens"],
          f"full_low={d['compressed_tokens']} preserve={dkeep['compressed_tokens']}")
    check("默认 full_low 比 preserve 文件更小",
          d["compressed_size"] < dkeep["compressed_size"],
          f"full_low={d['compressed_size']} preserve={dkeep['compressed_size']}")

    # quality 参数
    dq30 = compress_image(client, img_b64, quality=30)
    dq90 = compress_image(client, img_b64, quality=90)
    check("quality=30 比 quality=90 更小",
          dq30["compressed_size"] < dq90["compressed_size"],
          f"q30={dq30['compressed_size']} q90={dq90['compressed_size']}")

    # 图片已经很小（不需要 resize）
    small_data = make_image(100, 100)
    ds = compress_image(client, b64(small_data), max_dimension=768)
    check("小图片（100×100）压缩后 token 不变",
          ds["original_tokens"] == ds["compressed_tokens"],
          f"orig={ds['original_tokens']} comp={ds['compressed_tokens']}")

    # data: URI 前缀
    img_with_prefix = "data:image/png;base64," + img_b64
    dp = compress_image(client, img_with_prefix)
    check("接受 data: URI 前缀", dp.get("media_type") == "image/jpeg")

    # JPEG 输入
    jpg_data = make_image(800, 600, "JPEG")
    dj = compress_image(client, b64(jpg_data))
    check("JPEG 输入正常处理", dj.get("media_type") == "image/jpeg")

    # ── 8. 图片批量 ───────────────────────────────────────────────────────────
    section("8. 图片批量 /compress/image/batch")
    img_b64_2 = b64(make_image(800, 600))
    r = client.post("/compress/image/batch", json={"items": [
        {"image": img_b64,   "max_dimension": 768},
        {"image": img_b64_2, "max_dimension": 512},
        {"image": img_b64,   "max_dimension": 256, "quality": 60, "mode": "full_low"},
    ]})
    check("图片 batch → 200", r.status_code == 200)
    bd = r.json()
    check("results 数量正确", len(bd.get("results", [])) == 3)
    check("每条都有 compressed", all("compressed" in x for x in bd.get("results", [])))
    check("batch 支持 full_low", bd.get("results", [None, None, {}])[2].get("mode") == "full_low")
    sizes = [x["compressed_size"] for x in bd.get("results", [])]
    check("第3张（256px）最小", sizes[2] < sizes[0], f"sizes={sizes}")

    # ── 9. 错误处理 ───────────────────────────────────────────────────────────
    section("9. 错误处理")

    # 超长内容
    r = client.post("/compress", json={"content": "x" * 600_000})
    check("content 超限 → 422", r.status_code == 422)

    # 无效 base64
    r = client.post("/compress/image", json={"image": "not_valid_base64!!!"})
    check("无效 base64 → 400", r.status_code == 400,
          f"got {r.status_code}: {r.text[:100]}")

    # 非图片 base64
    r = client.post("/compress/image", json={"image": b64(b"this is not an image")})
    check("非图片数据 → 400", r.status_code == 400,
          f"got {r.status_code}: {r.text[:100]}")

    # max_dimension 越界
    r = client.post("/compress/image", json={"image": img_b64, "max_dimension": 9999})
    check("max_dimension=9999 → 422", r.status_code == 422)

    r = client.post("/compress/image", json={"image": img_b64, "max_dimension": 0})
    check("max_dimension=0 → 422", r.status_code == 422)

    # quality 越界
    r = client.post("/compress/image", json={"image": img_b64, "quality": 0})
    check("quality=0 → 422", r.status_code == 422)

    r = client.post("/compress/image", json={"image": img_b64, "quality": 100})
    check("quality=100 → 422", r.status_code == 422)

    # content 缺失
    r = client.post("/compress", json={})
    check("缺 content 字段 → 422", r.status_code == 422)

    # ── 10. 空内容 / 极短内容 ─────────────────────────────────────────────────
    section("10. 边界内容")
    d = compress(client, "hello")
    check("极短内容（hello）不报错", "compressed" in d)

    d = compress(client, "{}")
    check("空 JSON 对象不报错", "compressed" in d)

    d = compress(client, "[]")
    check("空 JSON 数组不报错", "compressed" in d)

    d = compress(client, "a" * 1000)
    check("1000字符重复内容不报错", "compressed" in d)

    # context 超长
    r = client.post("/compress", json={"content": "hello", "context": "x" * 3000})
    check("context 超限 → 422", r.status_code == 422)

    # 纯文本（不压缩，透传）
    d = compress(client, PLAIN_TEXT, "")
    check("纯文本 compressed 非空", len(d.get("compressed", "")) > 0)
    check("纯文本 ratio <= 1.0", d.get("ratio", 0) <= 1.0)

    # 中英混写日志
    mixed_log = "\n".join(
        f"2024-01-01 12:00:{i:02d} {'ERROR' if i % 10 == 0 else 'INFO'} "
        f"[auth] {'认证失败 user_id=' + str(i) if i % 10 == 0 else f'request {i} ok'}"
        for i in range(200)
    )
    d = compress(client, mixed_log, "认证失败 ERROR")
    check(f"中英混写日志: strategy={d['strategy']} ratio={d['ratio']:.1%}",
          d["ratio"] < 0.7 and len(d["compressed"]) > 0,
          f"ratio={d['ratio']:.1%}")

    # 接近上限的大内容（490KB）
    big_log = (LOG_EN + "\n") * 15
    big_log = big_log[:490_000]
    r = client.post("/compress", json={"content": big_log, "context": "error"}, timeout=60)
    check("490KB 内容正常处理", r.status_code == 200, f"got {r.status_code}")

    # ── 11. 批量包含代码场景 ──────────────────────────────────────────────────
    section("11. 批量含代码场景")
    r = client.post("/compress/batch", json={"items": [
        {"content": CODE_EN,  "context": "user service"},
        {"content": CODE_ZH,  "context": "获取用户"},
        {"content": JSON_EN,  "context": "find errors"},
        {"content": GIT_DIFF, "context": "auth change"},
    ]}, timeout=30)
    check("含代码的 batch → 200", r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")
    if r.status_code == 200:
        bd = r.json()
        check("含代码 batch results 数量", len(bd.get("results", [])) == 4)
        code_ratio = bd["results"][0]["ratio"]
        check(f"代码压缩有效果 ratio={code_ratio:.1%}", code_ratio < 0.9,
              f"ratio={code_ratio:.1%}")

    # ── 12. 并发稳定性 ────────────────────────────────────────────────────────
    section("12. 并发稳定性（10 个并发请求）")
    import threading
    results_concurrent: list = []
    errors_concurrent: list = []

    def _req(i: int) -> None:
        try:
            c = httpx.Client(base_url=base, timeout=30)
            payloads = [
                (JSON_EN, "find errors"),
                (LOG_EN,  "connection error"),
                (CODE_EN, "user service"),
                (SEARCH_ZH, "认证失败"),
            ]
            content, context = payloads[i % len(payloads)]
            d = compress(c, content, context)
            results_concurrent.append(d["ratio"])
        except Exception as e:
            errors_concurrent.append(str(e))

    threads = [threading.Thread(target=_req, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("10 并发全部返回", len(results_concurrent) == 10,
          f"成功={len(results_concurrent)} 失败={len(errors_concurrent)}: {errors_concurrent[:2]}")
    check("并发无 500 错误", len(errors_concurrent) == 0,
          f"errors: {errors_concurrent[:3]}")

    # ── 13. 图片 batch 异常场景 ──────────────────────────────────────────────
    section("13. 图片 batch 异常场景")
    r = client.post("/compress/image/batch", json={"items": []})
    check("图片 batch 空列表 → 422", r.status_code == 422)

    r = client.post("/compress/image/batch", json={"items": [
        {"image": img_b64},
        {"image": "invalid_base64!!!"},
    ]})
    check("图片 batch 含无效项 → 400/422/500", r.status_code in (400, 422, 500))

    # ── 14. 压缩幂等性（同内容多次调用结果一致）────────────────────────────────
    section("14. 幂等性")
    d1 = compress(client, JSON_EN, "find errors")
    d2 = compress(client, JSON_EN, "find errors")
    check("同请求两次 strategy 一致", d1["strategy"] == d2["strategy"])
    check("同请求两次 ratio 一致",    d1["ratio"] == d2["ratio"])

    d1 = compress(client, CODE_EN, "user service")
    d2 = compress(client, CODE_EN, "user service")
    check("代码同请求两次 strategy 一致", d1["strategy"] == d2["strategy"])

    # ── 15. 压缩后内容保留关键信息 ────────────────────────────────────────────
    section("15. 关键信息保留")
    # JSON 压缩后严重告警仍在
    d = compress(client, JSON_ZH, "严重告警")
    check("中文JSON压缩保留严重告警", "严重" in d["compressed"])

    # 日志压缩后 500 错误行仍在
    d = compress(client, LOG_EN, "500 error")
    check("日志压缩保留 500 状态码", "500" in d["compressed"])

    # 代码压缩后函数签名仍在
    d = compress(client, CODE_EN, "payment charge")
    check("代码压缩保留函数签名 charge",      "charge" in d["compressed"])
    check("代码压缩保留函数签名 refund",      "refund" in d["compressed"])
    check("代码压缩保留函数签名 get_history", "get_history" in d["compressed"])

    # 搜索结果压缩后关键词行仍在
    d = compress(client, SEARCH_ZH, "认证失败")
    check("搜索压缩保留关键词行", "认证失败" in d["compressed"])

    # diff 压缩后变更行仍在
    d = compress(client, GIT_DIFF, "auth change")
    check("diff 压缩保留变更行", "new_logic" in d["compressed"])

    # ── 16. 摘要 ─────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if _failures == 0:
        print(f"  \033[32m全部通过 {_total}/{_total}\033[0m")
    else:
        print(f"  \033[31m失败 {_failures}/{_total}\033[0m")
    print(f"{'='*60}\n")

    if _failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tool-compress 功能验证")
    parser.add_argument("--url", default=DEFAULT_URL, help="服务地址")
    args = parser.parse_args()
    run_verify(args.url)
