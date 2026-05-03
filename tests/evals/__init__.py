"""Agent 决策质量评估框架（失败模式导向）。

目录：
  cases/        YAML 评测用例（简单/中等/长任务）
  runner.py     评测执行器（Mock 模式 + LLM 模式）
  metrics.py    指标计算（工具错误率/推理断裂/幻觉/上下文丢失/自愈率）
"""
