"""测试专用的真实输入快照、完整性校验及逐调用回放；不访问实时业务库。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal

from pydantic import Field
from sqlalchemy.engine import make_url

from app.modules.evidence_answer.schemas import EvidencePackage
from app.modules.evidence_evaluation.schemas import StrictModel, TemporalFocus
from app.modules.evidence_evaluation.semantic_checker import SYSTEM_PROMPT
from app.modules.evidence_evaluation.validator import validate_issues
from app.modules.evidence_evaluation.version_checker import VersionSnapshot
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import SafeErrorDiagnostic

TEST_ROOT = Path(__file__).resolve().parents[5] / "runtime/evidence-test"
CASE_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


def digest(value: Any) -> str:
    """键排序但不改字符串/数组顺序，散列包含问题而不仅是证据指纹。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_new_json(path: Path, value: Any) -> None:
    """原子发布且不覆盖；本地业务正文文件只允许当前用户读取。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        # hard link 发布具有排他性，读者不会看见半个JSON。
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def private_dataset_path(path: Path | str) -> Path:
    """路径在隔离测试datasets内；解析符号链接后再次检查。"""
    path = Path(path).resolve()
    allowed = (TEST_ROOT / "datasets").resolve()
    if path == allowed or not path.is_relative_to(allowed):
        raise ValueError("FROZEN_PATH_OUTSIDE_TEST_DATASETS")
    return path


def require_test_database(settings) -> None:
    if not (make_url(settings.database_url).database or "").startswith("file_agent_evidence_test"):
        raise ValueError("FROZEN_REQUIRES_TEST_DATABASE")


class CaptureSelection(StrictModel):
    case_id: str = Field(pattern=CASE_ID)
    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[str] = Field(min_length=1, max_length=50)
    source: Literal["real", "derived"] = "real"


class CapturePlan(StrictModel):
    schema_version: Literal["evidence-capture-plan-v1"] = "evidence-capture-plan-v1"
    dataset_id: str = Field(pattern=CASE_ID)
    cases: list[CaptureSelection] = Field(min_length=1, max_length=300)


def runtime_config(settings) -> dict:
    """仅白名单配置，绝不序列化Settings或客户端。"""
    source_dir = Path(__file__).parent
    files = [source_dir / name for name in (
        "schemas.py", "validator.py", "semantic_checker.py", "version_checker.py", "service.py",
    )] + [source_dir.parent / "llm/client.py", source_dir.parent / "evidence_answer/schemas.py", Path(__file__)]
    dependencies = {}
    for name in ("pydantic", "httpx", "sqlalchemy", "jieba"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "unavailable"
    return {
        "model": settings.llm_chat_model, "service_alias": "configured-llm",
        "endpoint_sha256": digest(settings.llm_base_url), "temperature": 0,
        "semantic_prompt_sha256": digest(SYSTEM_PROMPT),
        "max_input_chars": settings.evidence_evaluation_max_input_chars,
        "max_output_tokens": settings.evidence_evaluation_max_output_tokens,
        "max_calls": settings.evidence_evaluation_max_calls,
        "timeout_seconds": settings.evidence_evaluation_timeout_seconds, "max_retries": 0,
        "code_sha256": {str(p.relative_to(source_dir.parent)):
                        hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "dependencies": dependencies,
    }


def validate_versions(package: EvidencePackage, versions: dict) -> dict[str, VersionSnapshot]:
    validate_issues([], package)
    if set(versions) != {item.evidence_id for item in package.evidence_items}:
        raise ValueError("FROZEN_VERSION_KEYS_MISMATCH")
    snapshots = {}
    for key, value in versions.items():
        if set(value) != {"status", "current_version_id"}:
            raise ValueError("FROZEN_VERSION_SCHEMA_INVALID")
        if value["status"] not in {"VERIFIED", "UNVERIFIABLE"}:
            raise ValueError("FROZEN_VERSION_STATUS_INVALID")
        current = value["current_version_id"]
        if current is not None and not isinstance(current, str):
            raise ValueError("FROZEN_VERSION_ID_INVALID")
        if value["status"] == "VERIFIED" and not current:
            raise ValueError("FROZEN_VERSION_ID_REQUIRED")
        snapshots[key] = VersionSnapshot(**value)
    return snapshots


class FrozenCapture:
    """只在明确题目、封闭文档集合都匹配时采集，不能让LLM指定输出路径。"""

    def __init__(self, *, directory: Path, selection: CaptureSelection, dataset_id: str):
        self.directory, self.selection, self.dataset_id = directory, selection, dataset_id

    @classmethod
    def select(cls, settings, package: EvidencePackage, original_question: str):
        configured = settings.evidence_evaluation_capture_plan
        if not configured:
            return None
        require_test_database(settings)
        path = private_dataset_path(configured)
        plan = CapturePlan.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if len({c.case_id for c in plan.cases}) != len(plan.cases):
            raise ValueError("CAPTURE_DUPLICATE_CASE_ID")
        evidence_docs = {item.document_id for item in package.evidence_items}
        scoped = package.scope.get("document_ids")
        docs = set(scoped) if isinstance(scoped, list) and scoped and all(isinstance(v, str) for v in scoped) else evidence_docs
        if not evidence_docs <= docs:
            raise ValueError("CAPTURE_EVIDENCE_OUTSIDE_SCOPE")
        matches = [case for case in plan.cases if case.question.strip() == original_question.strip()
                   and set(case.document_ids) == docs]
        if len(matches) > 1:
            raise ValueError("CAPTURE_AMBIGUOUS_CASE")
        if not matches:
            return None
        return cls(directory=path.parent, selection=matches[0], dataset_id=plan.dataset_id)

    def save_input(self, *, package: EvidencePackage, versions: dict[str, VersionSnapshot],
                   original_question: str, temporal_focus: TemporalFocus, provenance: dict,
                   config: dict) -> Path:
        serialized = {key: asdict(value) for key, value in versions.items()}
        validate_versions(package, serialized)
        data = {"package": package.model_dump(mode="json"), "versions": serialized,
                "context": {"original_question": original_question, "temporal_focus": temporal_focus}}
        fingerprint = digest(data)
        case_dir = private_dataset_path(self.directory / "captures" / self.selection.case_id)
        case_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = case_dir / fingerprint
        if target.exists():
            load_snapshot(target)
            return target
        temporary = Path(tempfile.mkdtemp(prefix=".capture-", dir=case_dir))
        try:
            from app.modules.chunks.service import INDEX_VERSION
            for name, value in data.items():
                write_new_json(temporary / f"{name}.json", value)
            write_new_json(temporary / "manifest.json", {
                "schema_version": "evidence-frozen-input-v1", "dataset_id": self.dataset_id,
                "case_id": self.selection.case_id, "input_sha256": fingerprint,
                "material_source": self.selection.source,
                "evidence_fingerprint": package.evidence_fingerprint,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "version_verification": {
                    "reader": "AuthorizedVersionReader", "index_version": INDEX_VERSION,
                    "checks": ["authorized_scope", "version_binding", "active_state", "current_index_ready"],
                    "verified_evidence_ids": sorted(k for k, v in serialized.items() if v["status"] == "VERIFIED"),
                },
                "provenance": provenance, "capture_config": config, "config_sha256": digest(config),
            })
            load_snapshot(temporary, check_directory_hash=False)
            try:
                temporary.rename(target)
            except OSError:
                if not target.exists():
                    raise
                load_snapshot(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return target


def load_snapshot(path: Path, *, check_directory_hash: bool = True) -> dict:
    path = private_dataset_path(path)
    names = ("package", "versions", "context", "manifest")
    data = {}
    for name in names:
        file = path / f"{name}.json"
        if file.is_symlink():
            raise ValueError("FROZEN_SYMLINK_NOT_ALLOWED")
        data[name] = json.loads(file.read_text(encoding="utf-8"))
    package = EvidencePackage.model_validate(data["package"])
    validate_versions(package, data["versions"])
    context = data["context"]
    if set(context) != {"original_question", "temporal_focus"} or context["temporal_focus"] not in {
        "CURRENT", "HISTORICAL", "UNKNOWN",
    } or not isinstance(context["original_question"], str):
        raise ValueError("FROZEN_CONTEXT_INVALID")
    manifest = data["manifest"]
    if manifest.get("schema_version") != "evidence-frozen-input-v1":
        raise ValueError("FROZEN_SCHEMA_VERSION_INVALID")
    if not re.fullmatch(CASE_ID, manifest.get("case_id", "")):
        raise ValueError("FROZEN_CASE_ID_INVALID")
    fingerprint = digest({name: data[name] for name in ("package", "versions", "context")})
    if manifest["input_sha256"] != fingerprint or (check_directory_hash and path.name != fingerprint):
        raise ValueError("FROZEN_INPUT_HASH_MISMATCH")
    if manifest.get("config_sha256") != digest(manifest["capture_config"]):
        raise ValueError("FROZEN_CONFIG_HASH_MISMATCH")
    return data


class RecordingSemanticClient:
    """只记录评估专用调用；记录失败不能改变模型返回或异常。"""

    def __init__(self, client):
        self.client, self.calls = client, []

    def complete_json_with_usage(self, **kwargs):
        entry = {"request_sha256": digest(kwargs)}
        try:
            response, usage = self.client.complete_json_with_usage(**kwargs)
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            entry["error"] = diagnostic.model_dump(mode="json") if isinstance(diagnostic, SafeErrorDiagnostic) else {
                "category": "TIMEOUT" if isinstance(exc, TimeoutError) else "UNKNOWN_LLM_ERROR",
                "stage": "LLM_REQUEST", "exception_type": "LLMResponseError", "http_status": None,
                "response_shape": None,
            }
            self.calls.append(entry)
            raise
        entry.update(response=response, usage={k: v for k, v in usage.items()
                                               if k in {"input_tokens", "output_tokens"}})
        self.calls.append(entry)
        return response, usage


class RecordedSemanticClient:
    """每批按请求散列匹配，拒绝以一条响应冒充多个不同调用；回放不计真实Token。"""

    def __init__(self, calls: list[dict]):
        self.calls, self.position = calls, 0
        self.alignment_error = None

    def complete_json_with_usage(self, **kwargs):
        if self.position >= len(self.calls):
            self.alignment_error = "REPLAY_CALL_SEQUENCE_EXHAUSTED"
            raise LLMResponseError("REPLAY_CALL_SEQUENCE_EXHAUSTED")
        entry = self.calls[self.position]
        self.position += 1
        if entry["request_sha256"] != digest(kwargs):
            self.alignment_error = "REPLAY_REQUEST_HASH_MISMATCH"
            raise LLMResponseError("REPLAY_REQUEST_HASH_MISMATCH")
        if "error" in entry:
            raise LLMResponseError("RECORDED_MODEL_FAILURE", diagnostic=SafeErrorDiagnostic.model_validate(entry["error"]))
        return entry["response"], {"input_tokens": 0, "output_tokens": 0}
