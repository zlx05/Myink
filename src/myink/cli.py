"""阶段 1 CLI：本地端到端跑通（章节生成 / 校验证据 / 批次 / 任务时间线）。

用法：
    myink init                                  # 建表 + RLS + demo 种子
    myink chapter <project_id> <seq>            # 生成单章
    myink batch <project_id> <N> <start_seq>    # 自动写作批次
    myink status <task_id>                      # 任务状态 + 每节点成本/耗时
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from myink.db import get_engine, tenant_session
from myink.models import Base
from myink.seed import create_demo_project
from myink.workflow.runner import generate_batch, generate_chapter, new_task

app = typer.Typer(help="Myink 推理层 CLI（阶段 1 单体闭环）")
console = Console()


@app.command()
def init(
    no_seed: Annotated[bool, typer.Option(
        "--no-seed",
        help="Run additive initialization without creating demo or sample books.",
    )] = False,
) -> None:
    """初始化数据库：建表 + RLS + demo 种子数据。"""
    from myink.db import (enable_row_level_security, ensure_chapter_versions,
                          ensure_genre_pack, ensure_global_audit_reports,
                          ensure_memory_candidate_kinds, ensure_project_creation,
                          ensure_storage_indexes, ensure_unique_constraints,
                          ensure_user_auth_schema, ensure_user_environment,
                          ensure_user_role, ensure_user_tier, get_admin_engine)
    from myink.invitations import ensure_invitation_schema
    from myink.seed import create_sample_books

    # 建表 + RLS 走超级用户（owner）连接；业务运行走 myink_app（NOBYPASSRLS，受 RLS 约束）
    # 扩展须在 create_all 之前建（HNSW 索引/vector 列依赖 vector 类型，评审存储建议）
    with get_admin_engine().begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    console.print("[bold]1/3[/] 建表（PostgreSQL + pgvector）...")
    Base.metadata.create_all(get_admin_engine())
    ensure_project_creation()
    console.print("[bold]1.5/3[/] 补齐唯一约束 + 组合索引/HNSW（幂等，评审 A6/存储建议）...")
    ensure_unique_constraints()
    ensure_storage_indexes()
    console.print("[bold]1.55/3[/] 补齐 users.tier（阶段 6 VIP 优先级，幂等）...")
    ensure_user_tier()
    console.print("[bold]1.555/3[/] 补齐 users.role（user/admin 独立权限，幂等）...")
    ensure_user_role()
    console.print("[bold]1.56/3[/] 补齐 users.environment（账号级环境配置，幂等）...")
    ensure_user_environment()
    console.print("[bold]1.565/3[/] 补齐账号密码字段与规范用户名唯一索引（幂等）...")
    ensure_user_auth_schema()
    ensure_invitation_schema()
    console.print("[bold]1.57/3[/] 补齐 project_settings.genre_pack（本书题材包，幂等）...")
    ensure_genre_pack()
    console.print("[bold]1.6/3[/] 补齐记忆候选 kind 枚举（memory_removal，阶段 3 编辑校正）...")
    ensure_memory_candidate_kinds()
    console.print("[bold]1.7/3[/] 补齐全局审计报告表（global_audit_reports，阶段 3 长线治理）...")
    ensure_global_audit_reports()
    console.print("[bold]1.8/3[/] 补齐章节版本表（chapter_versions，阶段 4 历史/回退）...")
    ensure_chapter_versions()
    # Startup upgrades are additive. Destructive legacy cleanup must never run
    # automatically when deploying authentication onto an existing book database.
    console.print("[bold]2/3[/] 启用 RLS 主强制（FORCE ROW LEVEL SECURITY）...")
    enable_row_level_security()
    if no_seed:
        console.print("[bold]3/3[/] 跳过 demo 与示例书种子...")
        console.print("[green]✓[/] 初始化完成（无种子数据）")
        return
    console.print("[bold]3/3[/] 写入 demo 种子（《九州问天》+ 示例书）...")
    pid = create_demo_project()
    new_books = create_sample_books()
    console.print(f"[green]✓[/] 初始化完成。demo project_id = [bold]{pid}[/]"
                  + (f"；新增示例书 {len(new_books)} 本（多书展示）" if new_books else ""))


@app.command("auth-upgrade")
def auth_upgrade() -> None:
    """Safely add account-authentication schema without legacy cleanup."""
    from myink.db import ensure_user_auth_schema, ensure_user_role
    from myink.invitations import ensure_invitation_schema
    from myink.models.admin import ensure_admin_schema

    ensure_user_auth_schema()
    ensure_user_role()
    ensure_invitation_schema()
    ensure_admin_schema()
    console.print("[green]OK[/] 账号认证结构已安全升级；旧账号仍需重设密码")


@app.command("set-role")
def set_role(username: str, role: str) -> None:
    """Set an existing account's user/admin role and revoke its sessions."""
    from myink.api.auth import normalize_username
    from myink.db import new_session
    from myink.models import User

    if role not in {"user", "admin"}:
        console.print("[red]角色必须为 user 或 admin[/]")
        raise typer.Exit(1)
    try:
        canonical = normalize_username(username)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    with new_session() as db:
        result = db.execute(
            update(User)
            .where(func.lower(func.btrim(User.username)) == canonical)
            .values(role=role, auth_version=User.auth_version + 1)
        )
        if result.rowcount != 1:
            db.rollback()
            console.print("[red]账号不存在[/]")
            raise typer.Exit(1)
        db.commit()
    console.print(f"[green]OK[/] {canonical} 角色已设为 {role}，旧 token 已全部失效")


