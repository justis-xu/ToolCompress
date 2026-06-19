# tool-compress

AI Agent 工具结果压缩服务。接收工具调用返回值，自动识别内容类型并压缩，减少送入 LLM 的 token 数量。

## 快速启动

```bash
docker build -t tool-compress:latest .
docker-compose up -d
curl http://localhost:8010/health
```

## 压缩能力

根据内容自动路由到对应策略，无需调用方指定：

| 内容类型 | 自动识别条件 | 策略 | token 节省 |
|---|---|---|---|
| JSON 数组 / 对象 | 合法 JSON | SmartCrusher（schema+CSV 紧凑化） | **60~99%** |
| 日志 / 构建输出 | 含 INFO/WARN/ERROR 前缀 | LogCompressor（保留错误行） | 75~92% |
| grep / 搜索结果 | `file:line:content` 格式 | SearchCompressor（按相关性选行） | 80~92% |
| 源代码 | 代码文件特征 | CodeAwareCompressor（AST 保留签名） | 30~60% |
| Git diff | `diff --git` 开头 | DiffCompressor | 40~55% |
| HTML 网页 | `<html>` / `<!DOCTYPE>` | HTMLExtractor（trafilatura 提取正文） | 20~30% |
| 图片 | base64 图片 | PIL resize + JPEG | 60~90% |
| 其他文本 | 以上均不匹配 | 透传（不压缩） | 0% |

**注意事项**：
- JSON 中字符串字段超过 250 字节会被预截断至 80 字符，防止 SmartCrusher 产生无法解析的 CCR 占位符（headroom#1091）
- 代码压缩：函数体需 >15 行才有收益；短函数自动透传，不增加 token
- 纯中文日志（无 ERROR/INFO 前缀）使用 PyPI 版时关键词打分效果有限

## 接口

### POST /compress — 文本压缩

**请求**

