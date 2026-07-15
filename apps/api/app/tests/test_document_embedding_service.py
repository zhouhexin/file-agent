"""文档向量分块、复用和失败隔离测试。"""

import math

import pytest

from app.modules.knowledge_graph.embedding import (
    DeterministicEmbeddingProvider,
    DocumentEmbeddingService,
    EmbeddingDependencyError,
    LocalSentenceTransformerProvider,
    split_document_text,
)


class EmbeddingGraphRepository:
    """记录向量元数据和写入的测试仓库。"""

    def __init__(self):
        self.metadata = None
        self.vector = None
        self.projections = []

    def read_embedding_metadata(self, *, document_version_id):
        return self.metadata

    def read_document_embedding(self, *, document_version_id):
        return self.vector

    def upsert_document_embeddings(self, *, projections):
        self.projections.extend(projections)


class PartiallyFailingProvider(DeterministicEmbeddingProvider):
    """遇到指定分块时模拟局部失败。"""

    def embed(self, texts):
        if "FAIL" in texts[0]:
            raise RuntimeError("chunk failed")
        return super().embed(texts)


def test_document_embedding_aggregates_successful_chunks_and_isolates_failure():
    """单块失败不能阻塞其他正文块写入文档向量。"""

    repository = EmbeddingGraphRepository()
    service = DocumentEmbeddingService(
        repository=repository,
        provider=PartiallyFailingProvider(dimension=8),
        embedding_version="v2",
        chunk_size=500,
        chunk_overlap=0,
    )
    text = ("正常正文。" * 90) + ("FAIL" * 130) + ("后续正文。" * 90)

    result = service.embed_document(
        document_id="document-1",
        document_version_id="document-1",
        sha256="a" * 64,
        filename="材料.docx",
        full_text=text,
    )

    assert result.status == "COMPLETED"
    assert result.successful_chunks > 0
    assert result.failed_chunks > 0
    assert len(repository.projections) == 1
    magnitude = math.sqrt(sum(value**2 for value in result.vector))
    assert magnitude == pytest.approx(1.0)


def test_document_embedding_reuses_matching_sha_model_and_version():
    """内容、模型、版本和维度全部一致时必须复用已有向量。"""

    repository = EmbeddingGraphRepository()
    repository.metadata = {
        "sha256": "b" * 64,
        "embedding_model": "deterministic-fake",
        "embedding_version": "v2",
        "embedding_dimension": 4,
    }
    repository.vector = [0.5, 0.5, 0.5, 0.5]
    service = DocumentEmbeddingService(
        repository=repository,
        provider=DeterministicEmbeddingProvider(dimension=4),
        embedding_version="v2",
    )

    result = service.embed_document(
        document_id="document-2",
        document_version_id="document-2",
        sha256="b" * 64,
        filename="材料.pdf",
        full_text="这段正文不应重新计算。",
    )

    assert result.reused is True
    assert repository.projections == []


def test_split_document_text_covers_beginning_middle_and_end():
    """分块必须覆盖长文首、中、尾内容。"""

    text = "START" + ("a" * 800) + "MIDDLE" + ("b" * 800) + "END"
    chunks = split_document_text(text, chunk_size=500, overlap=50)

    assert "START" in chunks[0]
    assert any("MIDDLE" in chunk for chunk in chunks)
    assert "END" in chunks[-1]


def test_local_embedding_provider_rejects_remote_model_identifier(tmp_path):
    """不存在的路径不能被当作远程模型 ID，避免分类时隐式联网下载。"""

    provider = LocalSentenceTransformerProvider(
        model_path=str(tmp_path / "sentence-transformers" / "remote-model"),
        model_name="local-model",
        dimension=384,
    )

    with pytest.raises(EmbeddingDependencyError, match="本地模型路径"):
        provider.embed(["测试正文"])