@app.command("create-invite")
def create_invite(
    expires_days: int = typer.Option(7, min=1, help="Days until the invitation expires."),
    max_redemptions: int = typer.Option(1, min=1, help="Maximum successful registrations."),
) -> None:
    """Create an invitation and print its plaintext exactly once."""
    from myink.db import new_session
    from myink.invitations import create_invitation

    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
    with new_session() as db:
        invitation, token = create_invitation(
            db,
            expires_at=expires_at,
            max_redemptions=max_redemptions,
        )
        db.commit()
    console.print(f"Invitation id: {invitation.id}")
    console.print(f"Expires at: {invitation.expires_at.isoformat()}")
    console.print(f"Maximum redemptions: {invitation.max_redemptions}")
    console.print(f"Invitation code: {token}")


@app.command("revoke-invite")
def revoke_invite(invitation_id: str) -> None:
    """Revoke an invitation by its non-secret identifier."""
    from myink.db import new_session
    from myink.invitations import revoke_invitation

    try:
        parsed_id = uuid.UUID(invitation_id)
    except ValueError:
        console.print("[red]Invalid invitation id[/]")
        raise typer.Exit(1)
    with new_session() as db:
        if not revoke_invitation(db, parsed_id):
            console.print("[red]Invitation not found[/]")
            raise typer.Exit(1)
        db.commit()
    console.print(f"[green]OK[/] Invitation revoked: {parsed_id}")


@app.command("reset-password")
def reset_password(username: str) -> None:
    """Interactively reset one local account password and revoke its tokens."""
    from myink.api.auth import normalize_username
    from myink.db import new_session
    from myink.models import User
    from myink.passwords import hash_password, validate_password

    try:
        canonical = normalize_username(username)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    password = typer.prompt("新密码", hide_input=True, confirmation_prompt=True)
    try:
        validate_password(password)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    password_hash = hash_password(password)
    with new_session() as db:
        user_id = db.scalar(
            select(User.id).where(func.lower(func.btrim(User.username)) == canonical)
        )
        if user_id is None:
            console.print("[red]账号不存在[/]")
            raise typer.Exit(1)
        result = db.execute(
            update(User)
            .where(User.id == user_id)
            .values(password_hash=password_hash, auth_version=User.auth_version + 1)
        )
        if result.rowcount != 1:
            db.rollback()
            console.print("[red]密码重设失败[/]")
            raise typer.Exit(1)
        db.commit()
    console.print("[green]OK[/] 密码已重设，该账号的旧 token 已全部失效")


@app.command()
def chapter(project_id: str, seq: int, instruction: str | None = None) -> None:
    """生成单章（含校验证据 + 战力通胀/大纲偏差报告）。"""
    task_id = new_task(project_id=project_id, task_type="chapter_generate",
                       payload={"seq": seq}, chapter_seq=seq)
    console.print(f"task_id = {task_id}\n[dim]生成中（10–60s）...[/]")
    result = generate_chapter(project_id=project_id, chapter_seq=seq, task_id=task_id,
                              user_instruction=instruction)
    _print_chapter_result(result)


