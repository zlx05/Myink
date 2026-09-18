"""Myink 阶段 2 展示前端（Streamlit）。

用法：
    streamlit run app.py

生成链路走网关 HTTP（异步：网关三层闸门 → Redis 队列 → worker → PG），
前端轮询任务详情到终态；展示辅助（章节库/流转图/成本时间线）直连 DB 复用
阶段 1 资产。多本书侧边栏切换（seed 已补示例书）。数据库未初始化时界面会
提示先运行 `myink init`。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import streamlit as st

from myink.config import settings
from myink.db import new_session, tenant_session
from myink.models import AgentRun, Chapter, Project, Task

st.set_page_config(page_title="Myink · 阶段 2 展示台", layout="wide")

# ---- 网关 HTTP 客户端（唯一入口，§17.2：生成走网关异步）----
_GW = settings.gateway_url.rstrip("/")
# 阶段 3 JWT 身份断言（§14.1 ③）：网关要求 Bearer，签发端点按 username 换 token（demo 用户）。
# 演示工具缓存于模块级（stale 时 401 由调用方 raise，可重跑本工具刷新）。
_JWT_TOKEN: str | None = None


def _fetch_token() -> str:
    resp = requests.post(f"{_GW}/api/v1/auth/token", json={"username": "demo"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["token"]


def _auth_headers() -> dict:
    """带 Bearer 的网关请求头（首次调用惰性拿 token，§14.1 ③ 替换 X-Myink-User 占位）。"""
    global _JWT_TOKEN
    if _JWT_TOKEN is None:
        _JWT_TOKEN = _fetch_token()
    return {"Authorization": f"Bearer {_JWT_TOKEN}", "Content-Type": "application/json"}


def gw_create_chapter(project_id: str, seq: int, instruction: str | None) -> dict:
    """POST 网关建单章任务 → 202 {task_id, status}。"""
    resp = requests.post(
        f"{_GW}/api/v1/projects/{project_id}/chapters/ch-{seq}/generate",
        json={"seq": seq, "user_instruction": instruction or ""},
        headers=_auth_headers(), timeout=10,
    )
    if resp.status_code == 429:
        raise RuntimeError(f"三层闸门拒绝：{resp.json().get('error')}（配额/并发/日成本超限）")
    resp.raise_for_status()
    return resp.json()


def gw_create_batch(project_id: str, size: int, start: int) -> dict:
    """POST 网关建批次任务 → 202 {task_id, status}。"""
    resp = requests.post(
        f"{_GW}/api/v1/projects/{project_id}/batches/generate",
        json={"size": size, "start": start},
        headers=_auth_headers(), timeout=10,
    )
    if resp.status_code == 429:
        raise RuntimeError(f"三层闸门拒绝：{resp.json().get('error')}")
    resp.raise_for_status()
    return resp.json()


def gw_get_task(task_id: str) -> dict:
    """GET 网关任务详情（status/payload/error/progress/runs）。"""
    resp = requests.get(f"{_GW}/api/v1/tasks/{task_id}", headers=_auth_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json()


def gw_poll_task(task_id: str, timeout: int = 240) -> dict:
    """轮询任务详情到终态（done/failed/awaiting_review/cancelled），返回终态详情。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            detail = gw_get_task(task_id)
        except requests.HTTPError as exc:
            # 任务尚未物化（入队 → DB 落行的异步窗口内）→ 网关透传 404，
            # 视为"仍在队列/在途"继续轮询；其余状态码是真实故障，照常抛出。
            if exc.response is not None and exc.response.status_code == 404:
                time.sleep(0.5)
                continue
            raise
        if detail.get("status") in ("done", "failed", "awaiting_review", "cancelled"):
            return detail
        time.sleep(0.5)
    raise TimeoutError(f"任务 {task_id[:8]} 未在 {timeout}s 内终态")


# ---- 全局任务状态（跨书可见工作状态）----
# 任务提交后立即返回（非阻塞），后台线程轮询到终态更新 session_state.tasks。
# 侧边栏据此展示「正在生成哪本书的哪一章」，切到其他书也不丢失工作状态。

_TASK_TERMINAL = ("done", "failed", "awaiting_review", "cancelled")


