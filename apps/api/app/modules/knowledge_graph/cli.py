"""知识图谱投影的显式运维命令入口。"""

from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.modules.knowledge_graph.classification_context import close_graph_resources, get_graph_repository
from app.modules.knowledge_graph.embedding import DocumentEmbeddingService, LocalSentenceTransformerProvider
from app.modules.knowledge_graph.embedding_projection_service import GraphEmbeddingProjectionService
from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry
from app.modules.knowledge_graph.projection_service import GraphProjectionService


def main() -> None:
    """执行显式全量投影或分批向量投影。"""

    parser = argparse.ArgumentParser(description="File Agent knowledge graph maintenance")
    parser.add_argument("command", choices=["sync-all", "sync-embeddings"])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()
    repository = get_graph_repository(settings)
    try:
        with SessionLocal() as db:
            try:
                if args.command == "sync-all":
                    summary = GraphProjectionService(
                        repository=repository,
                        profile_registry=ManagedPathProfileRegistry.load(
                            settings.managed_path_classification_profile_dir
                        ),
                    ).sync_all(db=db)
                else:
                    provider = LocalSentenceTransformerProvider(
                        model_path=settings.graph_embedding_model_path,
                        model_name=settings.graph_embedding_model_name,
                        dimension=settings.graph_embedding_dimension,
                    )
                    repository.ensure_vector_index(
                        index_name=settings.graph_vector_index_name,
                        dimension=settings.graph_embedding_dimension,
                    )
                    summary = GraphEmbeddingProjectionService(
                        embedding_service=DocumentEmbeddingService(
                            repository=repository,
                            provider=provider,
                            embedding_version=settings.graph_embedding_version,
                        ),
                        query_batch_size=settings.graph_projection_batch_size,
                    ).sync(
                        db=db,
                        limit=args.limit or settings.managed_path_vector_pilot_limit,
                    )
                db.commit()
                print(summary)
            except Exception:
                # Neo4j 失败不会使 PostgreSQL 会话失效，提交已记录的 FAILED 投影运行后继续抛错。
                db.commit()
                raise
    finally:
        close_graph_resources()


if __name__ == "__main__":
    main()
