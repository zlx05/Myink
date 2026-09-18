"""Python API 装配（FastAPI，127.0.0.1:8100，内部服务）。

启动：`myink-api` 或 `python -m myink.api.main`（pyproject scripts）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from myink.api.auth import current_user, require_owner
from myink.api.auth import router as auth_router
from myink.api.routes_book import router as book_router
from myink.api.routes_candidates import router as candidates_router
from myink.api.routes_chapters import router as chapters_router
from myink.api.routes_environment import router as environment_router
from myink.api.routes_genre import router as genre_router
from myink.api.routes_global_audit import router as global_audit_router
from myink.api.routes_lessons import router as lessons_router
from myink.api.routes_rankings import router as rankings_router
from myink.api.routes_settings import router as settings_router
from myink.api.routes_style import router as style_router
from myink.api.routes_tasks import router as tasks_router
from myink.api.schemas import ChapterDetailOut, ChapterMetaOut, ProjectOut
from myink.config import settings
from myink.db import new_session, tenant_session
from myink.models import Chapter, Project
from myink.worker.redis_client import get_redis

logger = logging.getLogger(__name__)

app = FastAPI(title="Myink 内部 API", docs_url=None, redoc_url=None)

# 仅网关访问，但 MVP 开发方便看错误；生产收紧为内网白名单（阶段 5）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(book_router)
app.include_router(tasks_router)
app.include_router(candidates_router)
app.include_router(chapters_router)
app.include_router(lessons_router)
app.include_router(style_router)
app.include_router(settings_router)
app.include_router(environment_router)
app.include_router(genre_router)
app.include_router(global_audit_router)
app.include_router(rankings_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    checks = {}
    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"fail: {exc}"
    try:
        from sqlalchemy import text

        with new_session() as db:
            db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"fail: {exc}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "degraded", "checks": checks}, status_code=503)
    return {"status": "ok", "checks": checks}


# ---- 项目/章节读（RLS 保护，tenant_session 带租户上下文）----


@app.get("/internal/v1/projects", response_model=list[ProjectOut])
def list_projects(user_id: str | None = Depends(current_user)) -> list[dict]:
    """项目列表（根表无 RLS，应用层按身份过滤：只返回自己的书，§14.1 ③）。

    identity 缺失/非法 → 空列表（fail closed，不返回他人作品）。
    """
    if not user_id:
        return []  # 身份缺失 → fail closed
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError, AttributeError):
        return []  # 身份非法 → fail closed
    with new_session() as db:
        rows = db.query(Project).filter(Project.user_id == uid).all()
        return [
            {
                "id": str(p.id),
                "title": p.title,
                "genre": p.genre,
                "current_chapter": p.current_chapter,
                "target_words": p.target_words,
            }
            for p in rows
        ]


@app.get("/internal/v1/projects/{project_id}/chapters",
         dependencies=[Depends(require_owner)], response_model=list[ChapterMetaOut])
def list_chapters(project_id: str) -> list[dict]:
    """章节列表（RLS：tenant_session 过滤，只返回本项目 + 归属断言双保险）。

    word_count = 正文字符数（len 口径，与 L1 chapter_length_check §6.9 同源——
    中文按字符计，含标点），供章节列表/正文侧展示每章字数。
    """
    with tenant_session(project_id) as db:
        rows = db.query(Chapter).order_by(Chapter.chapter_seq).all()
        return [
            {
                "id": str(c.id),
                "chapter_seq": c.chapter_seq,
                "title": c.title,
                "status": c.status,
                "word_count": len(c.content or ""),
                "summary": c.summary,
            }
            for c in rows
        ]


@app.get("/internal/v1/projects/{project_id}/chapters/{chapter_id}",
         dependencies=[Depends(require_owner)], response_model=ChapterDetailOut)
def get_chapter(project_id: str, chapter_id: str) -> dict:
    with tenant_session(project_id) as db:
        chapter = db.get(Chapter, uuid.UUID(chapter_id))
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return {
            "id": str(chapter.id),
            "chapter_seq": chapter.chapter_seq,
            "title": chapter.title,
            "status": chapter.status,
            "content": chapter.content,
            "version": chapter.version or 1,
            "summary": chapter.summary,
        }


def main() -> None:
    """`myink-api` 入口（uvicorn 127.0.0.1:8100，仅内部监听）。"""
    import uvicorn

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