```json
{
  "content": "...",
  "context": "..."
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | 是 | 待压缩内容，最大 500KB |
| `context` | string | 否 | 查询上下文，帮助日志/搜索场景按相关性打分保留重要行，中英文均可 |

**响应**

```json
{
  "compressed": "...",
  "strategy": "smart_crusher",
  "original_chars": 65000,
  "compressed_chars": 1200,
  "original_tokens": 8000,
  "compressed_tokens": 120,
  "ratio": 0.015
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `compressed` | string | 压缩后内容 |
| `strategy` | string | 实际使用的策略，见下表 |
| `original_chars` | int | 原始字符数 |
| `compressed_chars` | int | 压缩后字符数 |
| `original_tokens` | int | 原始 token 估算（支持中英文混合） |
| `compressed_tokens` | int | 压缩后 token 估算 |
| `ratio` | float | `compressed_tokens / original_tokens`，越小压缩率越高 |

`strategy` 取值：

| 值 | 含义 |
|---|---|
| `smart_crusher` | JSON 统计紧凑化 |
| `log` | 日志过滤 |
| `search` | 搜索结果过滤 |
| `code_aware` | AST 代码压缩 |
| `diff` | Git diff 压缩 |
| `html` | HTML 正文提取 |
| `text` / `passthrough` | 未压缩，原文透传 |

**错误码**

| 状态码 | 原因 |
|---|---|
| 422 | `content` 超过 500KB 或 `context` 超过 2000 字符 |
| 500 | 压缩内部错误 |

---

### POST /compress/batch — 批量文本压缩

**请求**

```json
{
  "items": [
    {"content": "...", "context": "..."},
    {"content": "..."}
  ]
}
```

- 最多 32 条（`MAX_BATCH`），每条限制同单条
- 顺序执行，响应顺序与请求一致

**响应**

```json
{
  "results": [
    { ...单条响应格式... },
    { ...单条响应格式... }
  ]
}
```

---

### POST /compress/image — 图片压缩

**请求**

```json
{
  "image": "<base64>",
  "max_dimension": 768,
  "quality": 85
}
```

| 字段 | 类型 | 必填 | 默认 | 范围 | 说明 |
|---|---|---|---|---|---|
| `image` | string | 是 | — | — | Base64 图片，支持带或不带 `data:image/...;base64,` 前缀 |
| `max_dimension` | int | 否 | 768 | 64~2048 | 最长边限制（px），超出按比例缩放 |
| `quality` | int | 否 | 85 | 10~95 | JPEG 压缩质量 |

**响应**

```json
{
  "compressed": "<base64 JPEG>",
  "media_type": "image/jpeg",
  "original_size": 102400,
  "compressed_size": 15360,
  "ratio": 0.15,
  "original_tokens": 765,
  "compressed_tokens": 255
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `compressed` | string | 压缩后 JPEG 的 base64 |
| `media_type` | string | 固定为 `image/jpeg` |
| `original_size` | int | 原始字节数 |
| `compressed_size` | int | 压缩后字节数 |
| `ratio` | float | `compressed_size / original_size` |
| `original_tokens` | int | 原始图片 token 估算（按 512px tile 计算） |
| `compressed_tokens` | int | 压缩后 token 估算 |

---

### POST /compress/image/batch — 批量图片压缩

```json
{
  "items": [
    {"image": "...", "max_dimension": 768, "quality": 85},
    {"image": "..."}
  ]
}
```

- 最多 32 条，并发处理（受 `IMAGE_CONCURRENCY` 控制）

---

### GET /health — 健康检查

```json
{"status": "ok"}
```

服务未就绪时返回 `503`。

---

## 部署

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | 8000 | 监听端口（docker-compose 映射到宿主机 8010） |
| `WORKERS` | 2 | uvicorn 进程数，建议 = CPU 核数 |
| `MAX_CONTENT_LEN` | 500000 | 文本最大字节数（500KB） |
| `MAX_CONTEXT_LEN` | 2000 | context 最大字符数 |
| `MAX_BATCH` | 32 | 单次 batch 最大条数 |
| `MAX_IMAGE_DIM` | 768 | 图片压缩最大边长（px） |
| `IMAGE_QUALITY` | 85 | JPEG 质量 |
| `IMAGE_CONCURRENCY` | 2 | 图片并发处理数 |
| `SLOW_MS` | 500 | 超过此耗时记 WARNING 日志（ms） |

### 构建镜像

```bash
# 直接构建（推荐）
docker build -t tool-compress:latest .
docker-compose up -d

# base + runtime 分离（适合多服务共用基础镜像时加速构建）
docker build -f Dockerfile.base -t tool-compress-base:latest .
docker build -t tool-compress:latest .
docker-compose up -d
```

---

## 测试

测试脚本在 `tests/` 目录下，依赖 `httpx` 和 `Pillow`：

```bash
pip install httpx Pillow
```

### 功能验证

```bash
mise run verify
# 或
python3.12 tests/verify.py --url http://localhost:8010
```

覆盖所有端点、内容类型、边界场景，共 84 个测试用例。

### 性能测试

```bash
# 60s 压测（显示 QPS、p50、p95）
mise run bench

# 快速测试（5s/场景）
mise run bench-quick

# 效果测试（关键词保留率）
mise run bench-quality
```

---

## 实测压缩率

测试环境：Docker 容器，stock headroom-ai，`enable_kompress=False`

| 类型 | 数据规模 | 策略 | token 压缩至 |
|---|---|---|---|
| JSON 英文 | 500 条订单记录 | smart_crusher | **0.0%** |
| JSON 中文 | 300 条告警记录 | smart_crusher | 37.6% |
| 日志 英文 | 1000 行 INFO/ERROR | log | **7.7%** |
| 日志 中文 | 800 行含 ERROR | log | **12.0%** |
| 代码 英文 | 4 个方法（PaymentService） | code_aware | 41.0% |
| 代码 中文 | 3 个方法（订单服务） | code_aware | 33.2% |
| 搜索 英文 | 500 行 grep 结果 | search | **7.5%** |
| 搜索 中文 | 400 行 grep 结果 | search | **9.1%** |
| Git diff | 20 个 hunk | diff | 56.6% |
| HTML | ML 文章含广告导航 | html | 75.7% |

## 已知限制

- **代码压缩**：函数体 <15 行时自动透传（压缩加注释反而增加 token）；class-level docstring 会触发 `mixed` 策略
- **HTML 压缩**：依赖 trafilatura 正文提取，对小段 HTML（<1KB）压缩率可能 <1
- **CCR 占位符**：JSON 中超长字符串字段（>250B）会预截断至 80 字符再压缩；若 headroom 仍产生 `<<ccr:>>` 占位符，服务自动回退透传并记录 ERROR 日志
- **AVX 依赖**：headroom Rust core 需要 AVX/AVX2 指令集，部分旧 CPU 或虚拟化环境不支持，可设 `HEADROOM_REQUIRE_RUST_CORE=false` 降级
- **中文 context 匹配**：Rust core 按空格分词，服务侧已对 context 做 CJK bigram 展开以提升中文关键词命中；content 本身不做此处理（否则会污染输出）