def _print_chapter_result(result: dict) -> None:
    if result.get("error"):
        console.print(f"[red]✗ 失败: {result['error']}[/]")
        return
    draft = result.get("draft", "")
    console.print(f"\n[bold]第 {result.get('chapter_seq')} 章[/]（{len(draft)} 字）")
    console.print(draft[:500] + ("..." if len(draft) > 500 else ""))

    report = result.get("report") or {}
    findings = report.get("findings", [])
    if findings:
        table = Table(title=f"校验报告（{report.get('summary', {}).get('total', 0)} 项）")
        table.add_column("类型"); table.add_column("严重度"); table.add_column("证据"); table.add_column("建议")
        for f in findings:
            ev = "; ".join(q["quote"][:60] for q in f.get("evidence", []))
            table.add_row(f.get("conflict_type"), f.get("severity"),
                          ev or "-", f.get("suggestion", ""))
        console.print(table)
    else:
        console.print("[green]✓ 校验通过，无冲突[/]")

    if result.get("needs_review"):
        console.print("[yellow]⏸ 存在 critical 冲突 → 候选已转待人工确认（批次将暂停）[/]")


@app.command()
def batch(project_id: str, n: int, start_seq: int = 1) -> None:
    """自动写作批次：一次规划 N 章推进蓝图，逐章生成（§6.11）。"""
    if n < 1 or n > 20:
        console.print("[red]N 需在 1–20（硬上限，§6.11）[/]")
        raise typer.Exit(1)
    task_id = new_task(project_id=project_id, task_type="batch_generate",
                       payload={"size": n, "start": start_seq}, chapter_seq=start_seq)
    console.print(f"batch_task_id = {task_id}\n[dim]批次生成中（N 章，每章 10–60s）...[/]")
    result = generate_batch(project_id=project_id, size=n, start_chapter=start_seq,
                            batch_task_id=task_id)
    if result.get("error"):
        console.print(f"[red]✗ 批次中断: {result['error']}[/]")
    summary = result.get("batch_summary") or {}
    console.print(f"[bold]批次汇总[/]: 状态={summary.get('status')} 完成={summary.get('completed')}/{summary.get('size')} "
                  f"总成本≈¥{summary.get('total_cost', 0)} 总耗时={summary.get('total_duration_ms', 0)}ms")


@app.command()
def status(task_id: str) -> None:
    """任务状态 + 每节点 token/成本/耗时（§6.8 节点时间线）。"""
    from myink.models import AgentRun, Task

    # agent_runs / tasks 是观测/队列数据，不走 RLS（§14 隔离清单：Redis/观测数据无租户语义）
    from myink.db import new_session

    with new_session() as db:
        task = db.get(Task, uuid.UUID(task_id))
        if task is None:
            console.print("[red]任务不存在[/]")
            raise typer.Exit(1)
        # 批次内每章 run 的 task_id = {task_id}:ch{seq}，前缀匹配才不漏章成本（同 node_batch_end）
        runs = db.query(AgentRun).filter(AgentRun.task_id.like(f"{task_id}%")).order_by(AgentRun.id).all()
        table = Table(title=f"任务 {task_id[:8]} · {task.task_type} · {task.status}")
        table.add_column("节点"); table.add_column("模型"); table.add_column("输入tok")
        table.add_column("输出tok"); table.add_column("缓存"); table.add_column("耗时ms")
        table.add_column("成本¥"); table.add_column("重试"); table.add_column("降级")
        for r in runs:
            table.add_row(r.node, r.model_id or "-", str(r.input_tokens), str(r.output_tokens),
                          "✓" if r.cache_hit else "-", str(r.duration_ms),
                          f"{r.cost_est:.4f}", str(r.retry_count), "✓" if r.degraded else "-")
        console.print(table)
        total = round(sum(r.cost_est for r in runs), 6)
        console.print(f"合计成本 ≈ ¥{total}，总耗时 {sum(r.duration_ms for r in runs)}ms")


