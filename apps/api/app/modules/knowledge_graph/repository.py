"""知识图谱仓库协议。"""

from __future__ import annotations

from typing import Any, Protocol

from app.modules.knowledge_graph.schemas import (
    CategoryProjection,
    CategoryRelationProjection,
    ConfirmedClassificationProjection,
    DocumentEmbeddingProjection,
    DocumentVersionProjection,
    FolderCategoryRelationProjection,
    GraphCandidateSeed,
    GraphCandidateSupport,
    LocatedInProjection,
    ManagedFolderProjection,
    ManagedFolderRelationProjection,
    ManagedRootProjection,
    PathSuggestionProjection,
    SuggestedClassificationProjection,
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

    def delete_confirmed_classifications_by_source(self, *, source_type: str) -> None:
        """清理可重建来源关系，避免撤销反馈或 Profile 变化后残留。"""

    def replace_suggested_classifications(
        self,
        *,
        relations: list[SuggestedClassificationProjection],
    ) -> None:
        """全量重建正文分类建议关系，避免重分类后旧建议残留。"""

    def replace_weak_path_suggestions(self, *, relations: list[PathSuggestionProjection]) -> None:
        """重建受管目录弱分类关系，避免旧 Profile 关系残留。"""

    def ensure_vector_index(self, *, index_name: str, dimension: int) -> None:
        """确保文档版本向量索引存在。"""

    def read_embedding_metadata(self, *, document_version_id: str) -> dict | None:
        """读取已存向量版本，用于跳过重复计算。"""

    def read_document_embedding(self, *, document_version_id: str) -> list[float] | None:
        """在运行时读取文档向量，不进入 Agent State 或日志。"""

    def upsert_document_embeddings(self, *, projections: list[DocumentEmbeddingProjection]) -> None:
        """幂等写入文档级向量和模型版本。"""

    def get_driver(self) -> Any:
        """向受控 GraphRAG Adapter 提供线程安全 Driver。"""

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
