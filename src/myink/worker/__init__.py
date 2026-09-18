"""阶段 2 Python Worker：消费 Redis Streams 跑 LangGraph（§阶段2）。

职责边界（plan.md §17.2 三进程拓扑）：worker 只做「读→跑→ack」，
队列机制（退避重投 / DLQ / 崩溃认领）全在网关 dispatcher；worker 是哑消费者。
"""
