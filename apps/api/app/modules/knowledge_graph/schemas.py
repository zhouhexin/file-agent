"""知识图谱投影和分类上下文的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CategoryProjection:
    """准备写入 Neo4j 的分类节点。"""

    graph_key: str
    category_id: str
    taxonomy_key: str
    taxonomy_version: str
    name: str
    path: list[str]
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class CategoryRelationProjection:
    """分类父子关系。"""

    parent_graph_key: str
    child_graph_key: str


@dataclass(frozen=True, slots=True)
class ManagedRootProjection:
    """受管根目录投影，不包含服务器绝对路径。"""

    root_key: str
    display_name: str
    classification_mode: str = "PATH_AS_CATEGORY"
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class ManagedFolderProjection:
    """受管子目录投影。"""

    graph_key: str
    root_key: str
    relative_path: str
    name: str
    depth: int
    classification_mode: str = "PATH_AS_CATEGORY"
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class ManagedFolderRelationProjection:
    """受管目录父子关系。"""

    root_key: str
    parent_graph_key: str | None
    child_graph_key: str


@dataclass(frozen=True, slots=True)
class FolderCategoryRelationProjection:
    """受管目录到动态分类节点的映射。"""

    folder_graph_key: str
    category_graph_key: str
    source_type: str = "managed_path"


@dataclass(frozen=True, slots=True)
class DocumentVersionProjection:
    """文件内容版本投影；当前无独立版本表时可由兼容层使用 document_id。"""

    document_version_id: str
    document_id: str
    sha256: str
    filename: str
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class ConfirmedClassificationProjection:
    """人工确认分类关系投影。"""

    document_version_id: str
    category_graph_key: str
    source_type: str
    source_id: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class LocatedInProjection:
    """文件版本位于受管目录中的关系投影。"""

    document_version_id: str
    folder_graph_key: str
    source_type: str = "managed_path"


@dataclass(frozen=True, slots=True)
class ProjectionSummary:
    """一次图谱投影的数量摘要。"""

    category_count: int = 0
    relation_count: int = 0
    root_count: int = 0
    folder_count: int = 0
    document_version_count: int = 0
    confirmed_relation_count: int = 0


@dataclass(frozen=True, slots=True)
class GraphCandidateSeed:
    """分类服务传给图谱的候选种子，不包含文件正文。"""

    category_id: str
    graph_key: str
    category_path: tuple[str, ...]
    taxonomy_key: str
    taxonomy_version: str
    rule_score: float
    negative_signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphCandidateSupport:
    """图谱返回的候选支持与解释。"""

    category_id: str
    graph_key: str
    category_path: list[str]
    graph_score: float
    confirmed_support_score: float
    support_count: int = 0
    paths: list[dict[str, Any]] = field(default_factory=list)
    taxonomy_key: str = ""
    taxonomy_version: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class GraphClassificationResult:
    """图谱候选查询结果。"""

    status: str
    candidates: list[GraphCandidateSupport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def category_graph_key(*, taxonomy_key: str, taxonomy_version: str, category_id: str) -> str:
    """生成包含 taxonomy 版本的稳定分类图键。"""

    return f"{taxonomy_key}:{taxonomy_version}:{category_id}"


def managed_folder_graph_key(*, root_key: str, relative_path: str) -> str:
    """生成受管目录稳定图键。"""

    normalized = normalize_relative_path(relative_path)
    return f"{root_key}:{normalized}"


def normalize_relative_path(value: str) -> str:
    """把目录路径规范化为 POSIX 相对路径。"""

    return "/".join(part.strip() for part in str(value or "").replace("\\", "/").split("/") if part.strip())
