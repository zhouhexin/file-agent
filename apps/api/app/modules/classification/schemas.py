"""分类体系配置的数据结构。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class CategoryNode(BaseModel):
    """配置文件中的一个分类节点。"""

    id: str | None = None
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    organization_path: list[str] = Field(default_factory=list, max_length=20)
    children: list["CategoryNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_organization_path(self) -> "CategoryNode":
        """校验物理整理目录，禁止 taxonomy 配置越过共享工作根。"""

        total_length = 0
        for raw_segment in self.organization_path:
            segment = str(raw_segment)
            total_length += len(segment)
            basename = segment.split(".", 1)[0].upper()
            if (
                not segment
                or segment in {".", ".."}
                or segment != segment.strip()
                or segment.endswith((".", " "))
                or len(segment) > 80
                or basename in _WINDOWS_RESERVED_NAMES
                or re.search(r'[\x00-\x1f<>:"/\\\\|?*]', segment)
            ):
                raise ValueError(f"非法分类整理目录段：{segment!r}")
        if total_length + max(0, len(self.organization_path) - 1) > 180:
            raise ValueError("分类整理目录路径过长")
        return self


class Taxonomy(BaseModel):
    """一套可版本化的文件分类体系。"""

    key: str
    name: str
    version: str
    source: str
    categories: list[CategoryNode]

    @model_validator(mode="after")
    def validate_unique_category_ids(self) -> "Taxonomy":
        """校验分类 id 唯一；旧版无 id 的配置继续兼容。"""

        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()

        def walk(node: CategoryNode) -> None:
            """递归收集分类 id，空 id 表示旧配置或非稳定节点。"""

            if node.id:
                if node.id in seen_ids:
                    duplicate_ids.add(node.id)
                seen_ids.add(node.id)
            for child in node.children:
                walk(child)

        for category in self.categories:
            walk(category)
        if duplicate_ids:
            raise ValueError(f"分类 id 重复：{', '.join(sorted(duplicate_ids))}")
        return self
