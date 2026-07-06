"""Agent 固定能力清单读取服务。

LLM 可以判断用户在询问系统能力，但能力内容必须来自本服务读取的固定清单，
不能由模型临时编造。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


CAPABILITY_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"


class AgentCapability(BaseModel):
    """能力清单中的单项能力。"""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    examples: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    capability_keys: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)


class AgentCapabilityCatalog(BaseModel):
    """Agent 固定能力清单结构。"""

    version: str = Field(min_length=1)
    capabilities: list[AgentCapability] = Field(default_factory=list)


def load_agent_capabilities(
    *,
    detail_level: Literal["brief", "full"] = "brief",
    path: Path = CAPABILITY_CATALOG_PATH,
) -> dict:
    """读取固定能力清单，并按详细程度返回结构化结果。"""

    with path.open("r", encoding="utf-8") as file:
        catalog = AgentCapabilityCatalog.model_validate(json.load(file))
    capabilities = [
        capability.model_dump()
        if detail_level == "full"
        else {
            "id": capability.id,
            "name": capability.name,
            "description": capability.description,
            "examples": capability.examples[:2],
        }
        for capability in catalog.capabilities
    ]
    return {
        "ok": True,
        "version": catalog.version,
        "capabilities": capabilities,
    }
