"""证据包建立后的只读评估编排；结果不能驱动写操作或改变答案。"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings
from app.core.logging import log_event
from app.modules.evidence_answer.schemas import EvidencePackage
from app.modules.evidence_evaluation.schemas import CheckResult, EvaluationResult, TemporalFocus
from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_evaluation.validator import EvaluationValidationError, validate_issues
from app.modules.evidence_evaluation.version_checker import VersionChecker, infer_temporal_focus
from app.modules.llm.client import OpenAICompatibleLLMClient
from app.modules.llm.response_shape import ResponseShape


class EvidenceEvaluationService:
    """可独立离线运行，也可由 EvidenceAnswerService 在 Shadow 模式调用。"""

    def __init__(self, *, version_checker: VersionChecker, semantic_checker: SemanticChecker) -> None:
        """注入只读检查器，数据库/模型客户端不进入 AgentGraphState。"""
        self.version_checker = version_checker
        self.semantic_checker = semantic_checker

    def evaluate(
        self, package: EvidencePackage, *, enabled: bool = True,
        temporal_focus: TemporalFocus | None = None,
    ) -> EvaluationResult:
        """允许多标签；任何失败显式记录，绝不返回 SUFFICIENT。"""
        started = time.perf_counter()
        if not enabled:
            return EvaluationResult(
                evaluation_status="DISABLED", version_check=CheckResult(status="NOT_EVALUATED"),
                semantic_check=CheckResult(status="NOT_EVALUATED"),
                evaluated_evidence_fingerprint=package.evidence_fingerprint,
            )
        try:
            validate_issues([], package)
            version = self.version_checker.check(
                package, temporal_focus=temporal_focus or infer_temporal_focus(package.question),
            )
            # 版本检查也承担授权核对；未通过时不把潜在越权文本送给语义模型。
            semantic = (
                CheckResult(status="NOT_EVALUATED", error_codes=["VERSION_CHECK_REQUIRED"])
                if version.status == "EVALUATION_FAILED" else self.semantic_checker.check(package)
            )
            issues = version.issues + semantic.issues
            validate_issues(issues, package)
        except Exception as exc:
            version = CheckResult(status="EVALUATION_FAILED", error_codes=[
                str(exc) if isinstance(exc, EvaluationValidationError) else "EVALUATION_FAILED",
            ])
            semantic = CheckResult(status="NOT_EVALUATED")
            issues = []
        statuses = {version.status, semantic.status}
        status = (
            "EVALUATION_FAILED" if "EVALUATION_FAILED" in statuses
            else "COMPLETED" if statuses == {"COMPLETED"} else "PARTIAL"
        )
        return EvaluationResult(
            evaluation_status=status, issues=issues, version_check=version, semantic_check=semantic,
            evaluated_evidence_fingerprint=package.evidence_fingerprint,
            limitations=list(dict.fromkeys([
                *package.limitations, *version.limitations, *semantic.limitations,
                "ONLY_CURRENT_EVIDENCE_PACKAGE_CHECKED",
            ])),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def build_semantic_checker(settings: Settings, *, client: Any | None = None) -> SemanticChecker:
    """Shadow 显式开关及全局 LLM 授权同时生效；不触碰主回答客户端。"""
    if not settings.llm_enabled:
        client = None
    elif client is None:
        def observe_response(shape: ResponseShape, http_status: int | None) -> None:
            """只记录评估模型的安全返回形态，包含成功JSON，便于区分后置schema校验失败。"""

            log_event(
                "evidence_evaluation.llm_response_shape", settings=settings,
                status="COMPLETED" if shape.parse_outcome == "MODEL_JSON_OBJECT" else "FAILED",
                http_status=http_status, response_shape=shape.log_fields(),
                message="评估模型返回形态已记录，仅包含类型、长度和固定状态。",
            )

        client = OpenAICompatibleLLMClient(
            api_key=settings.llm_api_key, base_url=settings.llm_base_url,
            model=settings.llm_chat_model,
            timeout_seconds=settings.evidence_evaluation_timeout_seconds, max_retries=0,
            response_observer=observe_response,
        )
    return SemanticChecker(
        client=client, max_input_chars=settings.evidence_evaluation_max_input_chars,
        max_calls=settings.evidence_evaluation_max_calls,
        max_output_tokens=settings.evidence_evaluation_max_output_tokens,
    )


def log_shadow_result(result: EvaluationResult, *, settings: Settings, **context: Any) -> None:
    """仅记录稳定码、引用 ID、指纹和成本，不记录模型理由、问题或原文。"""
    error_codes = result.version_check.error_codes + result.semantic_check.error_codes
    diagnostics = result.version_check.error_diagnostics + result.semantic_check.error_diagnostics
    log_event(
        "evidence_evaluation.shadow", settings=settings, **context,
        level="WARNING" if result.evaluation_status == "EVALUATION_FAILED" else "INFO",
        error_code=error_codes[0] if error_codes else None,
        message=diagnostics[0].log_fields()["message"] if diagnostics else None,
        status=result.evaluation_status, duration_ms=result.duration_ms,
        evaluator_version=result.evaluator_version,
        evidence_fingerprint=result.evaluated_evidence_fingerprint,
        issue_types=sorted({issue.type for issue in result.issues}),
        issues=[issue.model_dump(include={"type", "reason_code", "evidence_ids"}) for issue in result.issues],
        version_status=result.version_check.status, semantic_status=result.semantic_check.status,
        error_codes=error_codes,
        error_details=[item.log_fields() for item in diagnostics],
        # 包自带的 limitations 可能包含业务文本；日志只记录检查器产生的固定码。
        limitation_codes=result.version_check.limitations + result.semantic_check.limitations,
        llm_calls=result.semantic_check.llm_calls,
        input_tokens=result.semantic_check.input_tokens, output_tokens=result.semantic_check.output_tokens,
        token_usage_complete=result.semantic_check.token_usage_complete,
    )
