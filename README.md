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

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | 是 | 待压缩内容，最大 500KB |
| `context` | string | 否 | 查询上下文，仅对 log/search 策略有效：按关键词相关性打分决定保留哪些行 |

**请求示例（JSON 压缩，20条）**

```json
{"content": "[{\"id\": 0, \"name\": \"user_0\", \"status\": \"inactive\", \"score\": 0.0, \"email\": \"user0@example.com\"}, {\"id\": 1, \"name\": \"user_1\", \"status\": \"active\", \"score\": 1.5, \"email\": \"user1@example.com\"}, {\"id\": 2, \"name\": \"user_2\", \"status\": \"active\", \"score\": 3.0, \"email\": \"user2@example.com\"}, {\"id\": 3, \"name\": \"user_3\", \"status\": \"inactive\", \"score\": 4.5, \"email\": \"user3@example.com\"}, {\"id\": 4, \"name\": \"user_4\", \"status\": \"active\", \"score\": 6.0, \"email\": \"user4@example.com\"}, {\"id\": 5, \"name\": \"user_5\", \"status\": \"active\", \"score\": 7.5, \"email\": \"user5@example.com\"}, {\"id\": 6, \"name\": \"user_6\", \"status\": \"inactive\", \"score\": 9.0, \"email\": \"user6@example.com\"}, {\"id\": 7, \"name\": \"user_7\", \"status\": \"active\", \"score\": 10.5, \"email\": \"user7@example.com\"}, {\"id\": 8, \"name\": \"user_8\", \"status\": \"active\", \"score\": 12.0, \"email\": \"user8@example.com\"}, {\"id\": 9, \"name\": \"user_9\", \"status\": \"inactive\", \"score\": 13.5, \"email\": \"user9@example.com\"}, {\"id\": 10, \"name\": \"user_10\", \"status\": \"active\", \"score\": 15.0, \"email\": \"user10@example.com\"}, {\"id\": 11, \"name\": \"user_11\", \"status\": \"active\", \"score\": 16.5, \"email\": \"user11@example.com\"}, {\"id\": 12, \"name\": \"user_12\", \"status\": \"inactive\", \"score\": 18.0, \"email\": \"user12@example.com\"}, {\"id\": 13, \"name\": \"user_13\", \"status\": \"active\", \"score\": 19.5, \"email\": \"user13@example.com\"}, {\"id\": 14, \"name\": \"user_14\", \"status\": \"active\", \"score\": 21.0, \"email\": \"user14@example.com\"}, {\"id\": 15, \"name\": \"user_15\", \"status\": \"inactive\", \"score\": 22.5, \"email\": \"user15@example.com\"}, {\"id\": 16, \"name\": \"user_16\", \"status\": \"active\", \"score\": 24.0, \"email\": \"user16@example.com\"}, {\"id\": 17, \"name\": \"user_17\", \"status\": \"active\", \"score\": 25.5, \"email\": \"user17@example.com\"}, {\"id\": 18, \"name\": \"user_18\", \"status\": \"inactive\", \"score\": 27.0, \"email\": \"user18@example.com\"}, {\"id\": 19, \"name\": \"user_19\", \"status\": \"active\", \"score\": 28.5, \"email\": \"user19@example.com\"}]"}
```

响应（实测 ratio=0.5%）：

```json
{
  "compressed": "\"[20]{email:string,id:int,name:string,score:float,status:string}\\nuser0@example.com,0,user_0,0.0,inactive\\nuser1@example.com,1,user_1,1.5,active\\n...\"",
  "strategy": "smart_crusher",
  "original_chars": 1917,
  "compressed_chars": 904,
  "original_tokens": 200,
  "compressed_tokens": 1,
  "ratio": 0.005
}
```

**请求示例（日志压缩，100行，context 按相关性过滤）**

