"""分类体系配置的数据结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class CategoryNode(BaseModel):
    """配置文件中的一个分类节点。"""

    id: str | None = None
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    children: list["CategoryNode"] = Field(default_factory=list)


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
