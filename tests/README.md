# Tests

布局约定：每个效果评测在 `evals/<场景>/` 下自成一个目录，目录内只有一个
`eval.py`（评测脚本）+ `data/`（该评测专用的数据集，自包含，不跨目录共享）。

接口/性能验证（不属于"效果评测"，各自在 `tests/` 下单独一个目录）：

- `verify/verify.py`
  - 接口功能验证，覆盖 `/compress`、`/compress/batch`、`/compress/image`、`/compress/image/batch`、`/health`
- `benchmark/benchmark.py`
  - 性能与并发压测

效果评测（`evals/`）：

- `evals/json_tool_calling/`
  - JSON / tool-calling，BFCL simple 固定 100 条
- `evals/code_factqa/`
  - `code_aware` 在 CodeSearchNet fact-QA 固定 100 条样本上的评测
- `evals/search_deterministic/`
  - `search` 的确定性保真评测
- `evals/log_deterministic/`
  - `log` 的确定性保真评测（异常模板是否在压缩结果里还能找到）
- `evals/log_qa_judge/`
  - `log` 的端到端 QA judge 评测（和 log_deterministic 用同一份 LogHub 数据，
    各自目录下各放一份）
- `evals/git_diff_judge/`
  - `git diff` 的压缩前后回答一致性评测（`prepare_data.py` 记录了数据集如何从
    真实 commit 历史按压缩率筛选生成）
- `evals/image_textvqa/`
  - `image` 在 TextVQA 上的评测

其他：

- `eval_service.py`
  - 通用多数据集探索性评测工具（hotpotqa/msmarco/squad/codesearchnet/bfcl 等），
    非正式效果评测主结果，结果不进 README
- `fidelity.py`
  - 本地无 judge 的多策略 smoke / 回归检查