```json
{"content": "2024-01-01 12:00:00 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:01 INFO [app] processing request 1\n2024-01-01 12:00:02 INFO [app] processing request 2\n2024-01-01 12:00:03 INFO [app] processing request 3\n2024-01-01 12:00:04 INFO [app] processing request 4\n2024-01-01 12:00:05 INFO [app] processing request 5\n2024-01-01 12:00:06 INFO [app] processing request 6\n2024-01-01 12:00:07 INFO [app] processing request 7\n2024-01-01 12:00:08 INFO [app] processing request 8\n2024-01-01 12:00:09 INFO [app] processing request 9\n2024-01-01 12:00:10 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:11 INFO [app] processing request 11\n2024-01-01 12:00:12 INFO [app] processing request 12\n2024-01-01 12:00:13 INFO [app] processing request 13\n2024-01-01 12:00:14 INFO [app] processing request 14\n2024-01-01 12:00:15 INFO [app] processing request 15\n2024-01-01 12:00:16 INFO [app] processing request 16\n2024-01-01 12:00:17 INFO [app] processing request 17\n2024-01-01 12:00:18 INFO [app] processing request 18\n2024-01-01 12:00:19 INFO [app] processing request 19\n2024-01-01 12:00:20 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:21 INFO [app] processing request 21\n2024-01-01 12:00:22 INFO [app] processing request 22\n2024-01-01 12:00:23 INFO [app] processing request 23\n2024-01-01 12:00:24 INFO [app] processing request 24\n2024-01-01 12:00:25 INFO [app] processing request 25\n2024-01-01 12:00:26 INFO [app] processing request 26\n2024-01-01 12:00:27 INFO [app] processing request 27\n2024-01-01 12:00:28 INFO [app] processing request 28\n2024-01-01 12:00:29 INFO [app] processing request 29\n2024-01-01 12:00:30 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:31 INFO [app] processing request 31\n2024-01-01 12:00:32 INFO [app] processing request 32\n2024-01-01 12:00:33 INFO [app] processing request 33\n2024-01-01 12:00:34 INFO [app] processing request 34\n2024-01-01 12:00:35 INFO [app] processing request 35\n2024-01-01 12:00:36 INFO [app] processing request 36\n2024-01-01 12:00:37 INFO [app] processing request 37\n2024-01-01 12:00:38 INFO [app] processing request 38\n2024-01-01 12:00:39 INFO [app] processing request 39\n2024-01-01 12:00:40 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:41 INFO [app] processing request 41\n2024-01-01 12:00:42 INFO [app] processing request 42\n2024-01-01 12:00:43 INFO [app] processing request 43\n2024-01-01 12:00:44 INFO [app] processing request 44\n2024-01-01 12:00:45 INFO [app] processing request 45\n2024-01-01 12:00:46 INFO [app] processing request 46\n2024-01-01 12:00:47 INFO [app] processing request 47\n2024-01-01 12:00:48 INFO [app] processing request 48\n2024-01-01 12:00:49 INFO [app] processing request 49\n2024-01-01 12:00:50 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:51 INFO [app] processing request 51\n2024-01-01 12:00:52 INFO [app] processing request 52\n2024-01-01 12:00:53 INFO [app] processing request 53\n2024-01-01 12:00:54 INFO [app] processing request 54\n2024-01-01 12:00:55 INFO [app] processing request 55\n2024-01-01 12:00:56 INFO [app] processing request 56\n2024-01-01 12:00:57 INFO [app] processing request 57\n2024-01-01 12:00:58 INFO [app] processing request 58\n2024-01-01 12:00:59 INFO [app] processing request 59\n2024-01-01 12:01:00 ERROR [app] connection refused: db timeout\n2024-01-01 12:01:01 INFO [app] processing request 61\n2024-01-01 12:01:02 INFO [app] processing request 62\n2024-01-01 12:01:03 INFO [app] processing request 63\n2024-01-01 12:01:04 INFO [app] processing request 64\n2024-01-01 12:01:05 INFO [app] processing request 65\n2024-01-01 12:01:06 INFO [app] processing request 66\n2024-01-01 12:01:07 INFO [app] processing request 67\n2024-01-01 12:01:08 INFO [app] processing request 68\n2024-01-01 12:01:09 INFO [app] processing request 69\n2024-01-01 12:01:10 ERROR [app] connection refused: db timeout\n2024-01-01 12:01:11 INFO [app] processing request 71\n2024-01-01 12:01:12 INFO [app] processing request 72\n2024-01-01 12:01:13 INFO [app] processing request 73\n2024-01-01 12:01:14 INFO [app] processing request 74\n2024-01-01 12:01:15 INFO [app] processing request 75\n2024-01-01 12:01:16 INFO [app] processing request 76\n2024-01-01 12:01:17 INFO [app] processing request 77\n2024-01-01 12:01:18 INFO [app] processing request 78\n2024-01-01 12:01:19 INFO [app] processing request 79\n2024-01-01 12:01:20 ERROR [app] connection refused: db timeout\n2024-01-01 12:01:21 INFO [app] processing request 81\n2024-01-01 12:01:22 INFO [app] processing request 82\n2024-01-01 12:01:23 INFO [app] processing request 83\n2024-01-01 12:01:24 INFO [app] processing request 84\n2024-01-01 12:01:25 INFO [app] processing request 85\n2024-01-01 12:01:26 INFO [app] processing request 86\n2024-01-01 12:01:27 INFO [app] processing request 87\n2024-01-01 12:01:28 INFO [app] processing request 88\n2024-01-01 12:01:29 INFO [app] processing request 89\n2024-01-01 12:01:30 ERROR [app] connection refused: db timeout\n2024-01-01 12:01:31 INFO [app] processing request 91\n2024-01-01 12:01:32 INFO [app] processing request 92\n2024-01-01 12:01:33 INFO [app] processing request 93\n2024-01-01 12:01:34 INFO [app] processing request 94\n2024-01-01 12:01:35 INFO [app] processing request 95\n2024-01-01 12:01:36 INFO [app] processing request 96\n2024-01-01 12:01:37 INFO [app] processing request 97\n2024-01-01 12:01:38 INFO [app] processing request 98\n2024-01-01 12:01:39 INFO [app] processing request 99", "context": "connection error"}
```

响应（实测 ratio=68.5%，ERROR 行优先保留）：

