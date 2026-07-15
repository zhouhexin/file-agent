"""基于 Neo4j Driver 的参数化图谱仓库。"""

from __future__ import annotations

import re
from typing import Any

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

try:
    from neo4j import GraphDatabase, Query
except ImportError:  # pragma: no cover - 是否安装 optional dependency 由部署环境决定。
    GraphDatabase = None
    Query = None


class Neo4jDependencyError(RuntimeError):
    """启用图谱但未安装 Neo4j Driver。"""


class Neo4jGraphRepository:
    """使用固定 Cypher 模板维护图谱投影。"""

    def __init__(self, *, driver: Any, database: str = "neo4j", timeout_seconds: int = 3) -> None:
        """保存线程安全 Driver 和查询限制。"""

        self.driver = driver
        self.database = database
        self.timeout_seconds = max(1, timeout_seconds)

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
        timeout_seconds: int = 3,
    ) -> "Neo4jGraphRepository":
        """按显式配置创建 Repository；导入失败时给出可降级错误。"""

        if GraphDatabase is None:
            raise Neo4jDependencyError("未安装 Neo4j optional dependency。")
        driver = GraphDatabase.driver(
            uri,
            auth=(username, password),
            connection_timeout=max(1, timeout_seconds),
        )
        return cls(driver=driver, database=database, timeout_seconds=timeout_seconds)

    def health_check(self) -> dict[str, str]:
        """执行最小只读查询验证连接。"""

        self._execute("RETURN 1 AS ok", {})
        return {"status": "ok"}

    def ensure_schema(self) -> None:
        """创建第一版本唯一约束。"""

        statements = [
            "CREATE CONSTRAINT category_identity IF NOT EXISTS FOR (node:Category) REQUIRE node.graph_key IS UNIQUE",
            "CREATE CONSTRAINT managed_root_identity IF NOT EXISTS FOR (node:ManagedRoot) REQUIRE node.root_key IS UNIQUE",
            "CREATE CONSTRAINT managed_folder_identity IF NOT EXISTS FOR (node:ManagedFolder) REQUIRE node.graph_key IS UNIQUE",
            "CREATE CONSTRAINT document_version_identity IF NOT EXISTS FOR (node:DocumentVersion) REQUIRE node.document_version_id IS UNIQUE",
        ]
        for statement in statements:
            self._execute(statement, {})

    def upsert_categories(
        self,
        *,
        categories: list[CategoryProjection],
        relations: list[CategoryRelationProjection],
    ) -> None:
        """幂等写入分类节点和 `PARENT_OF`。"""

        if categories:
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (node:Category {graph_key: row.graph_key})
                SET node.category_id = row.category_id,
                    node.taxonomy_key = row.taxonomy_key,
                    node.taxonomy_version = row.taxonomy_version,
                    node.name = row.name,
                    node.path = row.path,
                    node.description = row.description,
                    node.aliases = row.aliases,
                    node.is_active = row.is_active,
                    node.updated_at = datetime()
                """,
                {"rows": [_category_row(item) for item in categories]},
            )
        if relations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (parent:Category {graph_key: row.parent_graph_key})
                MATCH (child:Category {graph_key: row.child_graph_key})
                MERGE (parent)-[relation:PARENT_OF]->(child)
                SET relation.updated_at = datetime()
                """,
                {"rows": [_relation_row(item) for item in relations]},
            )

    def upsert_managed_hierarchy(
        self,
        *,
        roots: list[ManagedRootProjection],
        folders: list[ManagedFolderProjection],
        relations: list[ManagedFolderRelationProjection],
        folder_category_relations: list[FolderCategoryRelationProjection],
    ) -> None:
        """幂等写入受管目录层级，不保存服务器绝对路径。"""

        # 目录到分类的映射完全由当前 Profile 重建，先清理可避免规则变更后残留旧关系。
        self._execute(
            "MATCH (:ManagedFolder)-[relation:MAPS_TO]->(:Category) DELETE relation",
            {},
        )
        if roots:
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (root:ManagedRoot {root_key: row.root_key})
                SET root.display_name = row.display_name,
                    root.classification_mode = row.classification_mode,
                    root.is_active = row.is_active,
                    root.updated_at = datetime()
                """,
                {"rows": [_root_row(item) for item in roots]},
            )
        if folders:
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (folder:ManagedFolder {graph_key: row.graph_key})
                SET folder.root_key = row.root_key,
                    folder.relative_path = row.relative_path,
                    folder.name = row.name,
                    folder.depth = row.depth,
                    folder.classification_mode = row.classification_mode,
                    folder.is_active = row.is_active,
                    folder.updated_at = datetime()
                """,
                {"rows": [_folder_row(item) for item in folders]},
            )
        root_relations = [item for item in relations if item.parent_graph_key is None]
        child_relations = [item for item in relations if item.parent_graph_key is not None]
        if root_relations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (root:ManagedRoot {root_key: row.root_key})
                MATCH (folder:ManagedFolder {graph_key: row.child_graph_key})
                MERGE (root)-[:HAS_FOLDER]->(folder)
                """,
                {"rows": [_folder_relation_row(item) for item in root_relations]},
            )
        if child_relations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (parent:ManagedFolder {graph_key: row.parent_graph_key})
                MATCH (child:ManagedFolder {graph_key: row.child_graph_key})
                MERGE (child)-[:CHILD_OF]->(parent)
                """,
                {"rows": [_folder_relation_row(item) for item in child_relations]},
            )
        if folder_category_relations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (folder:ManagedFolder {graph_key: row.folder_graph_key})
                MATCH (category:Category {graph_key: row.category_graph_key})
                MERGE (folder)-[relation:MAPS_TO]->(category)
                SET relation.source_type = row.source_type,
                    relation.updated_at = datetime()
                """,
                {"rows": [_folder_category_row(item) for item in folder_category_relations]},
            )

    def upsert_confirmed_classifications(
        self,
        *,
        versions: list[DocumentVersionProjection],
        relations: list[ConfirmedClassificationProjection],
        locations: list[LocatedInProjection],
    ) -> None:
        """幂等写入文件版本、可信分类和目录归属。"""

        if versions:
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (version:DocumentVersion {document_version_id: row.document_version_id})
                SET version.document_id = row.document_id,
                    version.sha256 = row.sha256,
                    version.filename = row.filename,
                    version.is_active = row.is_active,
                    version.updated_at = datetime()
                """,
                {"rows": [_document_version_row(item) for item in versions]},
            )
        if relations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (version:DocumentVersion {document_version_id: row.document_version_id})
                MATCH (category:Category {graph_key: row.category_graph_key})
                MERGE (version)-[relation:CONFIRMED_AS]->(category)
                SET relation.source_type = row.source_type,
                    relation.source_id = row.source_id,
                    relation.confidence = row.confidence,
                    relation.updated_at = datetime()
                """,
                {"rows": [_confirmed_relation_row(item) for item in relations]},
            )
        if locations:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (version:DocumentVersion {document_version_id: row.document_version_id})
                MATCH (folder:ManagedFolder {graph_key: row.folder_graph_key})
                MERGE (version)-[relation:LOCATED_IN]->(folder)
                SET relation.source_type = row.source_type,
                    relation.updated_at = datetime()
                """,
                {"rows": [_location_row(item) for item in locations]},
            )

    def delete_confirmed_classifications_by_source(self, *, source_type: str) -> None:
        """按受控来源清理可重建可信关系。"""

        if source_type not in {"user_feedback", "managed_path"}:
            raise ValueError("不支持清理该图谱关系来源。")
        self._execute(
            """
            MATCH (:DocumentVersion)-[relation:CONFIRMED_AS]->(:Category)
            WHERE relation.source_type = $source_type
            DELETE relation
            """,
            {"source_type": source_type},
        )

    def replace_suggested_classifications(
        self,
        *,
        relations: list[SuggestedClassificationProjection],
    ) -> None:
        """全量重建 `SUGGESTED_AS`，普通建议不能参与可信传播。"""

        self._execute(
            "MATCH (:DocumentVersion)-[relation:SUGGESTED_AS]->(:Category) DELETE relation",
            {},
        )
        if not relations:
            return
        self._execute(
            """
            UNWIND $rows AS row
            MATCH (version:DocumentVersion {document_version_id: row.document_version_id})
            MATCH (category:Category {graph_key: row.category_graph_key})
            MERGE (version)-[relation:SUGGESTED_AS {suggestion_id: row.suggestion_id}]->(category)
            SET relation.confidence = row.confidence,
                relation.status = row.status,
                relation.source = row.source,
                relation.updated_at = datetime()
            """,
            {"rows": [_suggested_relation_row(item) for item in relations]},
        )

    def replace_weak_path_suggestions(self, *, relations: list[PathSuggestionProjection]) -> None:
        """按 Profile 全量重建 `PATH_SUGGESTS`，它不能升级为可信关系。"""

        self._execute(
            "MATCH (:DocumentVersion)-[relation:PATH_SUGGESTS]->(:Category) DELETE relation",
            {},
        )
        if not relations:
            return
        self._execute(
            """
            UNWIND $rows AS row
            MATCH (version:DocumentVersion {document_version_id: row.document_version_id})
            MATCH (category:Category {graph_key: row.category_graph_key})
            MATCH (folder:ManagedFolder {graph_key: row.folder_graph_key})
            MERGE (version)-[relation:PATH_SUGGESTS]->(category)
            SET relation.folder_graph_key = folder.graph_key,
                relation.profile_version = row.profile_version,
                relation.confidence = row.confidence,
                relation.updated_at = datetime()
            """,
            {"rows": [_path_suggestion_row(item) for item in relations]},
        )

    def ensure_vector_index(self, *, index_name: str, dimension: int) -> None:
        """创建固定维度余弦向量索引，索引名只允许安全标识符。"""

        safe_name = _safe_identifier(index_name)
        safe_dimension = max(1, int(dimension))
        self._execute(
            f"""
            CREATE VECTOR INDEX `{safe_name}` IF NOT EXISTS
            FOR (node:DocumentVersion) ON (node.embedding)
            OPTIONS {{indexConfig: {{
                `vector.dimensions`: {safe_dimension},
                `vector.similarity_function`: 'cosine'
            }}}}
            """,
            {},
        )

    def read_embedding_metadata(self, *, document_version_id: str) -> dict[str, Any] | None:
        """读取文档向量版本，不返回向量数值。"""

        rows = self._execute(
            """
            MATCH (version:DocumentVersion {document_version_id: $document_version_id})
            WHERE version.embedding IS NOT NULL
            RETURN version.sha256 AS sha256,
                   version.embedding_model AS embedding_model,
                   version.embedding_version AS embedding_version,
                   version.embedding_dimension AS embedding_dimension
            LIMIT 1
            """,
            {"document_version_id": document_version_id},
        )
        return rows[0] if rows else None

    def read_document_embedding(self, *, document_version_id: str) -> list[float] | None:
        """读取已存文档向量，仅供受控相似召回使用。"""

        rows = self._execute(
            """
            MATCH (version:DocumentVersion {document_version_id: $document_version_id})
            WHERE version.embedding IS NOT NULL
            RETURN version.embedding AS embedding
            LIMIT 1
            """,
            {"document_version_id": document_version_id},
        )
        if not rows:
            return None
        return [float(item) for item in (rows[0].get("embedding") or [])]

    def upsert_document_embeddings(self, *, projections: list[DocumentEmbeddingProjection]) -> None:
        """写入文档级聚合向量及其可复用版本元数据。"""

        if not projections:
            return
        self._execute(
            """
            UNWIND $rows AS row
            MERGE (version:DocumentVersion {document_version_id: row.document_version_id})
            SET version.document_id = row.document_id,
                version.sha256 = row.sha256,
                version.filename = row.filename,
                version.embedding = row.embedding,
                version.embedding_model = row.embedding_model,
                version.embedding_version = row.embedding_version,
                version.embedding_dimension = row.embedding_dimension,
                version.embedding_successful_chunks = row.successful_chunks,
                version.embedding_failed_chunks = row.failed_chunks,
                version.embedding_updated_at = datetime(),
                version.is_active = true
            """,
            {"rows": [_embedding_row(item) for item in projections]},
        )

    def get_driver(self) -> Any:
        """向受控 GraphRAG Adapter 暴露线程安全 Driver。"""

        return self.driver

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        max_hops: int,
        limit: int,
    ) -> list[GraphCandidateSupport]:
        """查询候选自身和一跳父子节点的可信分类支持。"""

        if not candidates:
            return []
        rows = self._execute(
            """
            UNWIND $seeds AS seed
            MATCH (base:Category {graph_key: seed.graph_key})
            OPTIONAL MATCH path=(base)-[:PARENT_OF*1..2]-(neighbor:Category)
            WHERE length(path) <= $max_hops
            WITH seed, base,
                 [item IN collect(DISTINCT {
                    graph_key: neighbor.graph_key,
                    hops: length(path),
                    relation_type: CASE
                        WHEN startNode(head(relationships(path))) = base
                        THEN CASE WHEN length(path) = 1 THEN 'CHILD' ELSE 'DESCENDANT' END
                        ELSE CASE WHEN length(path) = 1 THEN 'PARENT' ELSE 'ANCESTOR' END
                    END
                 }) WHERE item.graph_key IS NOT NULL | item] AS neighbors
            WITH seed,
                 [{graph_key: base.graph_key, relation_type: 'EXACT', hops: 0}]
                 + neighbors AS candidate_refs
            UNWIND candidate_refs AS candidate_ref
            MATCH (candidate:Category {graph_key: candidate_ref.graph_key})
            OPTIONAL MATCH (supporting:DocumentVersion)-[:CONFIRMED_AS]->(candidate)
            RETURN seed.category_id AS seed_category_id,
                   candidate.graph_key AS graph_key,
                   candidate.category_id AS category_id,
                   candidate.path AS category_path,
                   candidate.taxonomy_key AS taxonomy_key,
                   candidate.taxonomy_version AS taxonomy_version,
                   candidate.name AS name,
                   candidate_ref.relation_type AS relation_type,
                   candidate_ref.hops AS hops,
                   count(DISTINCT supporting) AS support_count
            ORDER BY support_count DESC, candidate.graph_key ASC
            LIMIT $limit
            """,
            {
                "seeds": [_seed_row(item) for item in candidates],
                "max_hops": max(1, min(2, max_hops)),
                "limit": max(1, min(50, limit)),
            },
        )
        return _supports_from_rows(rows)

    def close(self) -> None:
        """关闭 Driver。"""

        self.driver.close()

    def _execute(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """统一执行参数化 Cypher，并设置服务端查询超时。"""

        with self.driver.session(database=self.database) as session:
            query_value = Query(query, timeout=self.timeout_seconds) if Query is not None else query
            result = session.run(query_value, parameters=parameters)
            return [dict(record) for record in result]


def _category_row(item: CategoryProjection) -> dict[str, Any]:
    return {
        "graph_key": item.graph_key,
        "category_id": item.category_id,
        "taxonomy_key": item.taxonomy_key,
        "taxonomy_version": item.taxonomy_version,
        "name": item.name,
        "path": item.path,
        "description": item.description,
        "aliases": item.aliases,
        "is_active": item.is_active,
    }


def _relation_row(item: CategoryRelationProjection) -> dict[str, str]:
    return {"parent_graph_key": item.parent_graph_key, "child_graph_key": item.child_graph_key}


def _root_row(item: ManagedRootProjection) -> dict[str, Any]:
    return {
        "root_key": item.root_key,
        "display_name": item.display_name,
        "classification_mode": item.classification_mode,
        "is_active": item.is_active,
    }


def _folder_row(item: ManagedFolderProjection) -> dict[str, Any]:
    return {
        "graph_key": item.graph_key,
        "root_key": item.root_key,
        "relative_path": item.relative_path,
        "name": item.name,
        "depth": item.depth,
        "classification_mode": item.classification_mode,
        "is_active": item.is_active,
    }


def _folder_relation_row(item: ManagedFolderRelationProjection) -> dict[str, Any]:
    return {
        "root_key": item.root_key,
        "parent_graph_key": item.parent_graph_key,
        "child_graph_key": item.child_graph_key,
    }


def _folder_category_row(item: FolderCategoryRelationProjection) -> dict[str, str]:
    return {
        "folder_graph_key": item.folder_graph_key,
        "category_graph_key": item.category_graph_key,
        "source_type": item.source_type,
    }


def _document_version_row(item: DocumentVersionProjection) -> dict[str, Any]:
    return {
        "document_version_id": item.document_version_id,
        "document_id": item.document_id,
        "sha256": item.sha256,
        "filename": item.filename,
        "is_active": item.is_active,
    }


def _confirmed_relation_row(item: ConfirmedClassificationProjection) -> dict[str, Any]:
    return {
        "document_version_id": item.document_version_id,
        "category_graph_key": item.category_graph_key,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "confidence": item.confidence,
    }


def _suggested_relation_row(item: SuggestedClassificationProjection) -> dict[str, Any]:
    return {
        "document_version_id": item.document_version_id,
        "category_graph_key": item.category_graph_key,
        "suggestion_id": item.suggestion_id,
        "confidence": item.confidence,
        "status": item.status,
        "source": item.source,
    }


def _location_row(item: LocatedInProjection) -> dict[str, str]:
    return {
        "document_version_id": item.document_version_id,
        "folder_graph_key": item.folder_graph_key,
        "source_type": item.source_type,
    }


def _path_suggestion_row(item: PathSuggestionProjection) -> dict[str, Any]:
    return {
        "document_version_id": item.document_version_id,
        "category_graph_key": item.category_graph_key,
        "folder_graph_key": item.folder_graph_key,
        "profile_version": item.profile_version,
        "confidence": item.confidence,
    }


def _embedding_row(item: DocumentEmbeddingProjection) -> dict[str, Any]:
    return {
        "document_version_id": item.document_version_id,
        "document_id": item.document_id,
        "sha256": item.sha256,
        "filename": item.filename,
        "embedding": list(item.embedding),
        "embedding_model": item.embedding_model,
        "embedding_version": item.embedding_version,
        "embedding_dimension": item.embedding_dimension,
        "successful_chunks": item.successful_chunks,
        "failed_chunks": item.failed_chunks,
    }


def _safe_identifier(value: str) -> str:
    """校验不能参数化的 Neo4j 索引标识符。"""

    normalized = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", normalized):
        raise ValueError("Neo4j 索引名只能包含字母、数字和下划线，并且必须以字母开头。")
    return normalized


def _seed_row(item: GraphCandidateSeed) -> dict[str, Any]:
    return {
        "category_id": item.category_id,
        "graph_key": item.graph_key,
        "rule_score": item.rule_score,
    }


def _supports_from_rows(rows: list[dict[str, Any]]) -> list[GraphCandidateSupport]:
    """把查询行合并为稳定候选，避免多个种子产生重复节点。"""

    supports: dict[str, GraphCandidateSupport] = {}
    for row in rows:
        graph_key = str(row.get("graph_key") or "")
        if not graph_key:
            continue
        relation_type = str(row.get("relation_type") or "EXACT")
        hops = int(row.get("hops") or 0)
        support_count = int(row.get("support_count") or 0)
        graph_score = {
            "EXACT": 0.35,
            "CHILD": 0.25,
            "PARENT": 0.15,
            "DESCENDANT": 0.18,
            "ANCESTOR": 0.10,
        }.get(relation_type, 0.08)
        candidate = GraphCandidateSupport(
            category_id=str(row.get("category_id") or ""),
            graph_key=graph_key,
            category_path=[str(item) for item in (row.get("category_path") or [])],
            taxonomy_key=str(row.get("taxonomy_key") or ""),
            taxonomy_version=str(row.get("taxonomy_version") or ""),
            name=str(row.get("name") or ""),
            graph_score=graph_score,
            confirmed_support_score=min(1.0, support_count / 5),
            support_count=support_count,
            paths=[
                {
                    "type": relation_type,
                    "seed_category_id": str(row.get("seed_category_id") or ""),
                    "hops": hops,
                    "support_count": support_count,
                }
            ],
        )
        previous = supports.get(graph_key)
        if previous is None or (
            candidate.graph_score + candidate.confirmed_support_score
            > previous.graph_score + previous.confirmed_support_score
        ):
            supports[graph_key] = candidate
    return list(supports.values())