@dataclass
class _TaskStatus:
    """一条全局任务的最新状态（跨书可见）。"""
    task_id: str
    project_id: str
    project_title: str
    seq: int
    label: str
    status: str = "queued"
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _init_task_state() -> None:
    if "tasks" not in st.session_state:
        st.session_state.tasks = {}


def _watch_task(t: _TaskStatus, timeout: int = 600) -> None:
    """后台线程轮询任务详情到终态，更新全局状态（非阻塞，跨书可见）。"""
    try:
        detail = gw_poll_task(t.task_id, timeout=timeout)
        t.status = detail.get("status", "done")
        t.detail = detail
    except TimeoutError as exc:
        t.status = "timeout"
        t.error = str(exc)
    except requests.HTTPError as exc:
        t.status = "error"
        t.error = str(exc)
    finally:
        st.session_state.tasks[t.task_id] = t


def _submit_task(project_id: str, title: str, seq: int, label: str, *, create: Any, timeout: int = 600) -> None:
    """建任务（非阻塞）→ 注册后台轮询线程 → 返回。任务状态全局可见。"""
    _init_task_state()
    try:
        resp = create()
    except Exception as exc:
        st.error(f"网关建任务失败：{exc}\n\n请确认网关已启动（`go run ./cmd/gateway`）且 worker 在消费（`myink-worker`）。")
        return
    task_id = resp["task_id"]
    t = _TaskStatus(task_id=task_id, project_id=project_id, project_title=title,
                    seq=seq, label=label)
    st.session_state.tasks[task_id] = t
    threading.Thread(target=_watch_task, args=(t, timeout), daemon=True).start()
    st.toast(f"已提交：{label}（task_id `{task_id[:8]}`），worker 处理中……")


def _render_global_work_status() -> None:
    """侧边栏：所有书的进行中/最近任务（跨书可见，切书不丢工作状态）。"""
    _init_task_state()
    running = {tid: t for tid, t in st.session_state.tasks.items() if t.status not in _TASK_TERMINAL}
    st.divider()
    st.markdown("**🔄 工作状态（跨书）**")
    if not st.session_state.tasks:
        st.caption("暂无任务")
        return
    for tid, t in list(st.session_state.tasks.items()):
        if t.status in _TASK_TERMINAL:
            icon = "✅" if t.status == "done" else "⚠️" if t.status in ("failed", "error", "timeout") else "⏸️"
            st.caption(f"{icon} `{tid[:8]}` {t.label} → {t.status}")
        else:
            st.caption(f"⏳ `{tid[:8]}` {t.label} → {t.status}（{t.project_title} 第{t.seq}章）")
    if running:
        st.caption(f"**{len(running)} 个任务进行中**，详见下方各 tab 结果区。")


# ---- 项目选择（projects 是租户根表，不走 RLS，普通会话可查）----


def list_projects() -> list[Project]:
    with new_session() as db:
        return db.query(Project).order_by(Project.created_at.desc()).all()


try:
    projects = list_projects()
except Exception as exc:  # noqa: BLE001 —— 未初始化时提示
    st.error(f"数据库未初始化或不可用：{exc}")
    st.info("请先在终端运行 `myink init` 建表 + 写入 demo 种子，再启动本页面。")
    st.stop()

if not projects:
    st.error("没有可用项目。请先运行 `myink init`（会创建《九州问天》demo 项目）。")
    st.stop()

project_labels = {f"{p.title} · {str(p.id)[:8]}": p for p in projects}
with st.sidebar:
    st.title("Myink · 阶段 2 展示台")
    label = st.selectbox("作品（多本书）", list(project_labels.keys()))
    project = project_labels[label]
    pid = str(project.id)
    st.caption(f"project_id：`{pid}`\n\n题材：{project.genre} · 当前进度：第 {project.current_chapter} 章")
    # 全局工作状态（跨书可见，切到其他书也不丢工作状态）
    _render_global_work_status()
    st.divider()
    st.caption("生成走**网关异步**（网关闸门 → Redis 队列 → worker → PG），前端轮询进度；"
               "展示辅助（流转图/成本时间线/章节库）直连观测库。")

# ---- 展示辅助 ----