```json
{
  "compressed": "2024-01-01 12:00:00 ERROR [app] connection refused: db timeout\n2024-01-01 12:00:01 INFO [app] processing request 1\n...",
  "strategy": "log",
  "original_chars": 5390,
  "compressed_chars": 3682,
  "original_tokens": 710,
  "compressed_tokens": 486,
  "ratio": 0.685
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

最多 32 条（`MAX_BATCH`），顺序执行，响应顺序与请求一致。

**请求示例（JSON）**

```json
{
  "items": [
    {"content": "[{\"id\":1,\"status\":\"error\"},{\"id\":2,\"status\":\"ok\"}]"},
    {"content": "2024-01-01 ERROR [app] timeout\n2024-01-01 INFO [app] ok", "context": "timeout"}
  ]
}
```

---

### POST /compress/image — 图片压缩

**请求**

```json
{"image": "<base64>", "max_dimension": 768, "quality": 85}
```

> `image` 为图片的 base64 字符串（支持带或不带 `data:image/...;base64,` 前缀）。手动测试可用以下命令生成一张测试图片的 base64：
>
> ```bash
> python3 -c "
> import base64, io, random
> from PIL import Image
> random.seed(42)
> img = Image.new('RGB', (1536, 1024))
> img.putdata([(random.randint(0,255),random.randint(0,255),random.randint(0,255)) for _ in range(1536*1024)])
> buf = io.BytesIO()
> img.save(buf, format='PNG')
> b64 = base64.b64encode(buf.getvalue()).decode()
> import json
> print(json.dumps({'image': b64, 'max_dimension': 768, 'quality': 85}))
> " > /tmp/img_payload.json && curl -s -X POST http://localhost:8010/compress/image \
>   -H "Content-Type: application/json" \
>   -d @/tmp/img_payload.json | python3 -m json.tool
> ```

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

最多 32 条，并发处理（受 `IMAGE_CONCURRENCY` 控制）。请求格式同 `/compress/image` 的批量包装，完整示例见 `tests/verify.py`（section 8）。

```json
{
  "items": [
    {"image": "<base64>", "max_dimension": 768, "quality": 85},
    {"image": "<base64>"}
  ]
}
```

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

测量**压缩服务本身**的延迟（不含 LLM 调用），关注 p95/p99。

```bash
# 60s 串行压测（单请求延迟基线）
mise run bench

# 并发压测（扫描 1→2→4→8→16 并发，60s/级，找峰值 QPS 和延迟拐点）
mise run bench-load

# 快速测试（5s/场景）
mise run bench-quick
```

Payload 覆盖三档规模（100 / 500 / 1000 条），模拟真实工具调用大小。

**实测吞吐（Linux 2核 2worker，并发8）**

| 场景 | payload 规模 | 峰值 QPS | p50 | p95 | p99 |
|---|---|---|---|---|---|
| JSON | 100 条 ~6KB | 82/s | 105ms | 158ms | 182ms |
| 搜索结果 | 200 行 ~12KB | 85/s | 102ms | 155ms | 182ms |
| 日志 英文 | 500 行 ~35KB | 68/s | 117ms | 161ms | 180ms |
| 日志 中文 | 300 行 ~20KB | 76/s | 104ms | 144ms | 160ms |
| 代码 | 30 函数 ~8KB | 44/s | 176ms | 271ms | 300ms |

> 并发 8 时 QPS 饱和，继续加并发延迟升、吞吐不增。单请求串行 p50 14~35ms。  
> 注：headroom 官方 benchmark 测 1K 条 JSON 时 p50≈2s，payload 越大压缩耗时线性增长。

### 效果评测

**方案**：before/after QA 准确度对比，不依赖 LLM judge。

```
数据集自带 ground_truth（短答案，如地名、人名、数字）

Baseline：  原始 context + question → LLM → 回答
                                               ↓ token-overlap F1 vs ground_truth
Compressed：/compress(context) + question → LLM → 回答
                                               ↓ token-overlap F1 vs ground_truth

保留率 = compressed_F1 / baseline_F1（越接近 100% 越好）
```

**数据集**：

| 数据集 | 场景 | 样本数 |
|---|---|---|
| tool_outputs | 内置工具调用样本 | 8 |
| hotpotqa | 多跳推理 QA（Wikipedia） | 500 |
| msmarco | 搜索段落 QA（Bing） | 500 |
| codesearchnet | 代码搜索 | 500 |

> squad / bfcl 不纳入：squad 全部透传（测不出压缩效果）；bfcl ground_truth 为 JSON schema，token overlap F1 无意义。

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com
export EVAL_MODEL=deepseek-v4-flash

# 端到端评测（hotpotqa + msmarco + codesearchnet，n=500，并发20）
mise run eval

# 全量评测（n=500，所有有效数据集）
mise run eval-full
```

**成本换算**（参考 GPT-5 输入价格 $5/1M token）：

```
节省成本 = (original_tokens - compressed_tokens) × $5 / 1,000,000
```

eval_service.py 输出结果中包含每个数据集的 token 节省量和估算成本。

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
