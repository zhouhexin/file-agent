"""文件检索歧义选择的持久化与权限服务。

选择项由后端生成并保存；浏览器只提交 option_id。该服务不执行检索，也不允许客户端
直接传入短语数组，从而保持 Tool schema 和用户权限边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import FileSearchClarification, utcnow
from app.modules.retrieval.synonym_service import validate_custom_search_phrase


class FileSearchClarificationError(ValueError):
    """检索选择请求无效、过期或越权。"""


@dataclass(frozen=True)
class ResolvedSearchSelection:
    """服务端校验后的检索执行参数。"""

    clarification_id: str
    conversation_id: str
    option_id: str
    display_content: str
    original_query: str
    match_mode: str
    phrases: tuple[str, ...]
    require_body_evidence: bool
    document_ids: tuple[str, ...] = ()
    result_message_id: str | None = None
    result_agent_run_id: str | None = None


class FileSearchClarificationService:
    """管理当前用户会话中的文件检索歧义选择。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        agent_run_id: str | None,
        original_query: str,
        core_phrase: str,
        relation_mode: str,
        options: list[dict[str, Any]],
    ) -> FileSearchClarification:
        """创建选择记录，并把同一会话旧的待选择记录标记为已替代。"""

        now = utcnow()
        (
            self.db.query(FileSearchClarification)
            .filter(
                FileSearchClarification.conversation_id == conversation_id,
                FileSearchClarification.user_id == user_id,
                FileSearchClarification.status == "WAITING_SELECTION",
            )
            .update(
                {
                    FileSearchClarification.status: "SUPERSEDED",
                    FileSearchClarification.resolved_at: now,
                },
                synchronize_session=False,
            )
        )
        record = FileSearchClarification(
            conversation_id=conversation_id,
            user_id=user_id,
            agent_run_id=agent_run_id,
            original_query=original_query,
            core_phrase=core_phrase,
            relation_mode=relation_mode,
            options_json=options,
            status="WAITING_SELECTION",
            expires_at=now + timedelta(hours=24),
        )
        self.db.add(record)
        self.db.flush()
        return record

    def resolve(
        self,
        *,
        clarification_id: str,
        user_id: str,
        option_id: str,
        custom_phrase: str | None = None,
    ) -> ResolvedSearchSelection:
        """校验选择并返回后端保存的执行参数；重复提交同一选项保持幂等。"""

        record = self.db.get(FileSearchClarification, clarification_id)
        if record is None or record.user_id != user_id:
            raise FileSearchClarificationError("检索选择不存在")
        now = utcnow()
        if _is_expired(record.expires_at, now) and record.status == "WAITING_SELECTION":
            record.status = "EXPIRED"
            record.resolved_at = now
            self.db.flush()
            raise FileSearchClarificationError("检索选择已过期，请重新发起查找")
        if record.status == "RESOLVED":
            if record.selected_option_id != option_id:
                raise FileSearchClarificationError("该检索选择已经处理")
            saved = record.resolution_json if isinstance(record.resolution_json, dict) else {}
            saved_phrases = tuple(
                str(value) for value in saved.get("phrases", []) if str(value)
            )
            saved_document_ids = tuple(
                str(value) for value in saved.get("document_ids", []) if str(value)
            )
            if not saved_phrases and not saved_document_ids:
                raise FileSearchClarificationError("已处理选择缺少执行记录")
            if option_id == "custom" and custom_phrase:
                requested = validate_custom_search_phrase(custom_phrase)
                if saved_phrases != (requested,):
                    raise FileSearchClarificationError("该检索选择已经使用其他自定义短语处理")
            return ResolvedSearchSelection(
                clarification_id=record.id,
                conversation_id=record.conversation_id,
                option_id=option_id,
                display_content=str(saved.get("display_content") or "继续查找"),
                original_query=record.original_query,
                match_mode=str(saved.get("match_mode") or "LITERAL"),
                phrases=saved_phrases,
                require_body_evidence=bool(saved.get("require_body_evidence", False)),
                document_ids=saved_document_ids,
                result_message_id=record.result_message_id,
                result_agent_run_id=record.result_agent_run_id,
            )
        elif record.status != "WAITING_SELECTION":
            raise FileSearchClarificationError("该检索选择已失效")

        option = next(
            (
                item
                for item in record.options_json
                if isinstance(item, dict) and str(item.get("id")) == option_id
            ),
            None,
        )
        if option is None:
            raise FileSearchClarificationError("选择项不属于当前检索")

        if record.relation_mode in {
            "DOCUMENT_SELECTION",
            "RENAME_DOCUMENT_SELECTION",
        }:
            document_id = str(option.get("document_id") or "")
            if not document_id:
                raise FileSearchClarificationError("文件选择项缺少有效文件")
            phrases = ()
            document_ids = (document_id,)
            if record.relation_mode == "RENAME_DOCUMENT_SELECTION":
                source_filename = str(option.get("source_filename") or "")
                target_filename = str(option.get("target_filename") or "")
                if not source_filename or not target_filename:
                    raise FileSearchClarificationError("重命名选择项缺少文件名")
                match_mode = "RENAME_DOCUMENT_SELECTION"
                require_body = False
                display_content = (
                    f"文件“{source_filename}”更正为“{target_filename}”"
                )
            else:
                match_mode = "AUTO"
                require_body = True
                display_content = (
                    f"使用“{str(option.get('label') or '所选文件')}”继续回答"
                )
        elif option_id == "custom":
            phrase = validate_custom_search_phrase(custom_phrase or "")
            phrases = (phrase,)
            document_ids = ()
            match_mode = "LITERAL"
            require_body = True
            display_content = f"按原文短语“{phrase}”继续查找"
        else:
            phrases = tuple(
                str(value).strip()
                for value in option.get("phrases", [])
                if str(value).strip()
            )
            if not phrases:
                raise FileSearchClarificationError("选择项缺少有效查找范围")
            match_mode = str(option.get("match_mode") or "LITERAL")
            require_body = bool(option.get("require_body_evidence", False))
            document_ids = ()
            display_content = str(option.get("display_content") or option.get("label") or "继续查找")

        record.status = "RESOLVED"
        record.selected_option_id = option_id
        record.resolution_json = {
            "display_content": display_content,
            "match_mode": match_mode,
            "phrases": list(phrases),
            "require_body_evidence": require_body,
            "document_ids": list(document_ids),
        }
        record.resolved_at = now
        self.db.flush()
        return ResolvedSearchSelection(
            clarification_id=record.id,
            conversation_id=record.conversation_id,
            option_id=option_id,
            display_content=display_content,
            original_query=record.original_query,
            match_mode=match_mode,
            phrases=phrases,
            require_body_evidence=require_body,
            document_ids=document_ids,
        )

    def mark_execution_result(
        self,
        *,
        clarification_id: str,
        user_id: str,
        message_id: str,
        agent_run_id: str,
    ) -> None:
        """绑定选择产生的唯一消息和 AgentRun，供网络重试直接复用。

        选择参数幂等还不够；如果每次重试都创建新消息，用户仍会看到重复回答。
        因此只有首次成功执行可以写入结果标识，后续请求必须读取同一结果。
        """

        record = self.db.get(FileSearchClarification, clarification_id)
        if (
            record is None
            or record.user_id != user_id
            or record.status != "RESOLVED"
        ):
            raise FileSearchClarificationError("检索选择尚未完成")
        if record.result_message_id or record.result_agent_run_id:
            if (
                record.result_message_id != message_id
                or record.result_agent_run_id != agent_run_id
            ):
                raise FileSearchClarificationError("该检索选择已经生成处理结果")
            return
        record.result_message_id = message_id
        record.result_agent_run_id = agent_run_id
        self.db.flush()

    def get_public(
        self,
        *,
        clarification_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """读取当前用户可见的选择状态，供页面刷新后恢复卡片。"""

        record = self.db.get(FileSearchClarification, clarification_id)
        if record is None or record.user_id != user_id:
            raise FileSearchClarificationError("检索选择不存在")
        if (
            record.status == "WAITING_SELECTION"
            and _is_expired(record.expires_at, utcnow())
        ):
            record.status = "EXPIRED"
            record.resolved_at = utcnow()
            self.db.flush()
        return self.public_payload(record)

    def resolve_from_text(
        self,
        *,
        conversation_id: str,
        user_id: str,
        message: str,
    ) -> ResolvedSearchSelection | None:
        """把明确的自然语言选择映射到当前会话最新待选择项。

        普通的新查询不会被强行解释为选择；只有“按同义表达、只查原文、只查某主题”
        等明确答复才会消费待选择记录。
        """

        record = (
            self.db.query(FileSearchClarification)
            .filter(
                FileSearchClarification.conversation_id == conversation_id,
                FileSearchClarification.user_id == user_id,
                FileSearchClarification.status == "WAITING_SELECTION",
            )
            .order_by(FileSearchClarification.created_at.desc())
            .first()
        )
        if record is None:
            return None
        normalized = "".join(str(message or "").strip().lower().split())
        option_id: str | None = None
        if any(value in normalized for value in ("同义", "相近表达", "近义")):
            option_id = "synonyms"
        elif any(value in normalized for value in ("只查原文", "精确短语", "完整短语", "原短语")):
            option_id = "exact"
        else:
            for item in record.options_json:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("id") or "")
                if not candidate_id.startswith("broad-"):
                    continue
                phrases = [str(value) for value in item.get("phrases", []) if str(value)]
                if (
                    "只查" in normalized
                    and any("".join(value.lower().split()) in normalized for value in phrases)
                ):
                    option_id = candidate_id
                    break
        if option_id is None:
            return None
        return self.resolve(
            clarification_id=record.id,
            user_id=user_id,
            option_id=option_id,
        )

    @staticmethod
    def public_payload(record: FileSearchClarification) -> dict[str, Any]:
        """投影普通用户选择卡，移除执行参数和内部模式字段。"""

        options = []
        for item in record.options_json:
            if not isinstance(item, dict):
                continue
            options.append(
                {
                    "id": str(item.get("id") or ""),
                    "label": str(item.get("label") or ""),
                    "description": str(item.get("description") or ""),
                    "examples": [
                        str(value)
                        for value in item.get("examples", [])
                        if str(value)
                    ][:8],
                    "estimated_count": item.get("estimated_count"),
                }
            )
        return {
            "id": record.id,
            "status": record.status,
            "prompt": (
                "请选择要重命名的具体文件。"
                if record.relation_mode == "RENAME_DOCUMENT_SELECTION"
                else "请选择这次需要查找的范围。"
            ),
            "core_phrase": record.core_phrase,
            "options": options,
            "allow_custom_phrase": record.relation_mode not in {
                "DOCUMENT_SELECTION",
                "RENAME_DOCUMENT_SELECTION",
            },
            "selection_type": (
                "DOCUMENT_SELECTION"
                if record.relation_mode
                in {"DOCUMENT_SELECTION", "RENAME_DOCUMENT_SELECTION"}
                else "SEARCH_PHRASE"
            ),
            "expires_at": record.expires_at.isoformat(),
        }


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    """兼容 SQLite 返回朴素时间与 PostgreSQL 时区时间的过期比较。"""

    normalized_expires = expires_at
    normalized_now = now
    if normalized_expires.tzinfo is None:
        normalized_expires = normalized_expires.replace(tzinfo=timezone.utc)
    if normalized_now.tzinfo is None:
        normalized_now = normalized_now.replace(tzinfo=timezone.utc)
    return normalized_expires <= normalized_now
