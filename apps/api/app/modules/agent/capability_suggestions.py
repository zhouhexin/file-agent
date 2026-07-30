"""能力缺口建议的校验、去重、持久化和管理员评审。

LLM 只能产生 CapabilitySuggestionDraft。真正的数据库写入由内部白名单 Tool 调用本服务完成；建议状态
变化不会创建代码、修改 SkillManifest 或启用 Tool。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import CapabilitySuggestion, User, utcnow
from app.modules.agent.planner_contracts import CapabilitySuggestionDraft
from app.modules.auth.dependencies import require_ops_or_admin


SUGGESTION_STATUSES = {
    "NEW",
    "UNDER_REVIEW",
    "ACCEPTED",
    "REJECTED",
    "MERGED",
    "IMPLEMENTED",
}
PRIVILEGED_REVIEW_STATUSES = {"ACCEPTED", "IMPLEMENTED"}
SENSITIVE_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|authorization|password|token)\s*[:=]\s*\S+"
)


class CapabilitySuggestionRecordInput(BaseModel):
    """内部记录 Tool 的严格输入。"""

    model_config = ConfigDict(extra="forbid")

    suggestions: list[CapabilitySuggestionDraft] = Field(max_length=5)
    user_goal: str = Field(min_length=1, max_length=2000)
    catalog_fingerprint: str = Field(min_length=1, max_length=64)
    enabled_tool_names: list[str] = Field(default_factory=list, max_length=200)
    enabled_skill_ids: list[str] = Field(default_factory=list, max_length=100)


class CapabilitySuggestionResponse(BaseModel):
    """管理员建议清单响应，不包含文件正文或内部路径。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    suggestion_kind: str
    title: str
    missing_capability: str
    reason: str
    expected_inputs_json: list[str]
    expected_outputs_json: list[str]
    related_skill_ids_json: list[str]
    confidence: float
    occurrence_count: int
    catalog_fingerprint: str
    status: str
    review_note: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CapabilitySuggestionReviewRequest(BaseModel):
    """管理员评审状态更新。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "UNDER_REVIEW",
        "ACCEPTED",
        "REJECTED",
        "MERGED",
        "IMPLEMENTED",
    ]
    review_note: str = Field(default="", max_length=2000)


class CapabilitySuggestionService:
    """封装建议去重和评审，禁止路由直接拼接数据库写入。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def record(
        self,
        *,
        payload: CapabilitySuggestionRecordInput,
        user_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        """验证、脱敏并按指纹新增或合并建议。"""

        existing_names = {
            _normalize_identifier(item)
            for item in [
                *payload.enabled_tool_names,
                *payload.enabled_skill_ids,
            ]
        }
        recorded_ids: list[str] = []
        rejected_count = 0
        for draft in payload.suggestions:
            if draft.confidence < 0.7:
                rejected_count += 1
                continue
            title = _sanitize_text(draft.title, max_length=200)
            missing_capability = _sanitize_text(
                draft.missing_capability,
                max_length=500,
            )
            reason = _sanitize_text(draft.reason, max_length=1000)
            if not title or not missing_capability or not reason:
                rejected_count += 1
                continue
            if {
                _normalize_identifier(title),
                _normalize_identifier(missing_capability),
            }.intersection(existing_names):
                rejected_count += 1
                continue
            fingerprint = _suggestion_fingerprint(
                suggestion_kind=draft.suggestion_kind,
                missing_capability=missing_capability,
                catalog_fingerprint=payload.catalog_fingerprint,
            )
            suggestion = (
                self.db.query(CapabilitySuggestion)
                .filter(
                    CapabilitySuggestion.deduplication_fingerprint
                    == fingerprint
                )
                .one_or_none()
            )
            if suggestion is None:
                suggestion = CapabilitySuggestion(
                    suggestion_kind=draft.suggestion_kind,
                    title=title,
                    missing_capability=missing_capability,
                    reason=reason,
                    expected_inputs_json=[
                        _sanitize_text(item, max_length=120)
                        for item in draft.expected_inputs
                        if _sanitize_text(item, max_length=120)
                    ],
                    expected_outputs_json=[
                        _sanitize_text(item, max_length=120)
                        for item in draft.expected_outputs
                        if _sanitize_text(item, max_length=120)
                    ],
                    related_skill_ids_json=[
                        item
                        for item in draft.related_skill_ids
                        if item in payload.enabled_skill_ids
                    ],
                    confidence=draft.confidence,
                    deduplication_fingerprint=fingerprint,
                    occurrence_count=1,
                    first_agent_run_id=agent_run_id,
                    latest_agent_run_id=agent_run_id,
                    requested_by_user_id=user_id,
                    catalog_fingerprint=payload.catalog_fingerprint,
                    status="NEW",
                )
                self.db.add(suggestion)
            else:
                suggestion.occurrence_count += 1
                suggestion.latest_agent_run_id = agent_run_id
                suggestion.updated_at = utcnow()
                suggestion.confidence = max(
                    float(suggestion.confidence or 0),
                    draft.confidence,
                )
            self.db.flush()
            recorded_ids.append(suggestion.id)
        return {
            "ok": True,
            "kind": "capability_suggestions_recorded",
            "recorded_ids": recorded_ids,
            "recorded_count": len(recorded_ids),
            "rejected_count": rejected_count,
        }

    def list(
        self,
        *,
        status: str | None,
        limit: int,
    ) -> list[CapabilitySuggestion]:
        """按状态和最近出现时间列出建议。"""

        query = self.db.query(CapabilitySuggestion)
        if status:
            if status not in SUGGESTION_STATUSES:
                raise ValueError("Unsupported capability suggestion status")
            query = query.filter(CapabilitySuggestion.status == status)
        return (
            query.order_by(
                CapabilitySuggestion.updated_at.desc(),
                CapabilitySuggestion.id.desc(),
            )
            .limit(limit)
            .all()
        )

    def get(self, suggestion_id: str) -> CapabilitySuggestion | None:
        """按主键读取建议。"""

        return self.db.get(CapabilitySuggestion, suggestion_id)

    def review(
        self,
        *,
        suggestion: CapabilitySuggestion,
        request: CapabilitySuggestionReviewRequest,
        reviewer: User,
    ) -> CapabilitySuggestion:
        """更新评审状态；只有 admin 可以接受或标记已实现。"""

        if reviewer.role != "admin" and (
            request.status in PRIVILEGED_REVIEW_STATUSES
            or suggestion.status in PRIVILEGED_REVIEW_STATUSES
        ):
            raise PermissionError(
                "Only admin can accept, implement, or change a privileged suggestion"
            )
        suggestion.status = request.status
        suggestion.review_note = _sanitize_text(
            request.review_note,
            max_length=2000,
        )
        suggestion.reviewed_by = reviewer.id
        suggestion.reviewed_at = utcnow()
        suggestion.updated_at = utcnow()
        self.db.flush()
        return suggestion


router = APIRouter(
    prefix="/api/admin/capability-suggestions",
    tags=["admin-capability-suggestions"],
)


@router.get("", response_model=list[CapabilitySuggestionResponse])
def list_capability_suggestions(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> list[CapabilitySuggestion]:
    """允许 ops/admin 查看去重后的能力建议清单。"""

    try:
        return CapabilitySuggestionService(db).list(status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{suggestion_id}", response_model=CapabilitySuggestionResponse)
def get_capability_suggestion(
    suggestion_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> CapabilitySuggestion:
    """允许 ops/admin 查看单条建议详情。"""

    suggestion = CapabilitySuggestionService(db).get(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Capability suggestion not found")
    return suggestion


@router.post(
    "/{suggestion_id}/review",
    response_model=CapabilitySuggestionResponse,
)
def review_capability_suggestion(
    suggestion_id: str,
    request: CapabilitySuggestionReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_ops_or_admin),
) -> CapabilitySuggestion:
    """评审能力建议，但不自动启用任何 Tool 或 Skill。"""

    service = CapabilitySuggestionService(db)
    suggestion = service.get(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Capability suggestion not found")
    try:
        result = service.review(
            suggestion=suggestion,
            request=request,
            reviewer=current_user,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()
    db.refresh(result)
    return result


def _sanitize_text(value: str, *, max_length: int) -> str:
    """移除敏感键值和控制字符，仅保留短业务摘要。"""

    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = SENSITIVE_PATTERN.sub("[REDACTED]", text)
    return text[:max_length].strip()


def _normalize_identifier(value: str) -> str:
    """归一化能力名称，用于拒绝把现有 Tool/Skill 当成新建议。"""

    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _suggestion_fingerprint(
    *,
    suggestion_kind: str,
    missing_capability: str,
    catalog_fingerprint: str,
) -> str:
    """按能力缺口与 Catalog 版本生成指纹，合并不同自然语言表述。"""

    normalized = "|".join(
        [
            suggestion_kind.lower(),
            missing_capability.lower(),
            catalog_fingerprint,
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