def show_runs(task_id: str) -> None:
    """按 thread_id 前缀查 agent_runs（观测表无租户语义，普通会话可查）。"""
    with new_session() as db:
        runs = (db.query(AgentRun)
                .filter(AgentRun.task_id.like(f"{task_id}%"))
                .order_by(AgentRun.id)
                .all())
    if not runs:
        st.caption("暂无节点执行记录。")
        return
    rows = [{
        "节点": r.node, "模型": r.model_id or "-",
        "输入tok": r.input_tokens, "输出tok": r.output_tokens,
        "缓存": "✓" if r.cache_hit else "-",
        "耗时ms": r.duration_ms, "成本¥": round(r.cost_est, 5),
        "重试": r.retry_count, "降级": "✓" if r.degraded else "-",
    } for r in runs]
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption(f"合计成本 ≈ ¥{round(sum(r.cost_est for r in runs), 6)}，"
               f"总耗时 {sum(r.duration_ms for r in runs)}ms，LLM 调用 {len(runs)} 次")


def _show_task_detail(detail: dict, task_id: str, label: str) -> None:
    """异步任务终态展示：状态 + 网关闸门/队列链路说明 + agent_runs 时间线。"""
    status = detail.get("status")
    if status == "failed":
        st.error(f"✗ 失败：{detail.get('error') or '未知错误'}")
    elif status == "awaiting_review":
        st.warning("⏸ 存在 critical 冲突 → 待人工确认")
    elif status == "cancelled":
        st.warning("⏸ 已取消")
    else:
        st.success(f"✓ {label} 完成（网关 → Redis 队列 → worker → PG 全链路）")
    st.caption(f"task_id：`{task_id}` · 终态 `{status}`"
               + (f" · 批次进度 {detail['progress']['current']}/{detail['progress']['total']}"
                  if detail.get("progress") else ""))
    st.divider()
    st.markdown("**节点执行时间线（agent_runs）**")
    show_runs(task_id)
    st.info("正文已落库，去「🗂 章节库」查看本章内容与执行流转图。")


# ---- 单章生成（走网关异步） ----


def _next_seq(pid: str) -> int:
    """真实已写最大章 + 1（chapters 是权威；current_chapter 仅展示，历史数据可能脱节）。

    写保护（worker 侧）按「DB 实际最大章 + 1」放行，前端必须与它同口径，
    否则派生字段 current_chapter 脱节时前端会永远提交旧章、被写保护拒绝而卡死。
    """
    with tenant_session(pid) as db:
        top = (db.query(Chapter).filter(Chapter.project_id == pid)
               .order_by(Chapter.chapter_seq.desc()).first())
        return (top.chapter_seq + 1) if top else 1


def render_chapter_tab(pid: str, project_title: str) -> None:
    st.subheader("单章生成")
    st.caption("生成走网关异步链路：网关三层闸门 → Redis 队列 → worker 跑图 → PG 落库。")
    # 章节顺序由 worker 写保护强制：只能写「已写最大章 + 1」（§11 顺序约束）。
    # 前端只读显示下一章，不可手填跳章/重写（重写会覆盖正文且后续章不级联）。
    next_seq = _next_seq(pid)
    with st.form("chapter_form"):
        seq = st.number_input("章节序号（写保护：只能写下一章）", min_value=1, value=next_seq, step=1, disabled=True)
        instruction = st.text_area("作者指令（可选）",
                                   placeholder="例：本章林砚与秦虎正面交锋，铺垫玉佩真相……",
                                   height=80)
        submitted = st.form_submit_button(f"生成第 {next_seq} 章（走网关）", type="primary")
    if submitted:
        _submit_task(
            pid, project_title, next_seq, f"单章 第{next_seq}章",
            create=lambda: gw_create_chapter(pid, next_seq, instruction or None),
            timeout=240,
        )


# ---- 批次生成（走网关异步） ----


def render_batch_tab(pid: str, project_title: str) -> None:
    st.subheader("自动写作批次")
    st.caption("一次规划 N 章推进蓝图，逐章生成（每章 10–60s，建议 N ≤ 3 试跑）。走网关异步。")
    # 批次从「下一章」起连续 N 章（worker 写保护逐章校验，批次内连续推进）
    next_seq = _next_seq(pid)
    with st.form("batch_form"):
        col1, col2 = st.columns(2)
        n = col1.number_input("章节数 N", min_value=1, max_value=20, value=3, step=1)
        start = col2.number_input("起始章（写保护：只能从下一章起）", min_value=1, value=next_seq, step=1, disabled=True)
        submitted = st.form_submit_button(f"开始批次（从第 {next_seq} 章起，走网关）", type="primary")
    if submitted:
        n, start = int(n), int(next_seq)
        _submit_task(
            pid, project_title, start, f"批次 {start}~{start+n-1}章",
            create=lambda: gw_create_batch(pid, n, start),
            timeout=600,
        )


