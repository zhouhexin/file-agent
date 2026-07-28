"""正式分类到共享工作副本目录的确定性映射。

目标目录只来自版本化 taxonomy 的 ``organization_path``，LLM、浏览器和用户文本
都不能直接提交物理路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

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

        if relation.status != "CONFIRMED":
            raise CategoryOrganizationPathError("文件分类尚未确认。")
        if (
            relation.working_copy_id != working_copy.id
            or relation.document_id != working_copy.document_id
            or relation.document_version_id != working_copy.current_version_id
        ):
            raise CategoryOrganizationPathError("分类与当前文件版本不一致，请重新确认。")
        taxonomy = load_default_taxonomy()
        if (
            relation.taxonomy_key != taxonomy.key
            or relation.taxonomy_version != taxonomy.version
        ):
            raise CategoryOrganizationPathError(
                "分类目录版本已经更新，请重新确认整理目标。"
            )
        node = _find_category(taxonomy.categories, relation.category_id)
        if node is None:
            raise CategoryOrganizationPathError("当前分类目录中不存在该分类。")
        if not node.organization_path:
            raise CategoryOrganizationPathError(
                "该分类只作为标签使用，尚未配置整理目录。"
            )
        category_path = PurePosixPath(*node.organization_path)
        target_relative_path = (category_path / working_copy.filename).as_posix()
        target_storage_path = (
            PurePosixPath(working_root.relative_storage_path)
            / target_relative_path
        ).as_posix()
        # StorageService 最终解析必须仍位于 WORKING_COPY_STORAGE_ROOT 下；
        # 这里只触发安全校验，不创建目录或产生物理副作用。
        self.storage.working_copy_path(target_storage_path)
        return CategoryOrganizationTarget(
            category_id=relation.category_id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            organization_path=tuple(node.organization_path),
            target_relative_path=target_relative_path,
            target_storage_path=target_storage_path,
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
