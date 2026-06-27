"""分类体系配置的数据结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CategoryNode(BaseModel):
    """配置文件中的一个分类节点。"""

    name: str
    children: list["CategoryNode"] = Field(default_factory=list)


class Taxonomy(BaseModel):
    """一套可版本化的文件分类体系。"""

    key: str
    name: str
    version: str
    source: str
    categories: list[CategoryNode]