# ---- 任务时间线 / 章节库 ----


def render_tasks_tab(pid: str) -> None:
    st.subheader("任务时间线")
    with new_session() as db:
        tasks = (db.query(Task).order_by(Task.created_at.desc()).limit(20).all())
    if not tasks:
        st.caption("暂无任务。先在「单章 / 批次」页生成。")
        return
    task_labels = {f"{t.task_type} · {t.status} · {str(t.id)[:8]}" : str(t.id) for t in tasks}
    selected = st.selectbox("选择任务查看节点时间线", list(task_labels.keys()))
    tid = task_labels[selected]
    st.caption(f"task_id：`{tid}`")
    show_runs(tid)


def _chapter_run_task_ids(pid: str, chapter_seq: int) -> list[str]:
    """收集该章可能对应的 agent_runs task_id 前缀：
    - 单章生成：task_id = 单章任务 id（tasks.chapter_seq 匹配）
    - 批次生成：task_id = {batch_task_id}:ch{seq}（从任务 payload 推导）
    agent_runs.task_id 是字符串非严格 uuid，用前 8 位精确前缀匹配即可定位。
    """
    ids: list[str] = []
    with tenant_session(pid) as db:
        from myink.models import Task

        # 单章任务：task_id = 任务 id（tasks.chapter_seq 精确匹配）
        for t in db.query(Task).filter(Task.chapter_seq == chapter_seq,
                                       Task.task_type == "chapter_generate").all():
            ids.append(str(t.id))
        # 批次任务：chapter_seq 落在 payload.start~start+size-1，task_id = {batch}:ch{seq}
        for t in db.query(Task).filter(Task.task_type == "batch_generate").all():
            payload = t.payload or {}
            start = payload.get("start") or 1
            size = payload.get("size") or 0
            if start <= chapter_seq < start + size:
                ids.append(f"{t.id}:ch{chapter_seq}")
    return ids


# 审计路由由 audit 的下一环节推断：revise→rewrite、plan_chapter→replan、persist→pass
_ROUTE_LABEL = {"revise": "rewrite", "batch_plan": "replan", "plan_chapter": "replan", "persist": "pass"}
_ROUTE_COLOR = {"rewrite": "#d97706", "replan": "#dc2626", "pass": "#16a34a", "review": "#7c3aed"}
_NODE_CN = {"batch_plan": "批次规划", "load_state": "加载状态", "recall": "召回",
            "plan_chapter": "章节规划", "write": "写作", "extract": "记忆抽取",
            "validate": "L1校验", "audit": "审核中枢", "revise": "修订",
            "persist": "落库", "needs_review": "待人工", "reflexion": "复盘",
            "global_audit": "全局审计"}
# 确定性节点（无 LLM 调用，cost=0，record_plain 记录）：卡片显示统计而非成本
# （reflexion 调 LLM 有真实成本，不列为确定性——record_plain 短路路径 cost=0 显示 ¥0 即可）
_DETERMINISTIC = {"load_state", "recall", "validate", "persist"}


