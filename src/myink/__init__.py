"""Myink 推理层（阶段 1：Python 单体闭环）。

架构（plan.md §6）：LangGraph 两级图（批次主图 + 单章子图），
PostgreSQL + pgvector 一库多用，ModelProvider 抽象可切换/降级。
"""

__version__ = "0.1.0"
