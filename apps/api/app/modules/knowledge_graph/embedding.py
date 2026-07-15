"""完整正文分块、向量聚合和 Neo4j 投影服务。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time
from typing import Any, Protocol

from app.core.logging import log_event
from app.modules.knowledge_graph.repository import GraphRepository
from app.modules.knowledge_graph.schemas import DocumentEmbeddingProjection


class EmbeddingProvider(Protocol):
    """本地或测试 Embedding Provider 的最小协议。"""

    @property
    def model_name(self) -> str:
        """返回模型稳定名称。"""

    @property
    def dimension(self) -> int:
        """返回向量维度。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量生成向量。"""


class EmbeddingDependencyError(RuntimeError):
    """本地 Embedding 模型依赖未安装或未配置。"""


def embedding_capability_status() -> str:
    """检查本地 Embedding 依赖，不加载模型文件。"""

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return "not_installed"
    return "available"


class LocalSentenceTransformerProvider:
    """延迟加载 sentence-transformers 的本地模型。"""

    def __init__(self, *, model_path: str, model_name: str, dimension: int) -> None:
        self.model_path = model_path
        self._model_name = model_name or model_path
        self._dimension = max(1, dimension)
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        """在本机生成向量，不向外部服务发送正文。"""

        if not self.model_path:
            raise EmbeddingDependencyError("未配置 GRAPH_EMBEDDING_MODEL_PATH。")
        model_path = Path(self.model_path).expanduser()
        if not model_path.exists():
            raise EmbeddingDependencyError(
                "GRAPH_EMBEDDING_MODEL_PATH 必须是已存在的本地模型路径。"
            )
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - 由部署环境 optional dependency 决定。
                raise EmbeddingDependencyError("未安装 sentence-transformers。") from exc
            self._model = SentenceTransformer(str(model_path.resolve()))
        vectors = self._model.encode(texts, normalize_embeddings=False)
        result = [[float(value) for value in vector] for vector in vectors]
        if result and len(result[0]) != self.dimension:
            raise ValueError(
                f"Embedding 维度不一致：配置 {self.dimension}，模型返回 {len(result[0])}。"
            )
        return result


class DeterministicEmbeddingProvider:
    """测试使用的稳定哈希向量，不访问外部模型。"""

    def __init__(self, *, dimension: int = 8, model_name: str = "deterministic-fake") -> None:
        self._dimension = max(2, dimension)
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        """由文本哈希稳定生成测试向量。"""

        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append(
                [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimension)]
            )
        return vectors


@dataclass(frozen=True, slots=True)
class DocumentEmbeddingResult:
    """一次文档向量计算的运行时结果。"""

    status: str
    vector: tuple[float, ...] = ()
    successful_chunks: int = 0
    failed_chunks: int = 0
    reused: bool = False
    warning: str | None = None


class DocumentEmbeddingService:
    """将完整正文转为单个文档级向量并写入派生图索引。"""

    def __init__(
        self,
        *,
        repository: GraphRepository,
        provider: EmbeddingProvider,
        embedding_version: str,
        chunk_size: int = 6000,
        chunk_overlap: int = 300,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.embedding_version = embedding_version
        self.chunk_size = max(500, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size // 3))

    def embed_document(
        self,
        *,
        document_id: str,
        document_version_id: str,
        sha256: str,
        filename: str,
        full_text: str,
    ) -> DocumentEmbeddingResult:
        """复用或生成文档向量；单块失败不会阻塞其他块。"""

        start = time.perf_counter()
        metadata = self.repository.read_embedding_metadata(document_version_id=document_version_id)
        if _metadata_matches(
            metadata,
            sha256=sha256,
            model_name=self.provider.model_name,
            embedding_version=self.embedding_version,
            dimension=self.provider.dimension,
        ):
            vector = self.repository.read_document_embedding(document_version_id=document_version_id) or []
            if len(vector) == self.provider.dimension:
                log_event(
                    "graph.embedding.completed",
                    document_id=document_id,
                    status="REUSED",
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    message="复用已有文档向量",
                )
                return DocumentEmbeddingResult(status="COMPLETED", vector=tuple(vector), reused=True)

        chunks = split_document_text(
            full_text,
            chunk_size=self.chunk_size,
            overlap=self.chunk_overlap,
        )
        if not chunks:
            return DocumentEmbeddingResult(status="NEEDS_REVIEW", warning="EMPTY_DOCUMENT_TEXT")

        vectors: list[list[float]] = []
        failed_chunks = 0
        for chunk in chunks:
            try:
                embedded = self.provider.embed([chunk])
                if not embedded or len(embedded[0]) != self.provider.dimension:
                    raise ValueError("Embedding Provider 返回了非法维度。")
                vectors.append(_normalize(embedded[0]))
            except Exception as exc:
                failed_chunks += 1
                log_event(
                    "graph.embedding.chunk_failed",
                    level="WARNING",
                    document_id=document_id,
                    status="FAILED",
                    error_code=exc.__class__.__name__,
                    message="文档向量分块处理失败",
                )

        if not vectors:
            log_event(
                "graph.embedding.failed",
                level="WARNING",
                document_id=document_id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code="ALL_CHUNKS_FAILED",
                message="所有正文分块向量生成失败",
            )
            return DocumentEmbeddingResult(
                status="NEEDS_REVIEW",
                failed_chunks=failed_chunks,
                warning="ALL_CHUNKS_FAILED",
            )

        aggregate = _normalize(
            [sum(vector[index] for vector in vectors) / len(vectors) for index in range(self.provider.dimension)]
        )
        projection = DocumentEmbeddingProjection(
            document_version_id=document_version_id,
            document_id=document_id,
            sha256=sha256,
            filename=filename,
            embedding=tuple(aggregate),
            embedding_model=self.provider.model_name,
            embedding_version=self.embedding_version,
            embedding_dimension=self.provider.dimension,
            successful_chunks=len(vectors),
            failed_chunks=failed_chunks,
        )
        self.repository.upsert_document_embeddings(projections=[projection])
        log_event(
            "graph.embedding.completed",
            document_id=document_id,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="文档向量生成完成",
            chunk_count=len(chunks),
            successful_chunk_count=len(vectors),
            failed_chunk_count=failed_chunks,
        )
        return DocumentEmbeddingResult(
            status="COMPLETED",
            vector=tuple(aggregate),
            successful_chunks=len(vectors),
            failed_chunks=failed_chunks,
        )


def split_document_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """按段落优先切分完整正文，并保留有限重叠。"""

    normalized = str(text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        if end < len(normalized):
            paragraph_end = normalized.rfind("\n", start + chunk_size // 2, end)
            if paragraph_end > start:
                end = paragraph_end
        chunk = normalized[start:end].strip()
        if chunk and (not chunks or chunk != chunks[-1]):
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _normalize(vector: list[float]) -> list[float]:
    """执行 L2 归一化，避免分块长度改变文档级向量尺度。"""

    magnitude = math.sqrt(sum(float(value) ** 2 for value in vector))
    if magnitude == 0:
        raise ValueError("Embedding 向量不能为全零。")
    return [float(value) / magnitude for value in vector]


def _metadata_matches(
    metadata: dict[str, Any] | None,
    *,
    sha256: str,
    model_name: str,
    embedding_version: str,
    dimension: int,
) -> bool:
    """判断 Neo4j 已有向量是否可安全复用。"""

    if not metadata:
        return False
    return (
        str(metadata.get("sha256") or "") == sha256
        and str(metadata.get("embedding_model") or "") == model_name
        and str(metadata.get("embedding_version") or "") == embedding_version
        and int(metadata.get("embedding_dimension") or 0) == dimension
    )