contract_app = typer.Typer(help="契约工具：导出 OpenAPI 响应契约（阶段 5 契约测试形式化）")
app.add_typer(contract_app, name="contract")


@contract_app.command()
def export() -> None:
    """导出 OpenAPI 契约到 spec/api-openapi.json（HTTP 响应契约单一事实源）。"""
    import json
    import pathlib

    from myink.api.main import app  # 延迟导入：CLI 不模块级引入 API 装配

    spec_path = pathlib.Path(__file__).resolve().parents[2] / "spec" / "api-openapi.json"
    spec_path.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # ASCII 输出（✓ 在 GBK 控制台会 UnicodeEncodeError，见 scripts/ci-local.sh 同款修复）
    console.print(f"[green]OK[/] OpenAPI 契约已导出"
                  f"（{len(app.openapi()['paths'])} 条路径）：{spec_path}")


def _count_embeddings(db: Session, pid: uuid.UUID, level: str) -> set[uuid.UUID]:
    from myink.models import EmbeddingRow

    return {row[0] for row in db.execute(
        select(EmbeddingRow.source_id).where(
            EmbeddingRow.project_id == pid, EmbeddingRow.level == level)).all()}


def _backfill_embeddings(db: Session, pid: uuid.UUID, level: str) -> int:
    """为本书缺失向量的源行补建索引，返回**核实已落库**的新建条数（幂等：已有的跳过）。

    返回的是回填后再查一次的实际行数而非尝试次数：_index_embedding 内部吞异常降级，
    只数尝试次数会报出一个什么都没建成的成功数字——正是本次审计要消灭的静默空转。
    硬约束不向量化（§7.2 恒在 Top-K、不参与相似度截断）。
    """
    from myink.models import Event, Fact
    from myink.workflow.nodes import _index_embedding

    have = _count_embeddings(db, pid, level)
    if level == "event":
        rows = db.execute(select(Event).where(Event.project_id == pid)).scalars()
        todo = [(r.id, r.source_chapter, r.summary) for r in rows]
    else:
        rows = db.execute(select(Fact).where(
            Fact.project_id == pid, Fact.is_hard.is_(False))).scalars()
        todo = [(r.id, r.source_chapter, r.content) for r in rows]
    todo = [(sid, seq, txt) for sid, seq, txt in todo if (txt or "").strip() and sid not in have]
    for source_id, source_chapter, txt in todo:
        _index_embedding(db, project_id=pid, level=level, source_id=source_id,
                         source_chapter=source_chapter, text=txt)
    db.flush()  # 让本次 add 的行能被下面的核实查询看到
    return len({sid for sid, _, _ in todo} & _count_embeddings(db, pid, level))


@app.command("embed-backfill")
def embed_backfill(project: str | None = None, level: str = "event") -> None:
    """为缺失向量的历史事件/非硬约束事实补建索引（§15 最小向量召回）。

    默认 EMBED_ENABLED=0 时向量腿本就是「关闭」，索引为空看不出异常；开关打开后必须
    跑一次本命令，否则召回静默退化为纯关键词（recall_stats 报 enabled_but_empty）。
    幂等可重跑：已建过的源行跳过。
    """
    from myink.config import settings
    from myink.models import Project

    from myink.db import new_session

    if level not in ("event", "world"):
        console.print("[red]level 只能是 event 或 world[/]")
        raise typer.Exit(1)
    if not settings.embed_enabled:
        console.print("[red]EMBED_ENABLED=0：向量化关闭，回填不会产生任何索引[/]")
        raise typer.Exit(1)

    with new_session() as db:  # projects 是租户根表、无 RLS，可跨书枚举
        pids = [str(p) for p in db.execute(select(Project.id)).scalars()]
    if project:
        if project not in pids:
            console.print(f"[red]项目不存在：{project}[/]")
            raise typer.Exit(1)
        pids = [project]

    added = 0
    for pid in pids:
        with tenant_session(pid) as db:
            added += _backfill_embeddings(db, uuid.UUID(pid), level)
    console.print(f"[green]OK[/] 扫描 {len(pids)} 本书，补建 {added} 条 {level} 向量")


if __name__ == "__main__":
    app()
