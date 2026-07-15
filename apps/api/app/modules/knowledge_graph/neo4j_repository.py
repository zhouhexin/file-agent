"""基于 Neo4j Driver 的参数化图谱仓库。"""

from __future__ import annotations

from typing import Any

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


def _location_row(item: LocatedInProjection) -> dict[str, str]:
    return {
        "document_version_id": item.document_version_id,
        "folder_graph_key": item.folder_graph_key,
        "source_type": item.source_type,
    }


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
