"""正式分类到共享工作副本目录的确定性映射。

目标目录只来自版本化 taxonomy 的 ``organization_path``，LLM、浏览器和用户文本
都不能直接提交物理路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from app.db.models import DocumentCategory, WorkingCopy, WorkingCopyRoot
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


class CategoryOrganizationPathError(ValueError):
    """分类没有安全整理目录或 taxonomy 版本已经变化。"""


@dataclass(frozen=True)
class CategoryOrganizationTarget:
    """后端确定的分类整理目标。"""

    category_id: str
    taxonomy_key: str
    taxonomy_version: str
    organization_path: tuple[str, ...]
    target_relative_path: str
    target_storage_path: str
    container_segments: tuple[str, ...] = ()


class CategoryOrganizationPathResolver:
    """从正式分类生成当前工作副本根内的安全目标路径。"""

    def __init__(
        self, storage: FileLifecycleStorageService | None = None
    ) -> None:
        """注入 StorageService，测试可使用隔离根目录。"""

        self.storage = storage or FileLifecycleStorageService()

    def resolve(
        self,
        *,
        relation: DocumentCategory,
        working_copy: WorkingCopy,
        working_root: WorkingCopyRoot,
    ) -> CategoryOrganizationTarget:
        """校验正式关系和当前 taxonomy 后生成目标相对路径。"""

        if relation.status not in {"AUTO_APPLIED", "CONFIRMED"}:
            raise CategoryOrganizationPathError("文件分类尚未确认。")
        if (
            relation.working_copy_id != working_copy.id
            or relation.document_id != working_copy.document_id
            or relation.document_version_id != working_copy.current_version_id
        ):
            raise CategoryOrganizationPathError("分类与当前文件版本不一致，请重新确认。")
        return self.resolve_category(
            category_id=relation.category_id,
            taxonomy_key=relation.taxonomy_key,
            taxonomy_version=relation.taxonomy_version,
            working_copy=working_copy,
            working_root=working_root,
        )

    def resolve_category(
        self,
        *,
        category_id: str,
        taxonomy_key: str,
        taxonomy_version: str,
        working_copy: WorkingCopy,
        working_root: WorkingCopyRoot,
        container_segments: tuple[str, ...] | list[str] = (),
    ) -> CategoryOrganizationTarget:
        """从经过策略门槛的稳定分类 ID 解析首次发布目标。"""

        taxonomy = load_default_taxonomy()
        if (
            taxonomy_key != taxonomy.key
            or taxonomy_version != taxonomy.version
        ):
            raise CategoryOrganizationPathError(
                "分类目录版本已经更新，请重新确认整理目标。"
            )
        node = _find_category(taxonomy.categories, category_id)
        if node is None:
            raise CategoryOrganizationPathError("当前分类目录中不存在该分类。")
        if not node.organization_path:
            raise CategoryOrganizationPathError(
                "该分类只作为标签使用，尚未配置整理目录。"
            )
        normalized_container = _validate_container_segments(container_segments)
        category_path = PurePosixPath(*node.organization_path)
        target_relative_path = (
            category_path / PurePosixPath(*normalized_container) / working_copy.filename
        ).as_posix()
        target_storage_path = (
            PurePosixPath(working_root.relative_storage_path)
            / target_relative_path
        ).as_posix()
        # StorageService 最终解析必须仍位于 WORKING_COPY_STORAGE_ROOT 下；
        # 这里只触发安全校验，不创建目录或产生物理副作用。
        self.storage.working_copy_path(target_storage_path)
        return CategoryOrganizationTarget(
            category_id=category_id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            organization_path=tuple(node.organization_path),
            target_relative_path=target_relative_path,
            target_storage_path=target_storage_path,
            container_segments=normalized_container,
        )

    def reverse_resolve_directory(
        self,
        *,
        target_root_key: str,
        target_directory_segments: tuple[str, ...] | list[str],
        working_root: WorkingCopyRoot,
    ) -> CategoryOrganizationTarget:
        """反向解析受控目录到最深 taxonomy 主类和可选收纳段。

        目录仅按完整路径段比较，禁止 ``startswith`` 造成“教学”匹配到
        “教学管理”一类的跨分类误归位。调用方仍须将返回 category_id 写入冻结命令。
        """

        if target_root_key != working_root.root_key:
            raise CategoryOrganizationPathError("目标根不属于当前工作副本。")
        normalized = _validate_container_segments(target_directory_segments)
        if not normalized:
            raise CategoryOrganizationPathError("目标目录不能为空。")
        taxonomy = load_default_taxonomy()
        matches: list[tuple[CategoryNode, tuple[str, ...]]] = []
        for node in _iter_categories(taxonomy.categories):
            if not node.primary_enabled or not node.organization_path:
                continue
            organization_path = tuple(node.organization_path)
            if tuple(normalized[: len(organization_path)]) == organization_path:
                matches.append((node, organization_path))
        if not matches:
            raise CategoryOrganizationPathError("目标目录不属于已注册分类目录。")
        node, organization_path = max(matches, key=lambda item: len(item[1]))
        containers = normalized[len(organization_path) :]
        target_relative_path = (
            PurePosixPath(*normalized) / "__filename_resolved_by_command__"
        ).as_posix()
        return CategoryOrganizationTarget(
            category_id=node.id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            organization_path=organization_path,
            target_relative_path=target_relative_path,
            target_storage_path=(
                PurePosixPath(working_root.relative_storage_path) / target_relative_path
            ).as_posix(),
            container_segments=containers,
        )


def _find_category(
    nodes: list[CategoryNode], category_id: str
) -> CategoryNode | None:
    """按稳定 ID 查找 taxonomy 节点，不接受显示名称模糊匹配。"""

    for node in nodes:
        if node.id == category_id:
            return node
        found = _find_category(node.children, category_id)
        if found is not None:
            return found
    return None


def _iter_categories(nodes: list[CategoryNode]):
    """按 taxonomy 快照顺序遍历节点，不引入显示名匹配。"""

    for node in nodes:
        yield node
        yield from _iter_categories(node.children)


_UNSAFE_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _validate_container_segments(
    raw_segments: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """校验附加收纳段，不允许路径穿越、隐藏目录或跨平台保留名。"""

    if len(raw_segments) > 20:
        raise CategoryOrganizationPathError("收纳目录层级过深。")
    normalized: list[str] = []
    for value in raw_segments:
        segment = str(value).strip()
        if (
            not segment
            or segment != value
            or segment in {".", ".."}
            or segment.startswith(".")
            or segment.rstrip(" .") != segment
            or _UNSAFE_SEGMENT.search(segment)
            or segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED
        ):
            raise CategoryOrganizationPathError("收纳目录段不合法。")
        normalized.append(segment)
    return tuple(normalized)
