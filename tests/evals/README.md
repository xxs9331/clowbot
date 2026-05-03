# 路由评测（Eval）

## 目的

对 `_run_chat_routing_pipeline` 做「给定输入 → 收集 `decisions_applied` + `wx_sent`」的回归；按失败模式分类（工具错 / 链断 / 幻觉等），而不是只看通过率。

## 两种模式

| 模式 | 说明 |
|------|------|
| **Stub（默认）** | `MinimalEvalACP`，不启动 opencode；适合 CI / 快速结构校验 |
| **Real** | 读仓库根目录 `config.yaml`，启动真实 `OpenCodeACP`，`await Handler.init_session()`；**vault 仍指向 `--tmp` 沙箱**，不写生产日记 |

## 运行方式

```bash
cd D:/Lenovo/Documents/世界树/世界树/.clawbot

# Stub：3 条示例（CI）
pytest tests/test_eval_runner.py -q
python tests/evals/run_eval.py

# 真模型：20 条 E2E（需本机 opencode 可用 + 已配置 config.yaml）
set CLAWBOT_EVAL_REAL=1
python tests/evals/run_eval.py --real
# 或显式指定用例与配置
python tests/evals/run_eval.py --real --cases tests/evals/cases.e2e.yaml --config config.yaml

# pytest 跑真模型（同样需环境变量）
set CLAWBOT_EVAL_REAL=1
pytest tests/test_eval_runner.py::test_eval_e2e_yaml_real_acp -q
```

## Case 文件格式（YAML）

| 字段 | 说明 |
|------|------|
| `id` / `tier` / `input` | 用例标识、档位、用户输入 |
| `from_user` / `context_token` | 可选，默认 `eval-001` / `eval-ctx` |
| `pre_setup` | 可选；`active_todo_queue` + `tasks` + `idx` |
| `expect_tool` | 最后一次 `_apply_unified_decision` 的 `tool` |
| `expect_tool_one_of` | 最后一跳 `tool` 允许集合（软断言，适合真模型） |
| `expect_tools_any` | 轨迹中至少出现一次的 `tool` |
| `expect_reply_contains` | 子串（字符串或列表），均在 `wx_sent` 拼接文本中 |
| `expect_eval_extra_branch` | `local_view` / `safe_fallback` 等（见 `handlers/base.py`） |
| `expect_wx_nonempty` | 为真时要求 `wx_sent` 或 `decisions_applied` 至少其一非空 |
| `expect_state_contains` | 子串（字符串或列表），在 `structured_state_snapshot.collected_data` 中出现 |

仓库内建：

- [`cases.example.yaml`](cases.example.yaml)：3 条，Stub 硬断言。
- [`cases.e2e.yaml`](cases.e2e.yaml)：20 条，混合硬断言与软断言，供 `--real`。

## 代码入口

- `Handler.eval_run_routing_pipeline`（`handlers/base.py`）
- `_eval_mode` 轨迹（`handlers/dispatcher.py`）
- `tests/evals/support.py`：`run_yaml_file` / `merge_config_for_eval` / `try_load_config`

## P1 衔接备忘

- **`consecutive_auto_steps` 重置**：`tool=none` 且无「原文引用」时重置；可与 `_sanitize_vague_none_reply` 思路对齐。
- **`collected_data`**：首版 `list[str]` 即可。