def _flow_svg(steps: list[dict]) -> str:
    """横向箭头流：每环节一个卡片（LLM 节点显示成本¥；确定性节点显示统计），环节间箭头标路由。"""
    box_w, box_h, gap = 138, 58, 34
    arrow_w = 26
    cards = []
    for i, s in enumerate(steps):
        x = i * (box_w + gap)
        color = "#1f2937"
        if s.get("route_from"):
            color = _ROUTE_COLOR[s["route_from"]]
        title = s.get("node_cn", s["node"])
        if s.get("deterministic"):
            sub = "确定性"
        elif s["cost"] > 0:
            sub = f"¥{s['cost']:.4f}"
        else:
            sub = "-"
        cards.append(f"""
        <g>
          <rect x="{x}" y="12" width="{box_w}" height="{box_h}" rx="8"
                fill="white" stroke="{color}" stroke-width="2"/>
          <text x="{x + box_w/2}" y="38" text-anchor="middle" font-size="13"
                font-weight="600" fill="{color}">{title}</text>
          <text x="{x + box_w/2}" y="58" text-anchor="middle" font-size="11"
                fill="#6b7280">{sub}</text>
        </g>""")
        if i < len(steps) - 1:
            ax = x + box_w
            ay = 12 + box_h / 2
            nxt = steps[i + 1]
            route = nxt.get("route_from")
            color = _ROUTE_COLOR.get(route, "#9ca3af")
            label = _ROUTE_LABEL.get(nxt["node"], "") if route else ""
            arrow = f"""
            <line x1="{ax}" y1="{ay}" x2="{ax + gap}" y2="{ay}" stroke="{color}"
                  stroke-width="2"/>
            <polygon points="{ax + gap},{ay} {ax + gap - 7},{ay - 4} {ax + gap - 7},{ay + 4}"
                     fill="{color}"/>
            <text x="{ax + gap/2}" y="{ay - 8}" text-anchor="middle" font-size="10"
                  fill="{color}">{label}</text>"""
            cards.append(arrow)
    width = len(steps) * box_w + (len(steps) - 1) * (gap + arrow_w)
    return (f'<svg width="{width}" height="86" xmlns="http://www.w3.org/2000/svg" '
            f'style="max-width:100%;overflow:visible">' + "".join(cards) + "</svg>")


def _build_trace_steps(pid: str, chapter_seq: int, chapter_status: str | None = None) -> list[dict] | None:
    """查询该章 agent_runs 并构建流转步骤（纯数据，供渲染 / 测试）。
    连续相同节点合并为一环节（工具循环 / 修订多轮累加）；audit 后的下一
    LLM 环节推断路由（revise→rewrite / batch_plan→replan）；audit 是最后
    环节时按章节终态合成终点（confirmed→pass 落库 / awaiting_review→待人工）。
    返回 None 表示该章无执行记录。
    """
    ids = _chapter_run_task_ids(pid, chapter_seq)
    if not ids:
        return None
    with new_session() as db:
        from myink.models import AgentRun

        runs = (db.query(AgentRun)
                .filter(AgentRun.project_id == pid, AgentRun.task_id.in_(ids))
                .order_by(AgentRun.id).all())
    if not runs:
        return None

    steps: list[dict] = []
    for r in runs:
        if steps and steps[-1]["node"] == r.node:
            prev = steps[-1]
            prev["cost"] += r.cost_est
            prev["duration_ms"] += r.duration_ms
            prev["input_tokens"] += r.input_tokens
            prev["output_tokens"] += r.output_tokens
            prev["runs"] += 1
            # detail 保留最后一次非 None（最终轮 detail 含完整 tool_trace / audit_verdict）
            if r.detail:
                prev["detail"] = r.detail
            continue
        route_from = None
        if steps and steps[-1]["node"] == "audit" and r.node != "audit":
            route_from = _ROUTE_LABEL.get(r.node, "pass")
        steps.append({
            "node": r.node, "node_cn": _NODE_CN.get(r.node, r.node),
            "route_from": route_from, "cost": r.cost_est,
            "duration_ms": r.duration_ms, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "runs": 1,
            "model_id": r.model_id, "cache_hit": r.cache_hit,
            "degraded": r.degraded, "detail": r.detail,
            "deterministic": r.node in _DETERMINISTIC,
        })
    # 旧数据兼容：agent_runs 尚无确定性节点行时，audit 作为最后环节 → 按章节终态合成终点
    if steps and steps[-1]["node"] == "audit":
        if chapter_status == "awaiting_review":
            steps.append({"node": "needs_review", "node_cn": "待人工", "route_from": "review",
                          "cost": 0.0, "duration_ms": 0, "input_tokens": 0, "output_tokens": 0,
                          "runs": 0, "model_id": None, "cache_hit": False, "degraded": False,
                          "deterministic": True})
        else:
            steps.append({"node": "persist", "node_cn": "落库(pass)", "route_from": "pass",
                          "cost": 0.0, "duration_ms": 0, "input_tokens": 0, "output_tokens": 0,
                          "runs": 0, "model_id": None, "cache_hit": False, "degraded": False,
                          "deterministic": True})
    return steps


