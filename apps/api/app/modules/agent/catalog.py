"""请求级 Tool/Skill Catalog 构建与快照校验。

动态 Catalog 只投影已经由代码注册并由部署启用的能力，不加载 Skill 提供的代码，也不允许 LLM 修改
handler。Skill 的机器权限来自 manifest.json，人类规则继续保存在同目录 SKILL.md。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings


DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[5] / "skills"


class SkillManifest(BaseModel):
    """运行时 Skill 的机器可读声明。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    status: Literal["ACTIVE", "DISABLED"] = "ACTIVE"
    description: str = Field(min_length=1)
    trigger_hints: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    risk_ceiling: Literal["low", "medium", "high"] = "medium"
    deployment_gate: Literal["structured_extraction"] | None = None


class CatalogValidationError(ValueError):
    """Tool/Skill Catalog 交叉校验失败。"""


class AgentCatalogService:
    """从请求级 Registry 和已发布 SkillManifest 构造安全 CatalogSnapshot。"""

    def __init__(self, *, registry: Any, skills_root: Path = DEFAULT_SKILLS_ROOT) -> None:
        """保存请求级 Registry；不得把 handler 或运行依赖写入快照。"""

        self.registry = registry
        self.skills_root = skills_root

    def build_snapshot(self) -> dict[str, Any]:
        """加载并交叉校验当前启用 Tool/Skill，返回带稳定指纹的安全快照。"""

        known_tools = self.registry.list_tools(planner_only=False)
        known_tool_names = {
            str(item["name"])
            for item in known_tools
            if str(item.get("name") or "")
        }
        tools = sorted(
            self.registry.list_tools(planner_only=True),
            key=lambda item: str(item.get("name") or ""),
        )
        tool_names = {str(item["name"]) for item in tools}
        skills = self._load_active_skills(
            known_tool_names=known_tool_names,
            adaptive_tool_names=tool_names,
        )
        canonical = {
            "tools": tools,
            "skills": [skill.model_dump() for skill in skills],
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return {
            "catalog_version": "adaptive-catalog-v1",
            "catalog_fingerprint": fingerprint,
            "enabled_tool_names": sorted(tool_names),
            "enabled_skill_ids": [skill.id for skill in skills],
            "tools": tools,
            "skills": [skill.model_dump() for skill in skills],
        }

    def _load_active_skills(
        self,
        *,
        known_tool_names: set[str],
        adaptive_tool_names: set[str],
    ) -> list[SkillManifest]:
        """校验全部引用，并只向 Adaptive Planner 投影已迁移严格输出契约的 Tool。"""

        manifests: list[SkillManifest] = []
        for manifest_path in sorted(self.skills_root.glob("*/manifest.json")):
            skill_dir = manifest_path.parent
            if not (skill_dir / "SKILL.md").is_file():
                raise CatalogValidationError(f"Skill 缺少 SKILL.md: {skill_dir.name}")
            with manifest_path.open("r", encoding="utf-8") as file:
                manifest = SkillManifest.model_validate(json.load(file))
            if manifest.id != skill_dir.name:
                raise CatalogValidationError(
                    f"Skill manifest id 与目录不一致: {manifest.id} != {skill_dir.name}"
                )
            if manifest.status != "ACTIVE":
                continue
            if manifest.deployment_gate == "structured_extraction":
                settings = get_settings()
                if not (
                    settings.structured_extraction_enabled
                    and settings.pp_structure_enabled
                ):
                    continue
            unknown_tools = sorted(
                set(manifest.allowed_tools) - known_tool_names
            )
            if unknown_tools:
                raise CatalogValidationError(
                    f"Skill {manifest.id} 引用了未知 Tool: {unknown_tools}"
                )
            adaptive_allowed_tools = [
                tool_name
                for tool_name in manifest.allowed_tools
                if tool_name in adaptive_tool_names
            ]
            manifests.append(
                manifest.model_copy(update={"allowed_tools": adaptive_allowed_tools})
            )
        return manifests
