"""向量存储抽象（§3 决策：VectorStore 接口，pgvector 实现，Milvus 可切换）。

隔离（§14.1 坑 1）：RLS 保证安全，但过滤在排序后——向量查询必须**显式 filter**
（project_id）让 HNSW 走过滤索引，保证召回质量不被他书向量污染 Top-K。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.orm import Session

from myink.models import EmbeddingRow


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, session: Session, *, project_id: uuid.UUID, level: str,
               source_id: uuid.UUID, source_chapter: int | None,
               model_version: str, embedding: list[float]) -> None:
        """写向量（阶段 3 起 Milvus 实现切换点）。"""

    @abstractmethod
    def search(self, session: Session, *, project_id: uuid.UUID, level: str | None,
               embedding: list[float], top_k: int = 20) -> list[tuple[uuid.UUID, float]]:
        """按 project_id 显式过滤召回（返回 [(source_id, distance)]）。"""

    @abstractmethod
    def delete(self, session: Session, *, project_id: uuid.UUID,
               source_chapter: int | None = None, level: str | None = None,
               source_id: uuid.UUID | None = None) -> int:
        """按 project_id(+可选 source_chapter/level/source_id) 删除向量行，返回 rowcount。

        章节重写失效重建（§7.3）：旧事件/事实向量随记忆失效删除，新记忆 persist 时重写。
        """


class PgvectorStore(VectorStore):
    """pgvector 实现（MVP 一库多用，§5.2）。"""

    def upsert(self, session: Session, *, project_id: uuid.UUID, level: str,
               source_id: uuid.UUID, source_chapter: int | None,
               model_version: str, embedding: list[float]) -> None:
        # 幂等：embeddings 没有 (project_id, level, source_id) 唯一约束，重复索引会留下
        # 重复行——同一事件在向量腿里被重复计数、挤占 Top-K（回填命令必须能重跑）。
        # 先 flush 再删：同会话内已 add 未落库的同键行也要一并清掉，否则删不到。
        session.flush()
        self.delete(session, project_id=project_id, level=level, source_id=source_id)
        session.add(EmbeddingRow(
            project_id=project_id, level=level, source_id=source_id,
            source_chapter=source_chapter, model_version=model_version,
            embedding=embedding,
        ))

    def search(self, session: Session, *, project_id: uuid.UUID, level: str | None,
               embedding: list[float], top_k: int = 20) -> list[tuple[uuid.UUID, float]]:
        # 显式 filter 走 HNSW 过滤索引（§14.1 坑 1：避免排序后过滤污染 Top-K）
        query = select(
            EmbeddingRow.source_id,
            EmbeddingRow.embedding.cosine_distance(embedding).label("dist"),
        ).where(EmbeddingRow.project_id == project_id)
        if level:
            query = query.where(EmbeddingRow.level == level)
        query = query.order_by(text("dist")).limit(top_k)
        rows = session.execute(query).all()
        return [(r.source_id, r.dist) for r in rows]

    def delete(self, session: Session, *, project_id: uuid.UUID,
               source_chapter: int | None = None, level: str | None = None,
               source_id: uuid.UUID | None = None) -> int:
        q = sa_delete(EmbeddingRow).where(EmbeddingRow.project_id == project_id)
        if source_chapter is not None:
            q = q.where(EmbeddingRow.source_chapter == source_chapter)
        if level is not None:
            q = q.where(EmbeddingRow.level == level)
        if source_id is not None:
            q = q.where(EmbeddingRow.source_id == source_id)
        return session.execute(q).rowcount or 0
