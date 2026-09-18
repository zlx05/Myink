"""扫榜端点（plan.md §10：MCP Client 接入外部榜单，只作建书前的灵感工具）。

GET /internal/v1/rankings：全局无项目端点，拉取外部榜单（经 MCP Client + sanitize），
供建书向导「扫榜灵感」面板展示。榜单数据**不进记忆/事实层**（不落库）——这里只是
把编排层受控能力的结果暴露给前端看 + 让用户触发刷新（refresh=true 绕过 TTL 缓存）。

身份：`current_user` 断言（§14.1 ③）——扫榜无项目归属，只要求已认证（缺失 → 403
fail closed）；不再挂 require_owner（无 project_id 可断言）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from myink.api.auth import current_user
from myink.api.schemas import RankingsOut
from myink.integrations import fetch_rankings

router = APIRouter(prefix="/internal/v1", tags=["rankings"])


@router.get("/rankings", response_model=RankingsOut)
async def get_rankings(refresh: bool = False,
                       user_id: str | None = Depends(current_user)) -> dict:
    """扫榜响应：source=remote（实时榜单）/ sample（降级样例），error 为降级原因。"""
    if not user_id:
        raise HTTPException(status_code=403, detail="缺失身份（未携带已认证用户）")
    return await fetch_rankings(refresh=refresh, user_id=user_id)
