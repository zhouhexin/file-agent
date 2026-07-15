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
from app.modules.classification.managed_catalog import (
    GlobalManagedCategoryCatalog,
    GlobalManagedCategoryCatalogService,
    build_global_managed_category_catalog,
    global_managed_category_id,
)
from app.modules.classification.schemas import CategoryNode, Taxonomy
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.knowledge_graph.repository import GraphRepository
from app.modules.knowledge_graph.classification_context import get_graph_repository
from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry
from app.modules.knowledge_graph.projection_runs import GraphProjectionRunRepository
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
    PathSuggestionProjection,
    ProjectionSummary,
    SuggestedClassificationProjection,
    category_graph_key,
    managed_folder_graph_key,
    normalize_relative_path,
)


CONFIRMED_FEEDBACK_ACTIONS = {"ACCEPT", "ACCEPTED", "CONFIRM", "CONFIRMED"}


class GraphProjectionService:
    """把权威数据幂等投影到图数据库。"""

    def __init__(
        self,
        *,
        repository: GraphRepository,
        profile_registry: ManagedPathProfileRegistry | None = None,
    ) -> None:
        """保存图谱仓库。"""

        self.repository = repository
        self.profile_registry = profile_registry or ManagedPathProfileRegistry()

    def sync_all(self, *, db: Session) -> ProjectionSummary:
        """初始化 schema，并同步 taxonomy、受管目录和可信分类关系。"""

        start = time.perf_counter()
        run_repository = GraphProjectionRunRepository(db)
        run = run_repository.create(projection_type="FULL", projection_version="graph-v2")
        log_event("graph.projection.started", status="RUNNING", message="知识图谱投影开始")
        try:
            self.repository.ensure_schema()
            taxonomy_summary = self.sync_taxonomy(load_default_taxonomy())
            catalog = GlobalManagedCategoryCatalogService(
                db=db,
                profile_registry=self.profile_registry,
            ).load()
            managed_summary = self.sync_managed_paths(
                ManagedFileRepository(db).list_graph_folder_paths(),
                catalog=catalog,
            )
            trusted_summary = self.sync_trusted_classifications(db=db, catalog=catalog)
        except Exception as exc:
            run_repository.fail(run, error=exc)
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
            suggested_relation_count=trusted_summary.suggested_relation_count,
            confirmed_relation_count=trusted_summary.confirmed_relation_count,
        )
        run_repository.complete(
            run,
            nodes_written=summary.category_count + summary.root_count + summary.folder_count + summary.document_version_count,
            relationships_written=(
                summary.relation_count
                + summary.suggested_relation_count
                + summary.confirmed_relation_count
            ),
            items_succeeded=summary.folder_count + summary.document_version_count,
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

    def sync_managed_paths(
        self,
        rows: Iterable[tuple],
        *,
        catalog: GlobalManagedCategoryCatalog | None = None,
    ) -> ProjectionSummary:
        """投影目录层级；弱标签模式只有 CATEGORY 角色生成分类节点。"""

        normalized_rows = list(rows)
        if catalog is None:
            catalog = _catalog_from_graph_rows(
                rows=normalized_rows,
                profile_registry=self.profile_registry,
            )

        root_names: dict[str, str] = {}
        root_modes: dict[str, str] = {}
        folders_by_key: dict[str, ManagedFolderProjection] = {}
        relations_by_child: dict[str, ManagedFolderRelationProjection] = {}
        categories_by_key: dict[str, CategoryProjection] = {}
        category_relations_by_child: dict[str, CategoryRelationProjection] = {}
        folder_category_relations: dict[str, FolderCategoryRelationProjection] = {}

        def ensure_category_path(*, parts: list[str]) -> str:
            """补齐一个全局分类路径并返回叶子图键。"""

            parent_key: str | None = None
            leaf_key = ""
            for index in range(1, len(parts) + 1):
                category_path = "/".join(parts[:index])
                dynamic_id = global_managed_category_id(category_path=parts[:index])
                leaf_key = category_graph_key(
                    taxonomy_key=catalog.taxonomy_key,
                    taxonomy_version=catalog.taxonomy_version,
                    category_id=dynamic_id,
                )
                categories_by_key[leaf_key] = CategoryProjection(
                    graph_key=leaf_key,
                    category_id=dynamic_id,
                    taxonomy_key=catalog.taxonomy_key,
                    taxonomy_version=catalog.taxonomy_version,
                    name=parts[index - 1],
                    path=parts[:index],
                    aliases=[parts[index - 1]],
                )
                if parent_key is not None:
                    category_relations_by_child[leaf_key] = CategoryRelationProjection(
                        parent_graph_key=parent_key,
                        child_graph_key=leaf_key,
                    )
                parent_key = leaf_key
            return leaf_key

        category_by_path = {category.category_path: category for category in catalog.categories}
        for row in normalized_rows:
            if len(row) == 4:
                root_key, display_name, raw_path, _file_count = row
                classification_mode = "PATH_AS_CATEGORY"
            else:
                root_key, display_name, classification_mode, raw_path, _file_count = row
            managed_path = normalize_relative_path(raw_path)
            if not root_key or not managed_path:
                continue
            root_names[root_key] = display_name or root_key
            root_modes[root_key] = classification_mode
            parts = managed_path.split("/")
            parent_folder_key: str | None = None
            for index in range(1, len(parts) + 1):
                relative_path = "/".join(parts[:index])
                folder_key = managed_folder_graph_key(root_key=root_key, relative_path=relative_path)
                folders_by_key[folder_key] = ManagedFolderProjection(
                    graph_key=folder_key,
                    root_key=root_key,
                    relative_path=relative_path,
                    name=parts[index - 1],
                    depth=index,
                    classification_mode=classification_mode,
                )
                relations_by_child[folder_key] = ManagedFolderRelationProjection(
                    root_key=root_key,
                    parent_graph_key=parent_folder_key,
                    child_graph_key=folder_key,
                )
                profile_rule = self.profile_registry.resolve(
                    root_key=root_key,
                    relative_path=relative_path,
                )
                category_parts = tuple(profile_rule.category_path) or tuple(
                    profile_rule.path_prefix.split("/")
                )
                if (
                    classification_mode in {"PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"}
                    and profile_rule.role == "CATEGORY"
                    and category_parts in category_by_path
                ):
                    dynamic_category_key = ensure_category_path(parts=list(category_parts))
                    folder_category_relations[folder_key] = FolderCategoryRelationProjection(
                        folder_graph_key=folder_key,
                        category_graph_key=dynamic_category_key,
                        source_type="managed_path_category_source",
                    )
                parent_folder_key = folder_key

        roots = [
            ManagedRootProjection(
                root_key=root_key,
                display_name=display_name,
                classification_mode=root_modes.get(root_key, "PATH_AS_CATEGORY"),
            )
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

    def sync_trusted_classifications(
        self,
        *,
        db: Session,
        catalog: GlobalManagedCategoryCatalog | None = None,
    ) -> ProjectionSummary:
        """同步人工确认分类和目录弱提示，不把文件位置提升为确认关系。"""

        versions: dict[str, DocumentVersionProjection] = {}
        confirmed: dict[tuple[str, str], ConfirmedClassificationProjection] = {}
        suggested: dict[tuple[str, str], SuggestedClassificationProjection] = {}
        locations: dict[tuple[str, str], LocatedInProjection] = {}
        weak_suggestions: dict[tuple[str, str], PathSuggestionProjection] = {}
        taxonomy = load_default_taxonomy()
        taxonomy_path_keys = _taxonomy_graph_keys_by_path(taxonomy)
        taxonomy_id_keys = _taxonomy_graph_keys_by_id(taxonomy)
        catalog = catalog or GlobalManagedCategoryCatalogService(
            db=db,
            profile_registry=self.profile_registry,
        ).load()
        managed_keys_by_path = {
            category.category_path: category_graph_key(
                taxonomy_key=catalog.taxonomy_key,
                taxonomy_version=catalog.taxonomy_version,
                category_id=category.category_id,
            )
            for category in catalog.categories
        }

        superseded_suggestion_ids = {
            feedback.suggestion_id
            for feedback in (
                db.query(DocumentCategoryFeedback)
                .filter(DocumentCategoryFeedback.is_active.is_(True))
                .filter(
                    DocumentCategoryFeedback.action.in_(
                        {"REJECT", "REJECTED", "CORRECT", "CORRECTED"}
                    )
                )
                .all()
            )
        }
        suggestion_rows = (
            db.query(DocumentCategorySuggestion, Document)
            .join(Document, DocumentCategorySuggestion.document_id == Document.id)
            .filter(DocumentCategorySuggestion.status.in_({"SUGGESTED", "NEEDS_REVIEW"}))
            .all()
        )
        for suggestion, document in suggestion_rows:
            if suggestion.id in superseded_suggestion_ids:
                continue
            graph_key = _suggestion_graph_key(suggestion)
            if graph_key is None:
                graph_key = taxonomy_id_keys.get(str(suggestion.category_id or ""))
            if graph_key is None:
                graph_key = taxonomy_path_keys.get(
                    tuple(str(item) for item in suggestion.category_path_json or [])
                )
            if graph_key is None:
                continue
            version = _document_projection(document)
            versions[version.document_version_id] = version
            suggested[(version.document_version_id, suggestion.id)] = (
                SuggestedClassificationProjection(
                    document_version_id=version.document_version_id,
                    category_graph_key=graph_key,
                    suggestion_id=suggestion.id,
                    confidence=float(suggestion.confidence or 0),
                    status=str(suggestion.status or "SUGGESTED"),
                    source=str(suggestion.source or "rule"),
                )
            )

        feedback_rows = (
            db.query(DocumentCategoryFeedback, DocumentCategorySuggestion, Document)
            .join(DocumentCategorySuggestion, DocumentCategoryFeedback.suggestion_id == DocumentCategorySuggestion.id)
            .join(Document, DocumentCategoryFeedback.document_id == Document.id)
            .filter(DocumentCategoryFeedback.is_active.is_(True))
            .all()
        )
        for feedback, suggestion, document in feedback_rows:
            action = str(feedback.action or "").upper()
            if action not in CONFIRMED_FEEDBACK_ACTIONS | {"CORRECT", "CORRECTED"}:
                continue
            if action in {"CORRECT", "CORRECTED"}:
                graph_key = taxonomy_id_keys.get(str(feedback.corrected_category_id or ""))
                if graph_key is None and feedback.corrected_category_id:
                    corrected_suggestion = (
                        db.query(DocumentCategorySuggestion)
                        .filter(DocumentCategorySuggestion.category_id == feedback.corrected_category_id)
                        .order_by(DocumentCategorySuggestion.created_at.desc())
                        .first()
                    )
                    if corrected_suggestion is not None:
                        graph_key = _suggestion_graph_key(corrected_suggestion)
                if graph_key is None:
                    graph_key = taxonomy_path_keys.get(
                        tuple(str(item) for item in feedback.corrected_category_path_json or [])
                    )
            else:
                graph_key = taxonomy_id_keys.get(str(suggestion.category_id or ""))
                if graph_key is None:
                    graph_key = _suggestion_graph_key(suggestion)
                if graph_key is None:
                    graph_key = taxonomy_path_keys.get(
                        tuple(str(item) for item in suggestion.category_path_json or [])
                    )
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
            .filter(ManagedRoot.enabled.is_(True))
            .filter(ManagedRoot.classification_mode.in_({"PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"}))
            .all()
        )
        for snapshot, managed_file, root, document in snapshot_rows:
            category_path = normalize_relative_path(
                managed_file.category_path or _parent_path(managed_file.relative_path)
            )
            if not category_path:
                continue
            version = _document_projection(document)
            versions[version.document_version_id] = version
            folder_key = managed_folder_graph_key(root_key=root.root_key, relative_path=category_path)
            locations[(version.document_version_id, folder_key)] = LocatedInProjection(
                document_version_id=version.document_version_id,
                folder_graph_key=folder_key,
            )
            profile_rule = self.profile_registry.resolve(
                root_key=root.root_key,
                relative_path=category_path,
            )
            if profile_rule.role != "CATEGORY":
                continue
            dynamic_path = tuple(profile_rule.category_path) or tuple(
                profile_rule.path_prefix.split("/")
            )
            dynamic_key = managed_keys_by_path.get(dynamic_path)
            if dynamic_key is None:
                continue
            profile = self.profile_registry.get(root.root_key)
            weak_suggestions[(version.document_version_id, dynamic_key)] = PathSuggestionProjection(
                document_version_id=version.document_version_id,
                category_graph_key=dynamic_key,
                folder_graph_key=folder_key,
                profile_version=profile.version if profile else "unversioned",
            )

        self.repository.delete_confirmed_classifications_by_source(source_type="user_feedback")
        # 清理旧版本曾经由目录位置错误生成的确认关系。
        self.repository.delete_confirmed_classifications_by_source(source_type="managed_path")
        self.repository.upsert_confirmed_classifications(
            versions=list(versions.values()),
            relations=list(confirmed.values()),
            locations=list(locations.values()),
        )
        self.repository.replace_suggested_classifications(relations=list(suggested.values()))
        self.repository.replace_weak_path_suggestions(relations=list(weak_suggestions.values()))
        return ProjectionSummary(
            document_version_count=len(versions),
            suggested_relation_count=len(suggested),
            confirmed_relation_count=len(confirmed),
        )


def _catalog_from_graph_rows(
    *,
    rows: list[tuple],
    profile_registry: ManagedPathProfileRegistry,
) -> GlobalManagedCategoryCatalog:
    """在独立目录投影时，从相同目录行构建全局候选快照。"""

    source_root_keys: set[str] = set()
    category_rows: list[tuple[str, str, int]] = []
    for row in rows:
        if len(row) == 4:
            root_key, _display_name, raw_path, file_count = row
            classification_mode = "PATH_AS_CATEGORY"
        else:
            root_key, _display_name, classification_mode, raw_path, file_count = row
        if classification_mode != "PATH_AS_CATEGORY":
            continue
        source_root_keys.add(root_key)
        category_rows.append((root_key, normalize_relative_path(raw_path), int(file_count or 0)))
    return build_global_managed_category_catalog(
        category_rows=category_rows,
        source_root_keys=source_root_keys,
        profile_registry=profile_registry,
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


def _taxonomy_graph_keys_by_id(taxonomy: Taxonomy) -> dict[str, str]:
    """建立稳定分类 ID 到图键的映射。"""

    result: dict[str, str] = {}

    def walk(node: CategoryNode, parent_path: list[str]) -> None:
        path = [*parent_path, node.name]
        category_id = node.id or _legacy_category_id(path)
        result[category_id] = category_graph_key(
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            category_id=category_id,
        )
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return result


def _parent_path(relative_path: str) -> str:
    """从文件相对路径提取 POSIX 父目录。"""

    normalized = normalize_relative_path(relative_path)
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def _suggestion_graph_key(suggestion: DocumentCategorySuggestion) -> str | None:
    """从持久化建议的稳定 taxonomy 元数据构造图键。"""

    if not suggestion.category_id or not suggestion.taxonomy_key or not suggestion.taxonomy_version:
        return None
    return category_graph_key(
        taxonomy_key=suggestion.taxonomy_key,
        taxonomy_version=suggestion.taxonomy_version,
        category_id=suggestion.category_id,
    )


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
        profile_registry = ManagedPathProfileRegistry.load(settings.managed_path_classification_profile_dir)
        summary = GraphProjectionService(
            repository=repository,
            profile_registry=profile_registry,
        ).sync_all(db=db)
        db.commit()
        return summary
    except Exception:
        # 详细异常已由 Repository 工厂或投影服务记录；启动链路只执行无损降级。
        db.commit()
        return None
