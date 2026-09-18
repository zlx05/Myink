"""外部集成（plan.md §10）：MCP Client 扫榜能力。

- `mcp.py`：MCP 协议客户端薄封装（所有 mcp SDK import 的隔离点）；
- `rankings.py`：扫榜域封装（sanitize + 缓存 + 降级），对外暴露 `fetch_rankings`
  （async，API）。扫榜已整体前移至建书前的灵感工具，不再有 sync 图节点入口。
"""

from myink.integrations.rankings import fetch_rankings

__all__ = ["fetch_rankings"]