def show_chapter_trace(pid: str, chapter_seq: int, chapter_status: str | None = None) -> None:
    """章节执行轨迹：状态流转图 + 每环节精确花费（agent_runs，§6.8）。"""
    steps = _build_trace_steps(pid, chapter_seq, chapter_status)
    if steps is None:
        st.caption("该章无 LLM 环节执行记录（可能是测试数据直接写入，或 agent_runs 被清）。")
        return

    st.markdown("**执行流转图**（箭头 = 实际路由；audit 后箭头标注 verdict）")
    import base64
    import json as _json

    svg = _flow_svg(steps)
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    svg_w = len(steps) * 138 + (len(steps) - 1) * 60
    st.image(data_uri, width=min(svg_w, 2400))
    st.caption("灰色卡片 = 确定性节点（纯代码，cost=0）；audit → persist = pass 放行；"
               "audit → 修订 = rewrite；audit → 章节规划 = replan。")

    st.markdown("**每环节记录（agent_runs，含关键产物 detail）**")
    rows = []
    for s in steps:
        d = s.get("detail") or {}
        if d.get("audit_verdict"):
            av = d["audit_verdict"]
            detail_txt = f"verdict={av.get('verdict')} conf={av.get('confidence')}"
        elif d.get("tool_trace"):
            tools = ", ".join(sorted({t["tool"] for t in d["tool_trace"]}))
            detail_txt = f"工具: {tools}"
        elif d:
            detail_txt = ", ".join(f"{k}={v}" for k, v in d.items())
        else:
            detail_txt = "-"
        rows.append({
            "环节": s["node_cn"], "节点": s["node"],
            "LLM次数": s["runs"] if not s.get("deterministic") else "—",
            "输入tok": s["input_tokens"], "输出tok": s["output_tokens"],
            "耗时ms": s["duration_ms"], "成本¥": round(s["cost"], 5),
            "明细": detail_txt,
        })
    st.dataframe(rows, width="stretch", hide_index=True)
    total_cost = sum(s["cost"] for s in steps)
    total_ms = sum(s["duration_ms"] for s in steps)
    st.caption(f"本章 LLM 总成本 ≈ ¥{total_cost:.5f} · 总耗时 {total_ms}ms · "
               f"LLM 调用 {sum(s['runs'] for s in steps)} 次")

    with st.expander("查看每环节完整 detail（JSON，debug 用）"):
        for s in steps:
            d = s.get("detail")
            if d:
                st.markdown(f"**{s['node_cn']}** `{s['node']}`")
                st.code(_json.dumps(d, ensure_ascii=False, indent=1), language="json")


def render_chapters_tab(pid: str) -> None:
    st.subheader("已落库章节")
    with tenant_session(pid) as db:
        chapters = db.query(Chapter).order_by(Chapter.chapter_seq).all()
    if not chapters:
        st.caption("暂无已确认章节。")
        return
    rows = [{"章": c.chapter_seq, "状态": c.status, "字数": len(c.content or ""),
             "来源": c.generation_source or "-", "版本": c.version,
             "标题": c.title or "-"} for c in chapters]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.divider()
    st.markdown("**查看章节详情**")
    seq_by_label = {f"第 {c.chapter_seq} 章 · {c.status} · {len(c.content or '')} 字": c
                    for c in chapters}
    selected = st.selectbox("选择章节", list(seq_by_label.keys()))
    ch = seq_by_label[selected]
    meta = f"状态：{ch.status} · 来源：{ch.generation_source or '-'} · 版本：{ch.version}"
    if ch.title:
        meta += f" · 标题：{ch.title}"
    st.caption(meta)
    st.text_area("正文", value=ch.content or "（本章暂无正文）", height=300, disabled=True,
                 label_visibility="collapsed")
    st.divider()
    show_chapter_trace(pid, ch.chapter_seq, ch.status)


# ---- 主区 ----

tab_chapter, tab_batch, tab_tasks, tab_chapters = st.tabs(
    ["📄 单章生成", "📚 批次生成", "⏱ 任务时间线", "🗂 章节库"])

with tab_chapter:
    render_chapter_tab(pid, project.title)
with tab_batch:
    render_batch_tab(pid, project.title)
with tab_tasks:
    render_tasks_tab(pid)
with tab_chapters:
    render_chapters_tab(pid)
