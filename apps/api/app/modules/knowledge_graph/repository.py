"""知识图谱仓库协议。"""

from __future__ import annotations

from typing import Protocol

from app.modules.knowledge_graph.schemas import (
    CategoryProjection,
    CategoryRelationProjection,
    ConfirmedClassificationProjection,
    DocumentVersionProjection,
    FolderCategoryRelationProjection,
    GraphCandidateSeed,
    GraphCandidateSupport,
    LocatedInProjection,
    ManagedFolderProjection,
    ManagedFolderRelationProjection,
    ManagedRootProjection,
)


class GraphRepository(Protocol):
    """图谱持久化和只读分类查询的最小协议。"""

    def health_check(self) -> dict[str, str]:
        """检查图数据库连接。"""

    def ensure_schema(self) -> None:
        """创建第一版本约束和索引。"""

    def upsert_categories(
        self,
        *,
        categories: list[CategoryProjection],
        relations: list[CategoryRelationProjection],
    ) -> None:
        """幂等写入分类节点及其父子关系。"""

    def upsert_managed_hierarchy(
        self,
        *,
        roots: list[ManagedRootProjection],
        folders: list[ManagedFolderProjection],
        relations: list[ManagedFolderRelationProjection],
        folder_category_relations: list[FolderCategoryRelationProjection],
    ) -> None:
        """幂等写入受管目录层级和分类映射。"""

    def upsert_confirmed_classifications(
        self,
        *,
        versions: list[DocumentVersionProjection],
        relations: list[ConfirmedClassificationProjection],
        locations: list[LocatedInProjection],
    ) -> None:
        """幂等写入可信分类和目录归属。"""

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        max_hops: int,
        limit: int,
    ) -> list[GraphCandidateSupport]:
        """查询候选分类邻居和可信样本支持。"""

    def close(self) -> None:
        """关闭底层连接资源。"""
