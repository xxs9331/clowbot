# ClawBot LangGraph v2 需求抽取（第一版）

## 目标

将微信消息处理流程从旧版 `Handler + DispatcherMixin` 抽离为并行的 LangGraph 编排，实现核心链路可测试、可替换、可回滚。

## 输入

- 微信文本消息。
- slash 命令：`/待办`、`/提醒`、`/记录`、`/时间轴`。
- 用户标识 `from_user` 与上下文 `context_token`。

## 输出

- 一条用户可见回复 `reply`。
- 可选写入动作：
  - `record.add`
  - `remind.add`
  - `timeline.append`
  - `todo.merge_new_items`
  - `todo.done_current`
  - `todo.next`
  - `none`

## 核心规则

1. slash 命令优先：命中后直接返回本地读取结果，不走 LLM 决策。
2. 快通道优先：`做完了/好了` -> `todo.done_current`；`下一个` -> `todo.next`。
3. 普通文本走结构化决策：`tool + payload + reply`。
4. 工具执行走 Python 服务层，不让模型直接改 Markdown。
5. `tool=none` 时优先回 `decision.reply`，缺失则走安全兜底文案。

## 第一阶段范围

- 仅覆盖文本主链路与核心工具。
- 图片、agent 权限确认、提醒修改、记录修改不纳入 v2 第一阶段。

