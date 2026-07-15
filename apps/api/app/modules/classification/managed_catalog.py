"""从受管目录构建全局、版本化的业务分类候选集。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from sqlalchemy.orm import Session

from app.db.models import ManagedRoot
from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry
from app.modules.managed_files.repository import ManagedFileRepository


GLOBAL_MANAGED_TAXONOMY_KEY = "managed_global_categories"


@dataclass(frozen=True, slots=True)
class GlobalManagedCategory:
    """一个由多个受管根共享的稳定业务分类。"""

    category_id: str
    category_path: tuple[str, ...]
    name: str
    aliases: tuple[str, ...]
    source_roots: tuple[str, ...]
    source_folders: tuple[str, ...]
    file_count: int


@dataclass(frozen=True, slots=True)
class GlobalManagedCategoryCatalog:
    """一次全局受管分类目录快照。"""

    taxonomy_key: str
    taxonomy_version: str
    categories: tuple[GlobalManagedCategory, ...]
    source_root_count: int

    @property
    def configured(self) -> bool:
        """是否存在启用的受管分类来源根。"""

        return self.source_root_count > 0


class GlobalManagedCategoryCatalogService:
    """把所有分类来源根合并为一个不依赖文件当前位置的候选空间。"""

    def __init__(
        self,
        *,
        db: Session,
        profile_registry: ManagedPathProfileRegistry,
    ) -> None:
        self.db = db
        self.profile_registry = profile_registry

    def load(self) -> GlobalManagedCategoryCatalog:
        """加载 Profile 审核后的分类路径，并按完整路径全局去重。"""

        source_roots = (
            self.db.query(ManagedRoot)
            .filter(ManagedRoot.enabled.is_(True))
            .filter(ManagedRoot.classification_mode == "PATH_AS_CATEGORY")
            .order_by(ManagedRoot.root_key.asc())
            .all()
        )
        return build_global_managed_category_catalog(
            category_rows=(
                (root_key, category_path, file_count)
                for root_key, _display_name, category_path, file_count in ManagedFileRepository(
                    self.db
                ).list_category_paths()
            ),
            source_root_keys=[root.root_key for root in source_roots],
            profile_registry=self.profile_registry,
        )


def build_global_managed_category_catalog(
    *,
    category_rows: Iterable[tuple[str, str, int]],
    source_root_keys: Iterable[str],
    profile_registry: ManagedPathProfileRegistry,
) -> GlobalManagedCategoryCatalog:
    """从目录行构建全局目录，供分类服务和图谱投影共享。"""

    root_keys = tuple(sorted({str(item).strip() for item in source_root_keys if str(item).strip()}))
    source_root_key_set = set(root_keys)
    grouped: dict[tuple[str, ...], dict[str, set[str] | int]] = {}
    for root_key, category_path, file_count in category_rows:
        if root_key not in source_root_key_set:
            continue
        rule = profile_registry.resolve(
            root_key=root_key,
            relative_path=category_path,
        )
        if rule.role != "CATEGORY":
            continue
        normalized_path = _normalize_category_path(rule.category_path or category_path.split("/"))
        if not normalized_path:
            continue
        entry = grouped.setdefault(
            normalized_path,
            {
                "source_roots": set(),
                "source_folders": set(),
                "file_count": 0,
            },
        )
        source_roots = entry["source_roots"]
        source_folders = entry["source_folders"]
        if isinstance(source_roots, set):
            source_roots.add(root_key)
        if isinstance(source_folders, set):
            source_folders.add(category_path)
        entry["file_count"] = int(entry["file_count"]) + int(file_count or 0)

    categories = tuple(
        GlobalManagedCategory(
            category_id=global_managed_category_id(category_path=path),
            category_path=path,
            name=path[-1],
            aliases=(path[-1],),
            source_roots=tuple(sorted(_string_set(entry["source_roots"]))),
            source_folders=tuple(sorted(_string_set(entry["source_folders"]))),
            file_count=int(entry["file_count"]),
        )
        for path, entry in sorted(grouped.items())
    )
    profile_versions = {
        root_key: (
            profile_registry.get(root_key).version
            if profile_registry.get(root_key) is not None
            else "missing"
        )
        for root_key in root_keys
    }
    return GlobalManagedCategoryCatalog(
        taxonomy_key=GLOBAL_MANAGED_TAXONOMY_KEY,
        taxonomy_version=_catalog_version(
            categories=categories,
            profile_versions=profile_versions,
        ),
        categories=categories,
        source_root_count=len(root_keys),
    )


def global_managed_category_id(*, category_path: tuple[str, ...] | list[str]) -> str:
    """仅使用规范化完整路径生成跨受管根稳定分类 ID。"""

    normalized = "/".join(_normalize_category_path(category_path))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"managed.global.{digest}"


def _normalize_category_path(values) -> tuple[str, ...]:
    """规范化分类路径并移除空路径段。"""

    return tuple(str(value).strip() for value in values if str(value).strip())


def _string_set(value: set[str] | int) -> set[str]:
    """把聚合字典中的集合值收窄为字符串集合。"""

    return value if isinstance(value, set) else set()


def _catalog_version(
    *,
    categories: tuple[GlobalManagedCategory, ...],
    profile_versions: dict[str, str],
) -> str:
    """根据 Profile 版本和分类路径生成可复现目录版本。"""

    payload = {
        "profiles": profile_versions,
        "categories": [
            {
                "id": category.category_id,
                "path": category.category_path,
                "roots": category.source_roots,
            }
            for category in categories
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"managed-global-{digest}"
