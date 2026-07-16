"""使用受管文件索引校验 LLM 提议的目录范围。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.managed_files.repository import ManagedFileRepository


@dataclass(frozen=True)
class ManagedDirectoryCandidate:
    """一个已经由后端索引确认存在的逻辑目录。"""

    root_key: str
    path_prefix: str

    def to_dict(self) -> dict[str, str]:
        """返回不包含服务器真实路径的安全候选。"""

        return {
            "root_key": self.root_key,
            "path_prefix": self.path_prefix,
            "display_path": self.path_prefix,
        }


@dataclass(frozen=True)
class ManagedDirectoryScopeResolution:
    """目录范围唯一解析结果或待澄清候选。"""

    status: str
    root_key: str | None = None
    path_prefix: str | None = None
    candidates: list[ManagedDirectoryCandidate] = field(default_factory=list)
    error_code: str | None = None


class ManagedDirectoryScopeResolver:
    """把 LLM 目录候选约束到数据库中真实存在的受管目录。"""

    def __init__(self, repository: ManagedFileRepository) -> None:
        self.repository = repository

    def resolve(
        self,
        *,
        root_key: str | None,
        configured_root_keys: list[str],
        path_prefix: str | None,
        path_candidates: list[str] | None = None,
    ) -> ManagedDirectoryScopeResolution:
        """唯一匹配时返回真实完整路径，多匹配或无匹配时要求澄清。"""

        requested_paths = _unique_paths([path_prefix, *(path_candidates or [])])
        if not requested_paths:
            return ManagedDirectoryScopeResolution(
                status="RESOLVED",
                root_key=root_key,
                path_prefix=None,
            )

        indexed_paths = self.repository.list_directory_paths(
            root_key=root_key,
            root_keys=configured_root_keys if root_key is None else None,
        )
        matches: set[tuple[str, str]] = set()
        for requested_path in requested_paths:
            matches.update(
                _match_directory_path(
                    requested_path=requested_path,
                    indexed_paths=indexed_paths,
                    configured_root_keys=configured_root_keys,
                )
            )

        candidates = [
            ManagedDirectoryCandidate(root_key=item_root_key, path_prefix=item_path)
            for item_root_key, item_path in sorted(matches)
        ]
        if len(candidates) == 1:
            selected = candidates[0]
            return ManagedDirectoryScopeResolution(
                status="RESOLVED",
                root_key=selected.root_key,
                path_prefix=selected.path_prefix,
                candidates=candidates,
            )
        return ManagedDirectoryScopeResolution(
            status="NEEDS_CLARIFICATION",
            candidates=candidates,
            error_code=(
                "MANAGED_DIRECTORY_SCOPE_NOT_FOUND"
                if not candidates
                else "MANAGED_DIRECTORY_SCOPE_AMBIGUOUS"
            ),
        )


def _unique_paths(values: list[str | None]) -> list[str]:
    """规范化候选路径并保持 LLM 给出的优先顺序。"""

    normalized = [str(value).replace("\\", "/").strip().strip("/") for value in values if value]
    return list(dict.fromkeys(value for value in normalized if value))


def _match_directory_path(
    *,
    requested_path: str,
    indexed_paths: list[tuple[str, str]],
    configured_root_keys: list[str],
) -> set[tuple[str, str]]:
    """精确路径优先；否则只允许末级路径的唯一后缀匹配。"""

    normalized = requested_path
    requested_root_key = None
    first_part, separator, remaining = normalized.partition("/")
    if separator and first_part in configured_root_keys:
        requested_root_key = first_part
        normalized = remaining

    eligible = [
        (item_root_key, item_path)
        for item_root_key, item_path in indexed_paths
        if requested_root_key is None or item_root_key == requested_root_key
    ]
    exact = {(item_root_key, item_path) for item_root_key, item_path in eligible if item_path == normalized}
    if exact:
        return exact
    return {
        (item_root_key, item_path)
        for item_root_key, item_path in eligible
        if item_path.endswith(f"/{normalized}")
    }
