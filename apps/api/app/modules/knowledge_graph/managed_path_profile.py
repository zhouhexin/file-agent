"""受管目录角色 Profile 的加载和最长前缀匹配。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from app.modules.knowledge_graph.schemas import normalize_relative_path


PATH_ROLES = {
    "DEPARTMENT",
    "CATEGORY",
    "YEAR",
    "COLLECTION",
    "TEMPORARY",
    "UNKNOWN",
}


@dataclass(frozen=True, slots=True)
class ManagedPathRule:
    """一个受管目录前缀的业务角色声明。"""

    path_prefix: str
    role: str
    category_path: tuple[str, ...] = ()
    recursive: bool = True

    def matches(self, relative_path: str) -> bool:
        """判断路径是否命中当前规则。"""

        normalized = normalize_relative_path(relative_path)
        if normalized == self.path_prefix:
            return True
        return self.recursive and normalized.startswith(f"{self.path_prefix}/")


@dataclass(frozen=True, slots=True)
class ManagedRootClassificationProfile:
    """一个受管根的版本化目录角色配置。"""

    root_key: str
    version: str
    default_role: str = "UNKNOWN"
    rules: tuple[ManagedPathRule, ...] = ()

    def resolve(self, relative_path: str) -> ManagedPathRule:
        """使用最长路径前缀解析目录角色，避免上级规则遮蔽具体子目录。"""

        normalized = normalize_relative_path(relative_path)
        matching = [rule for rule in self.rules if rule.matches(normalized)]
        if matching:
            return max(matching, key=lambda rule: len(rule.path_prefix))
        return ManagedPathRule(path_prefix=normalized, role=self.default_role, recursive=False)


@dataclass(slots=True)
class ManagedPathProfileRegistry:
    """按 root_key 提供目录角色 Profile。"""

    profiles: dict[str, ManagedRootClassificationProfile] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: str | Path) -> "ManagedPathProfileRegistry":
        """从目录加载 JSON Profile；不存在时返回空注册表。"""

        profile_dir = _resolve_profile_dir(directory)
        if not profile_dir.exists():
            return cls()
        profiles: dict[str, ManagedRootClassificationProfile] = {}
        for path in sorted(profile_dir.glob("*.json")):
            if path.name.endswith(".schema.json"):
                continue
            profile = _parse_profile(json.loads(path.read_text(encoding="utf-8")))
            if profile.root_key in profiles:
                raise ValueError(f"受管目录 Profile 重复：{profile.root_key}")
            profiles[profile.root_key] = profile
        return cls(profiles=profiles)

    def get(self, root_key: str) -> ManagedRootClassificationProfile | None:
        """读取指定受管根 Profile。"""

        return self.profiles.get(str(root_key or "").strip())

    def resolve(self, *, root_key: str, relative_path: str) -> ManagedPathRule:
        """解析目录角色；没有 Profile 时安全返回 UNKNOWN。"""

        profile = self.get(root_key)
        if profile is None:
            return ManagedPathRule(
                path_prefix=normalize_relative_path(relative_path),
                role="UNKNOWN",
                recursive=False,
            )
        return profile.resolve(relative_path)


def _parse_profile(payload: dict[str, Any]) -> ManagedRootClassificationProfile:
    """校验单个 Profile 的必要字段和角色枚举。"""

    root_key = str(payload.get("root_key") or "").strip()
    version = str(payload.get("version") or "").strip()
    default_role = str(payload.get("default_role") or "UNKNOWN").strip().upper()
    if not root_key or not version:
        raise ValueError("受管目录 Profile 必须包含 root_key 和 version。")
    if default_role not in PATH_ROLES:
        raise ValueError(f"不支持的目录默认角色：{default_role}")

    rules: list[ManagedPathRule] = []
    for raw_rule in payload.get("rules") or []:
        path_prefix = normalize_relative_path(str(raw_rule.get("path_prefix") or ""))
        role = str(raw_rule.get("role") or "UNKNOWN").strip().upper()
        if not path_prefix:
            raise ValueError("目录角色规则的 path_prefix 不能为空。")
        if role not in PATH_ROLES:
            raise ValueError(f"不支持的目录角色：{role}")
        raw_category_path = raw_rule.get("category_path") or []
        if isinstance(raw_category_path, str):
            category_path = tuple(normalize_relative_path(raw_category_path).split("/"))
        else:
            category_path = tuple(str(item).strip() for item in raw_category_path if str(item).strip())
        rules.append(
            ManagedPathRule(
                path_prefix=path_prefix,
                role=role,
                category_path=category_path,
                recursive=bool(raw_rule.get("recursive", True)),
            )
        )
    return ManagedRootClassificationProfile(
        root_key=root_key,
        version=version,
        default_role=default_role,
        rules=tuple(rules),
    )


def _resolve_profile_dir(directory: str | Path) -> Path:
    """解析 Profile 目录，兼容项目根目录和 apps/api 目录启动。"""

    configured = Path(directory).expanduser()
    if configured.is_absolute():
        return configured
    current = Path.cwd() / configured
    if current.exists():
        return current
    project_root = Path(__file__).resolve().parents[5]
    return project_root / configured
