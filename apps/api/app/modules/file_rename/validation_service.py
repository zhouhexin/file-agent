"""编排确定性风险分析、可选模型校验和最终后端复核。"""

from __future__ import annotations

from datetime import datetime, timezone
import time

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.modules.file_rename.difference_analyzer import RenameDifferenceAnalyzer
from app.modules.file_rename.llm_validator import LLMRenameValidator, build_title_candidates
from app.modules.file_rename.schemas import FilenameMetadataResult
from app.modules.file_rename.validation_schemas import (
    RenameValidationAudit,
    RenameValidationDecision,
    RenameValidationVerdict,
)
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient


class RenameValidationService:
    """对每条建议执行失败隔离的质量校验。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        validator: LLMRenameValidator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.analyzer = RenameDifferenceAnalyzer()
        self.validator = validator

    def validate(
        self,
        *,
        original_filename: str,
        proposed_filename: str,
        metadata: FilenameMetadataResult,
        arbitration_warnings: list[dict] | None = None,
        allow_llm: bool = True,
    ) -> RenameValidationDecision:
        """返回公开 READY/NEEDS_REVIEW 状态和完整审计。"""

        started = time.perf_counter()
        assessment = self.analyzer.analyze(
            original_filename=original_filename,
            proposed_filename=proposed_filename,
            metadata=metadata,
            arbitration_warnings=arbitration_warnings,
        )
        reasons = [*assessment.reason_codes, *assessment.hard_blockers]
        llm_result = None
        should_call = self._should_call_llm(assessment.risk_score, bool(assessment.hard_blockers))
        enforcement_enabled = (
            self.settings.file_rename_llm_validation_enabled
            and self.settings.file_rename_llm_validation_mode != "off"
        )
        if not enforcement_enabled:
            # 默认关闭时只积累审计数据，确保上线前不改变现有可执行建议集合。
            status = "READY"
        elif assessment.hard_blockers:
            status = "NEEDS_REVIEW"
        elif should_call and not allow_llm:
            status = "NEEDS_REVIEW"
            reasons.append("LLM_VALIDATION_LIMIT_REACHED")
        elif should_call:
            try:
                llm_result = self._validator().validate(
                    original_filename=original_filename,
                    proposed_filename=proposed_filename,
                    metadata=metadata,
                    assessment=assessment,
                )
                status = "READY" if self._is_pass_supported(llm_result, metadata) else "NEEDS_REVIEW"
                reasons.extend(llm_result.reason_codes)
                if status != "READY" and not llm_result.reason_codes:
                    reasons.append("RENAME_DIFFERENCE_UNVERIFIED")
            except (LLMResponseError, RuntimeError, ValueError):
                status = "NEEDS_REVIEW"
                reasons.append("LLM_VALIDATION_UNAVAILABLE")
        elif assessment.risk_score >= self.settings.file_rename_llm_validation_threshold:
            status = "NEEDS_REVIEW"
            reasons.append("RENAME_DIFFERENCE_UNVERIFIED")
        else:
            status = "READY"

        audit = RenameValidationAudit(
            risk_level=assessment.risk_level,
            risk_score=assessment.risk_score,
            reason_codes=list(dict.fromkeys(reasons)),
            hard_blockers=assessment.hard_blockers,
            validation_mode=self.settings.file_rename_llm_validation_mode,
            llm_verdict=llm_result.verdict if llm_result else None,
            validator_model=self.settings.llm_chat_model if llm_result else None,
            prompt_version=self.settings.file_rename_llm_validation_prompt_version if should_call else None,
            validated_at=datetime.now(timezone.utc).isoformat(),
        )
        event = "file_rename.validation.completed"
        level = "INFO"
        if "LLM_VALIDATION_UNAVAILABLE" in reasons or "LLM_VALIDATION_LIMIT_REACHED" in reasons:
            event = "file_rename.validation.degraded"
            level = "WARNING"
        log_event(
            event,
            level=level,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            risk_level=assessment.risk_level.value,
            reason_codes=audit.reason_codes,
            hard_blockers=audit.hard_blockers,
            validation_mode=audit.validation_mode,
            validator_model=audit.validator_model,
        )
        return RenameValidationDecision(
            status=status,
            warning_codes=audit.reason_codes if status == "NEEDS_REVIEW" else [],
            audit=audit,
        )

    def _should_call_llm(self, risk_score: float, has_blockers: bool) -> bool:
        """按配置决定是否调用模型，硬阻断不浪费调用额度。"""

        if has_blockers or not self.settings.file_rename_llm_validation_enabled:
            return False
        if not self.settings.llm_enabled or self.settings.file_rename_llm_validation_mode == "off":
            return False
        if self.settings.file_rename_llm_validation_mode == "all":
            return True
        return risk_score >= self.settings.file_rename_llm_validation_threshold

    def would_call_llm(
        self,
        *,
        original_filename: str,
        proposed_filename: str,
        metadata: FilenameMetadataResult,
        arbitration_warnings: list[dict] | None = None,
    ) -> bool:
        """供批次服务在调用前计算同步模型额度。"""

        assessment = self.analyzer.analyze(
            original_filename=original_filename,
            proposed_filename=proposed_filename,
            metadata=metadata,
            arbitration_warnings=arbitration_warnings,
        )
        return self._should_call_llm(assessment.risk_score, bool(assessment.hard_blockers))

    def _validator(self) -> LLMRenameValidator:
        """按请求配置延迟创建客户端，测试可直接注入 fake。"""

        if self.validator is not None:
            return self.validator
        client = OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.file_rename_llm_validation_timeout_seconds,
        )
        return LLMRenameValidator(client)

    @staticmethod
    def _is_pass_supported(result, metadata: FilenameMetadataResult) -> bool:
        """模型 PASS 后再次验证候选 ID 和字段支持状态。"""

        if result.verdict != RenameValidationVerdict.PASS or not result.title_supported:
            return False
        known_ids = {item.candidate_id for item in build_title_candidates(metadata)}
        if result.selected_title_candidate_id not in known_ids:
            return False
        if metadata.year.value and not result.year_supported:
            return False
        if metadata.document_number.value and not result.document_number_supported:
            return False
        return True
