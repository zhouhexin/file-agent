"""PostgreSQL 与 taxonomy 到 Neo4j 的可重建投影服务。"""

from __future__ import annotations

import hashlib
import time
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    ManagedFile,
    ManagedFileSnapshot,
    ManagedRoot,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.managed_path import (
    TAXONOMY_KEY as MANAGED_TAXONOMY_KEY,
    TAXONOMY_VERSION as MANAGED_TAXONOMY_VERSION,
    managed_category_id,
)
from app.modules.classification.schemas import CategoryNode, Taxonomy
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.knowledge_graph.repository import GraphRepository
from app.modules.knowledge_graph.classification_context import get_graph_repository
from app.modules.knowledge_graph.schemas import (
    CategoryProjection,
    CategoryRelationProjection,
    ConfirmedClassificationProjection,
    DocumentVersionProjection,
    FolderCategoryRelationProjection,
    LocatedInProjection,
    ManagedFolderProjection,
    ManagedFolderRelationProjection,
    ManagedRootProjection,
    ProjectionSummary,
    category_graph_key,
    managed_folder_graph_key,
    normalize_relative_path,
)


CONFIRMED_FEEDBACK_ACTIONS = {"ACCEPT", "ACCEPTED", "CONFIRM", "CONFIRMED"}


class GraphProjectionService:
    """把权威数据幂等投影到图数据库。"""

    def __init__(self, *, repository: GraphRepository) -> None:
        """保存图谱仓库。"""

        self.repository = repository

    def sync_all(self, *, db: Session) -> ProjectionSummary:
        """初始化 schema，并同步 taxonomy、受管目录和可信分类关系。"""

        start = time.perf_counter()
        log_event("graph.projection.started", status="RUNNING", message="知识图谱投影开始")
        try:
            self.repository.ensure_schema()
            taxonomy_summary = self.sync_taxonomy(load_default_taxonomy())
            managed_summary = self.sync_managed_paths(ManagedFileRepository(db).list_category_paths())
            trusted_summary = self.sync_trusted_classifications(db=db)
        except Exception as exc:
            log_event(
                "graph.projection.failed",
                level="ERROR",
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message="知识图谱投影失败",
            )
            raise
        summary = ProjectionSummary(
            category_count=taxonomy_summary.category_count + managed_summary.category_count,
            relation_count=taxonomy_summary.relation_count + managed_summary.relation_count,
            root_count=managed_summary.root_count,
            folder_count=managed_summary.folder_count,
            document_version_count=trusted_summary.document_version_count,
            confirmed_relation_count=trusted_summary.confirmed_relation_count,
        )
        log_event(
            "graph.projection.completed",
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="知识图谱投影完成",
            category_count=summary.category_count,
            relation_count=summary.relation_count,
            document_count=summary.document_version_count,
        )
        return summary

    def sync_taxonomy(self, taxonomy: Taxonomy) -> ProjectionSummary:
        """投影 taxonomy 节点和完整父子关系。"""

        categories: list[CategoryProjection] = []
        relations: list[CategoryRelationProjection] = []

        def walk(node: CategoryNode, parent_path: list[str], parent_key: str | None) -> None:
            """递归构造稳定分类投影。"""

            path = [*parent_path, node.name]
            category_id = node.id or _legacy_category_id(path)
            graph_key = category_graph_key(
                taxonomy_key=taxonomy.key,
                taxonomy_version=taxonomy.version,
                category_id=category_id,
            )
            categories.append(
                CategoryProjection(
                    graph_key=graph_key,
                    category_id=category_id,
                    taxonomy_key=taxonomy.key,
                    taxonomy_version=taxonomy.version,
                    name=node.name,
                    path=path,
                    description=node.description,
                    aliases=list(node.aliases),
                )
            )
            if parent_key is not None:
                relations.append(
                    CategoryRelationProjection(
                        parent_graph_key=parent_key,
                        child_graph_key=graph_key,
                    )
                )
            for child in node.children:
                walk(child, path, graph_key)

        for root in taxonomy.categories:
            walk(root, [], None)
        self.repository.upsert_categories(categories=categories, relations=relations)
        return ProjectionSummary(category_count=len(categories), relation_count=len(relations))

    def sync_managed_paths(self, rows: Iterable[tuple[str, str, str, int]]) -> ProjectionSummary:
        """投影动态分类目录，并补齐叶子路径缺失的所有父目录。"""

        root_names: dict[str, str] = {}
        folders_by_key: dict[str, ManagedFolderProjection] = {}
        relations_by_child: dict[str, ManagedFolderRelationProjection] = {}
        categories_by_key: dict[str, CategoryProjection] = {}
        category_relations_by_child: dict[str, CategoryRelationProjection] = {}
        folder_category_relations: dict[str, FolderCategoryRelationProjection] = {}

        for root_key, display_name, raw_path, _file_count in rows:
            category_path = normalize_relative_path(raw_path)
            if not root_key or not category_path:
                continue
            root_names[root_key] = display_name or root_key
            parts = category_path.split("/")
            parent_folder_key: str | None = None
            parent_category_key: str | None = None
            for index in range(1, len(parts) + 1):
                relative_path = "/".join(parts[:index])
                folder_key = managed_folder_graph_key(root_key=root_key, relative_path=relative_path)
                dynamic_category_id = managed_category_id(root_key=root_key, category_path=relative_path)
                dynamic_category_key = category_graph_key(
                    taxonomy_key=MANAGED_TAXONOMY_KEY,
                    taxonomy_version=MANAGED_TAXONOMY_VERSION,
                    category_id=dynamic_category_id,
                )
                folders_by_key[folder_key] = ManagedFolderProjection(
                    graph_key=folder_key,
                    root_key=root_key,
                    relative_path=relative_path,
                    name=parts[index - 1],
                    depth=index,
                )
                relations_by_child[folder_key] = ManagedFolderRelationProjection(
                    root_key=root_key,
                    parent_graph_key=parent_folder_key,
                    child_graph_key=folder_key,
                )
                categories_by_key[dynamic_category_key] = CategoryProjection(
                    graph_key=dynamic_category_key,
                    category_id=dynamic_category_id,
                    taxonomy_key=MANAGED_TAXONOMY_KEY,
                    taxonomy_version=MANAGED_TAXONOMY_VERSION,
                    name=parts[index - 1],
                    path=parts[:index],
                    aliases=[parts[index - 1]],
                )
                if parent_category_key is not None:
                    category_relations_by_child[dynamic_category_key] = CategoryRelationProjection(
                        parent_graph_key=parent_category_key,
                        child_graph_key=dynamic_category_key,
                    )
                folder_category_relations[folder_key] = FolderCategoryRelationProjection(
                    folder_graph_key=folder_key,
                    category_graph_key=dynamic_category_key,
                )
                parent_folder_key = folder_key
                parent_category_key = dynamic_category_key

        roots = [
            ManagedRootProjection(root_key=root_key, display_name=display_name)
            for root_key, display_name in sorted(root_names.items())
        ]
        categories = list(categories_by_key.values())
        category_relations = list(category_relations_by_child.values())
        self.repository.upsert_categories(categories=categories, relations=category_relations)
        self.repository.upsert_managed_hierarchy(
            roots=roots,
            folders=list(folders_by_key.values()),
            relations=list(relations_by_child.values()),
            folder_category_relations=list(folder_category_relations.values()),
        )
        return ProjectionSummary(
            category_count=len(categories),
            relation_count=len(category_relations) + len(relations_by_child),
            root_count=len(roots),
            folder_count=len(folders_by_key),
        )

    def sync_trusted_classifications(self, *, db: Session) -> ProjectionSummary:
        """同步人工确认分类和已分类受管目录快照，不提升普通建议。"""

        versions: dict[str, DocumentVersionProjection] = {}
        confirmed: dict[tuple[str, str], ConfirmedClassificationProjection] = {}
        locations: dict[tuple[str, str], LocatedInProjection] = {}
        taxonomy_path_keys = _taxonomy_graph_keys_by_path(load_default_taxonomy())

        feedback_rows = (
            db.query(DocumentCategoryFeedback, DocumentCategorySuggestion, Document)
            .join(DocumentCategorySuggestion, DocumentCategoryFeedback.suggestion_id == DocumentCategorySuggestion.id)
            .join(Document, DocumentCategoryFeedback.document_id == Document.id)
            .all()
        )
        for feedback, suggestion, document in feedback_rows:
            if str(feedback.action or "").upper() not in CONFIRMED_FEEDBACK_ACTIONS:
                continue
            graph_key = taxonomy_path_keys.get(tuple(str(item) for item in suggestion.category_path_json or []))
            if graph_key is None:
                continue
            version = _document_projection(document)
            versions[version.document_version_id] = version
            confirmed[(version.document_version_id, graph_key)] = ConfirmedClassificationProjection(
                document_version_id=version.document_version_id,
                category_graph_key=graph_key,
                source_type="user_feedback",
                source_id=feedback.id,
                confidence=1.0,
            )

        snapshot_rows = (
            db.query(ManagedFileSnapshot, ManagedFile, ManagedRoot, Document)
            .join(ManagedFile, ManagedFileSnapshot.managed_file_id == ManagedFile.id)
            .join(ManagedRoot, ManagedFile.root_id == ManagedRoot.id)
            .join(Document, ManagedFileSnapshot.document_id == Document.id)
            .filter(ManagedFileSnapshot.status == "ACTIVE")
            .filter(ManagedFile.status == "ACTIVE")
            .filter(ManagedRoot.classification_mode == "PATH_AS_CATEGORY")
            .all()
        )
        for snapshot, managed_file, root, document in snapshot_rows:
            category_path = normalize_relative_path(managed_file.category_path or "")
            if not category_path:
                continue
            version = _document_projection(document)
            versions[version.document_version_id] = version
            folder_key = managed_folder_graph_key(root_key=root.root_key, relative_path=category_path)
            dynamic_id = managed_category_id(root_key=root.root_key, category_path=category_path)
            dynamic_key = category_graph_key(
                taxonomy_key=MANAGED_TAXONOMY_KEY,
                taxonomy_version=MANAGED_TAXONOMY_VERSION,
                category_id=dynamic_id,
            )
            confirmed[(version.document_version_id, dynamic_key)] = ConfirmedClassificationProjection(
                document_version_id=version.document_version_id,
                category_graph_key=dynamic_key,
                source_type="managed_path",
                source_id=snapshot.id,
                confidence=0.7,
            )
            locations[(version.document_version_id, folder_key)] = LocatedInProjection(
                document_version_id=version.document_version_id,
                folder_graph_key=folder_key,
            )

        self.repository.upsert_confirmed_classifications(
            versions=list(versions.values()),
            relations=list(confirmed.values()),
            locations=list(locations.values()),
        )
        return ProjectionSummary(
            document_version_count=len(versions),
            confirmed_relation_count=len(confirmed),
        )


