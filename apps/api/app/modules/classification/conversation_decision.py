"""自然语言分类接受、拒绝和纠正的受控应用服务。

Planner 只传用户原话和后端附件范围。本服务从当前会话、规范工作副本和 taxonomy
中解析真实对象；存在多个文件或建议时只创建选择卡，不猜测最高分候选。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    User,
)
from app.modules.classification.clarification_service import (
    ClassificationClarificationError,
    ClassificationClarificationService,
)
from app.modules.classification.decision_service import ClassificationDecisionService
from app.modules.classification.feedback_schemas import ClassificationFeedbackRequest
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)


class ConversationalClassificationDecisionService:
    """将用户原话收敛为唯一正式分类事务或分类选择卡。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级用户和数据库会话。"""

        self.db = db
        self.user_id = user_id
        self.resolver = CanonicalWorkingFileResolver(db)

    def execute(
        self,
        *,
        action: str,
        message: str,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        """执行唯一决定；歧义时返回持久化选择卡。"""

        user = self.db.get(User, self.user_id)
        run = self.db.get(AgentRun, agent_run_id)
        if user is None:
            return _error("USER_NOT_FOUND", "当前用户不存在。")
        if run is None or run.user_id != user.id or run.conversation_id != conversation_id:
            return _error("AGENT_RUN_SCOPE_INVALID", "分类决定与当前对话范围不一致。")
        suggestions = self._candidate_suggestions(
            document_ids=document_ids,
            conversation_id=conversation_id,
        )
        if not suggestions:
            return _error(
                "CLASSIFICATION_SUGGESTION_NOT_FOUND",
                "没有找到可确认的分类建议，请先读取并分类该文件。",
            )

        original_category_hint = _original_category_hint(message)
        if original_category_hint:
            filtered = [
                item
                for item in suggestions
                if _category_matches_text(item, original_category_hint)
            ]
            if not filtered:
                return _error(
                    "ORIGINAL_CATEGORY_NOT_FOUND",
                    "没有找到与原分类描述一致的建议，请选择具体文件和分类后再更正。",
                )
            suggestions = filtered
        target_category_id = None
        target_category_ids: list[str] = []
        if action == "CORRECT":
            target_category_ids = _resolve_target_categories(message)
            if not target_category_ids:
                return _error(
                    "TARGET_CATEGORY_NOT_FOUND",
                    "没有在当前分类目录中找到目标分类，请换一种分类名称后重试。",
                )
            if len(target_category_ids) == 1:
                target_category_id = target_category_ids[0]

        if len(suggestions) != 1 or len(target_category_ids) > 1:
            try:
                clarification = ClassificationClarificationService(self.db).create(
                    conversation_id=conversation_id,
                    user_id=user.id,
                    agent_run_id=agent_run_id,
                    action=action,
                    suggestion_ids=[item.id for item in suggestions],
                    target_category_id=target_category_id,
                    target_category_ids=target_category_ids or None,
                    relation_role=(
                        "PRIMARY" if action in {"ACCEPT", "CORRECT"} else "RELATED"
                    ),
                )
            except ClassificationClarificationError as exc:
                return _error("CLASSIFICATION_SCOPE_INVALID", str(exc))
            return {
                "ok": True,
                "kind": "classification_clarification",
                "status": "WAITING_SELECTION",
                "classification_clarification": (
                    ClassificationClarificationService.public_payload(clarification)
                ),
                "message": "请选择要确认或纠正的具体文件分类。",
            }

        suggestion = suggestions[0]
        request = ClassificationFeedbackRequest(
            action=action,
            corrected_category_id=target_category_id,
            relation_role=(
                "PRIMARY" if action in {"ACCEPT", "CORRECT"} else "RELATED"
            ),
            agent_run_id=agent_run_id,
            idempotency_key=f"{agent_run_id}:{suggestion.id}:{action}:{target_category_id or ''}",
        )
        response = ClassificationDecisionService(self.db).decide(
            suggestion_id=suggestion.id,
            request=request,
            current_user=user,
        )
        return {
            "ok": True,
            "kind": "classification_decision",
            "status": "COMPLETED",
            "feedback_id": response.id,
            "working_copy_id": response.working_copy_id,
            "document_id": response.document_id,
            "document_version_id": response.document_version_id,
            "action": response.action,
            "changeset_id": response.changeset_id,
            "file_position_changed": False,
            "message": response.user_message,
        }

    def _candidate_suggestions(
        self,
        *,
        document_ids: list[str],
        conversation_id: str,
    ) -> list[DocumentCategorySuggestion]:
        """在后端附件范围或当前会话最近分类结果中读取有效建议。"""

        candidates: list[DocumentCategorySuggestion] = []
        if document_ids:
            direct = (
                self.db.query(DocumentCategorySuggestion)
                .filter(
                    DocumentCategorySuggestion.document_id.in_(
                        list(dict.fromkeys(document_ids))
                    ),
                    DocumentCategorySuggestion.status.in_(
                        {"SUGGESTED", "NEEDS_REVIEW", "AUTO_APPLIED", "CONFIRMED"}
                    ),
                )
                .order_by(
                    DocumentCategorySuggestion.created_at.desc(),
                    DocumentCategorySuggestion.rank.asc(),
                )
                .all()
            )
            candidates.extend(direct)
            # 当前附件可能已经被上下文服务规范化为 WorkingCopy.document_id，也可能仍是
            # 上传 Document；解析后补查另一侧建议，保持上传与共享对象身份一致。
            canonical_document_ids: set[str] = set()
            for document_id in document_ids:
                try:
                    canonical = self.resolver.resolve_document(document_id=document_id)
                except CanonicalWorkingFileError:
                    continue
                canonical_document_ids.add(canonical.working_copy.document_id)
            if canonical_document_ids:
                candidates.extend(
                    self.db.query(DocumentCategorySuggestion)
                    .filter(
                        DocumentCategorySuggestion.document_id.in_(
                            canonical_document_ids
                        ),
                        DocumentCategorySuggestion.status.in_(
                            {"SUGGESTED", "NEEDS_REVIEW", "AUTO_APPLIED", "CONFIRMED"}
                        ),
                    )
                    .order_by(
                        DocumentCategorySuggestion.created_at.desc(),
                        DocumentCategorySuggestion.rank.asc(),
                    )
                    .all()
                )
        else:
            candidates = (
                self.db.query(DocumentCategorySuggestion)
                .join(
                    DocumentClassificationRun,
                    DocumentClassificationRun.id
                    == DocumentCategorySuggestion.classification_run_id,
                )
                .join(AgentRun, AgentRun.id == DocumentClassificationRun.agent_run_id)
                .filter(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.user_id == self.user_id,
                    DocumentCategorySuggestion.status.in_(
                        {"SUGGESTED", "NEEDS_REVIEW", "AUTO_APPLIED", "CONFIRMED"}
                    ),
                )
                .order_by(
                    DocumentCategorySuggestion.created_at.desc(),
                    DocumentCategorySuggestion.rank.asc(),
                )
                .limit(50)
                .all()
            )
        valid: list[DocumentCategorySuggestion] = []
        seen: set[tuple[str, str]] = set()
        for suggestion in candidates:
            try:
                canonical = self.resolver.resolve_suggestion(suggestion)
            except CanonicalWorkingFileError:
                continue
            key = (canonical.working_copy.id, suggestion.category_id)
            if key in seen:
                continue
            seen.add(key)
            valid.append(suggestion)
        return valid


def classification_decision_action(message: str) -> str | None:
    """确定性识别用户是否在接受、拒绝或纠正分类。"""

    compact = re.sub(r"\s+", "", str(message or ""))
    if not compact or "分类" not in compact and not any(
        value in compact for value in ("这个是对的", "这个不是", "不是")
    ):
        return None
    if re.search(r"(?:分类)?(?:改成|改为|更正为)", compact) or (
        "不是" in compact
        and (
            "而是" in compact
            or re.search(r"不是.+(?:，|,|；|;|、)是.+", compact) is not None
        )
    ):
        return "CORRECT"
    if any(
        value in compact
        for value in (
            "分类是对的",
            "分类正确",
            "接受这个分类",
            "确认这个分类",
            "这个分类没问题",
            "这个是对的",
        )
    ):
        return "ACCEPT"
    if any(
        value in compact
        for value in (
            "分类不对",
            "分类错误",
            "拒绝这个分类",
            "不是这个分类",
            "这个不是",
        )
    ):
        return "REJECT"
    return None


def has_organize_by_classification_intent(message: str) -> bool:
    """识别用户明确要求按已确认分类移动共享文件。"""

    compact = re.sub(r"\s+", "", str(message or ""))
    return (
        "分类" in compact
        and any(value in compact for value in ("整理", "归位", "移动到对应目录", "放到对应目录"))
        and not any(value in compact for value in ("不要移动", "位置不变", "只修改分类"))
    )


def _resolve_target_categories(message: str) -> list[str]:
    """从用户原话召回 ACTIVE taxonomy 节点；多义时交给选择卡。"""

    compact = re.sub(r"\s+", "", message)
    matches: list[tuple[int, str]] = []

    def walk(node: CategoryNode, parents: list[str]) -> None:
        path = [*parents, node.name]
        signals = [node.name, "/".join(path), "／".join(path), *node.aliases]
        longest = max((len(value) for value in signals if value and value in compact), default=0)
        if node.id and longest:
            matches.append((longest, node.id))
        for child in node.children:
            walk(child, path)

    for root in load_default_taxonomy().categories:
        walk(root, [])
    if not matches:
        return []
    best_score = max(score for score, _ in matches)
    best = list(dict.fromkeys(category_id for score, category_id in matches if score == best_score))
    return best


def _original_category_hint(message: str) -> str:
    """提取“不是 X”中的原分类短语，仅用于缩小已有建议范围。"""

    compact = re.sub(r"\s+", "", message)
    matched = re.search(r"不是(.+?)(?:，|,|而是|是|$)", compact)
    if matched:
        return matched.group(1).replace("分类", "").replace("材料", "")
    return ""


def _category_matches_text(
    suggestion: DocumentCategorySuggestion, hint: str
) -> bool:
    """按建议名称和完整路径匹配用户明确提到的原分类。"""

    normalized_hint = hint.strip()
    if not normalized_hint:
        return True
    values = [
        suggestion.category_name,
        suggestion.category_id,
        "/".join(str(item) for item in suggestion.category_path_json),
    ]
    return any(
        normalized_hint in value or value in normalized_hint for value in values if value
    )


def _error(code: str, message: str) -> dict[str, Any]:
    """构造 Tool 可持久化且不泄漏内部 ID 的失败结果。"""

    return {
        "ok": False,
        "kind": "classification_decision",
        "status": "FAILED",
        "error": {"code": code, "message": message},
        "message": message,
    }