def _legacy_category_id(path: list[str]) -> str:
    """仅兼容旧 taxonomy 无 ID 节点，不使用显示路径直接充当图键。"""

    digest = hashlib.sha256("/".join(path).encode("utf-8")).hexdigest()[:24]
    return f"legacy.{digest}"


def _taxonomy_graph_keys_by_path(taxonomy: Taxonomy) -> dict[tuple[str, ...], str]:
    """建立分类路径到稳定图键的映射。"""

    result: dict[tuple[str, ...], str] = {}

    def walk(node: CategoryNode, parent_path: list[str]) -> None:
        path = [*parent_path, node.name]
        category_id = node.id or _legacy_category_id(path)
        result[tuple(path)] = category_graph_key(
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            category_id=category_id,
        )
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return result


def _document_projection(document: Document) -> DocumentVersionProjection:
    """兼容当前无独立 DocumentVersion 表的模型；Document 内容不可变时以其 ID 表示版本。"""

    return DocumentVersionProjection(
        document_version_id=document.id,
        document_id=document.id,
        sha256=document.sha256,
        filename=document.original_filename,
    )


def sync_graph_projection_if_enabled(*, db: Session, settings) -> ProjectionSummary | None:
    """按启动配置执行图谱同步；失败只记录日志，不阻断 API 启动。"""

    if not settings.neo4j_sync_enabled:
        return None
    if not settings.neo4j_uri or not settings.neo4j_username or not settings.neo4j_password:
        log_event(
            "graph.projection.failed",
            level="WARNING",
            status="DEGRADED",
            error_code="GRAPH_CONFIGURATION_MISSING",
            message="图谱同步已启用，但 Neo4j 连接配置不完整。",
        )
        return None
    try:
        repository = get_graph_repository(settings)
        return GraphProjectionService(repository=repository).sync_all(db=db)
    except Exception:
        # 详细异常已由 Repository 工厂或投影服务记录；启动链路只执行无损降级。
        return None
