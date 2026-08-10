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
    user_id: str
    option_id: str
    display_content: str
    original_query: str
    match_mode: str
    phrases: tuple[str, ...]
    require_body_evidence: bool
    show_all_results: bool = False
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
        option_id: str | None = None,
        option_ids: list[str] | None = None,
        custom_phrase: str | None = None,
    ) -> ResolvedSearchSelection:
        """校验单选或文件多选并返回执行参数；相同选择重复提交保持幂等。"""

        record = self.db.get(FileSearchClarification, clarification_id)
        if record is None or record.user_id != user_id:
            raise FileSearchClarificationError("检索选择不存在")
        requested_option_ids = _normalize_requested_option_ids(
            record=record,
            option_id=option_id,
            option_ids=option_ids,
        )
        primary_option_id = requested_option_ids[0]
        now = utcnow()
        if _is_expired(record.expires_at, now) and record.status == "WAITING_SELECTION":
            record.status = "EXPIRED"
            record.resolved_at = now
            self.db.flush()
            raise FileSearchClarificationError("检索选择已过期，请重新发起查找")
        if record.status == "RESOLVED":
            saved = record.resolution_json if isinstance(record.resolution_json, dict) else {}
            saved_option_ids = tuple(
                str(value) for value in saved.get("option_ids", []) if str(value)
            ) or ((str(record.selected_option_id),) if record.selected_option_id else ())
            if saved_option_ids != requested_option_ids:
                raise FileSearchClarificationError("该检索选择已经处理")
            saved_phrases = tuple(
                str(value) for value in saved.get("phrases", []) if str(value)
            )
            saved_document_ids = tuple(
                str(value) for value in saved.get("document_ids", []) if str(value)
            )
            saved_show_all_results = bool(saved.get("show_all_results", False))
            if (
                not saved_phrases
                and not saved_document_ids
                and not saved_show_all_results
            ):
                raise FileSearchClarificationError("已处理选择缺少执行记录")
            if primary_option_id == "custom" and custom_phrase:
                requested = validate_custom_search_phrase(custom_phrase)
                if saved_phrases != (requested,):
                    raise FileSearchClarificationError("该检索选择已经使用其他自定义短语处理")
            return ResolvedSearchSelection(
                clarification_id=record.id,
                conversation_id=record.conversation_id,
                user_id=record.user_id,
                option_id=primary_option_id,
                display_content=str(saved.get("display_content") or "继续查找"),
                original_query=record.original_query,
                match_mode=str(saved.get("match_mode") or "LITERAL"),
                phrases=saved_phrases,
                require_body_evidence=bool(saved.get("require_body_evidence", False)),
                show_all_results=saved_show_all_results,
                document_ids=saved_document_ids,
                result_message_id=record.result_message_id,
                result_agent_run_id=record.result_agent_run_id,
            )
        elif record.status != "WAITING_SELECTION":
            raise FileSearchClarificationError("该检索选择已失效")

        options_by_id = {
            str(item.get("id")): item
            for item in record.options_json
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        selected_options = [
            options_by_id[value] for value in requested_option_ids if value in options_by_id
        ]
        if len(selected_options) != len(requested_option_ids):
            raise FileSearchClarificationError("选择项不属于当前检索")
        option = selected_options[0]

        show_all_results = False
        if record.relation_mode == "RESULT_LIMIT_CONFIRMATION":
            if primary_option_id != "show-all-results":
                raise FileSearchClarificationError("结果数量确认选项无效")
            phrases = ()
            document_ids = ()
            match_mode = "AUTO"
            require_body = False
            show_all_results = True
            estimated_count = int(option.get("estimated_count") or 0)
            display_content = (
                f"全部展示这次找到的 {estimated_count} 个文件"
                if estimated_count
                else "全部展示这次找到的文件"
            )
        elif record.relation_mode in {
            "DOCUMENT_SELECTION",
            "RENAME_DOCUMENT_SELECTION",
        }:
            if (
                record.relation_mode == "RENAME_DOCUMENT_SELECTION"
                and len(selected_options) != 1
            ):
                raise FileSearchClarificationError("重命名时只能选择一个具体文件")
            document_ids = tuple(
                str(item.get("document_id") or "") for item in selected_options
            )
            if not all(document_ids):
                raise FileSearchClarificationError("文件选择项缺少有效文件")
            phrases = ()
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
                labels = [
                    str(item.get("label") or "所选文件") for item in selected_options
                ]
                display_content = f"使用所选 {len(labels)} 份文件继续原任务"
        elif primary_option_id == "custom":
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
        record.selected_option_id = primary_option_id
        record.resolution_json = {
            "display_content": display_content,
            "match_mode": match_mode,
            "phrases": list(phrases),
            "require_body_evidence": require_body,
            "show_all_results": show_all_results,
            "document_ids": list(document_ids),
            "option_ids": list(requested_option_ids),
        }
        record.resolved_at = now
        self.db.flush()
        return ResolvedSearchSelection(
            clarification_id=record.id,
            conversation_id=record.conversation_id,
            user_id=record.user_id,
            option_id=primary_option_id,
            display_content=display_content,
            original_query=record.original_query,
            match_mode=match_mode,
            phrases=phrases,
            require_body_evidence=require_body,
            show_all_results=show_all_results,
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

    def validate_resolved_document_selection(
        self,
        *,
        clarification_id: str,
        user_id: str,
        conversation_id: str,
        document_ids: list[str],
    ) -> None:
        """验证证据回答范围确实来自当前用户已解决的文件选择。

        该校验是续跑范围的信任边界：Tool 输入里出现选择记录 ID 并不代表可信，
        必须同时匹配用户、会话、选择类型、状态和完整文件集合。这样既能避免续跑
        再次扩张到同名文件，也不能被 LLM 或客户端伪造字段绕过确认。
        """

        record = self.db.get(FileSearchClarification, clarification_id)
        if (
            record is None
            or record.user_id != user_id
            or record.conversation_id != conversation_id
            or record.relation_mode != "DOCUMENT_SELECTION"
            or record.status != "RESOLVED"
        ):
            raise FileSearchClarificationError("已确认文件范围不存在或尚未解决")
        resolution = (
            record.resolution_json
            if isinstance(record.resolution_json, dict)
            else {}
        )
        saved_document_ids = [
            str(value)
            for value in resolution.get("document_ids", [])
            if str(value)
        ]
        requested_document_ids = list(
            dict.fromkeys(str(value) for value in document_ids if str(value))
        )
        if (
            not saved_document_ids
            or len(saved_document_ids) != len(requested_document_ids)
            or set(saved_document_ids) != set(requested_document_ids)
        ):
            raise FileSearchClarificationError("已确认文件范围与本次 Tool 输入不一致")

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
        if (
            record.relation_mode == "RESULT_LIMIT_CONFIRMATION"
            and normalized
            in {
                "是",
                "是的",
                "好的",
                "可以",
                "确认",
                "全部",
                "全部展示",
                "全部显示",
                "都展示",
                "都显示",
                "都要",
                "全都展示",
                "展示全部",
                "显示全部",
                "查看全部",
                "全部列出",
            }
        ):
            option_id = "show-all-results"
        elif any(value in normalized for value in ("同义", "相近表达", "近义")):
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
                else "请选择一份或多份文件，然后继续原任务。"
                if record.relation_mode == "DOCUMENT_SELECTION"
                else _result_limit_prompt(record)
                if record.relation_mode == "RESULT_LIMIT_CONFIRMATION"
                else "请选择这次需要查找的范围。"
            ),
            "core_phrase": record.core_phrase,
            "options": options,
            "allow_custom_phrase": record.relation_mode not in {
                "DOCUMENT_SELECTION",
                "RENAME_DOCUMENT_SELECTION",
                "RESULT_LIMIT_CONFIRMATION",
            },
            "selection_type": (
                "DOCUMENT_SELECTION"
                if record.relation_mode
                in {"DOCUMENT_SELECTION", "RENAME_DOCUMENT_SELECTION"}
                else "RESULT_LIMIT_CONFIRMATION"
                if record.relation_mode == "RESULT_LIMIT_CONFIRMATION"
                else "SEARCH_PHRASE"
            ),
            "allow_multiple": record.relation_mode == "DOCUMENT_SELECTION",
            "expires_at": record.expires_at.isoformat(),
        }


def _result_limit_prompt(record: FileSearchClarification) -> str:
    """生成只含用户可见数量的结果确认提示。"""

    count = next(
        (
            int(item.get("estimated_count") or 0)
            for item in record.options_json
            if isinstance(item, dict)
            and item.get("id") == "show-all-results"
        ),
        0,
    )
    return (
        f"找到 {count} 个相关文件，查询结果较多，是否全部展示？"
        if count
        else "查询结果较多，是否全部展示？"
    )


def _normalize_requested_option_ids(
    *,
    record: FileSearchClarification,
    option_id: str | None,
    option_ids: list[str] | None,
) -> tuple[str, ...]:
    """按服务端选项顺序规范化选择，并只允许文件选择卡提交多项。"""

    requested = {
        str(value).strip()
        for value in (option_ids or ([option_id] if option_id else []))
        if str(value).strip()
    }
    if not requested:
        raise FileSearchClarificationError("请至少选择一个选项")
    ordered = tuple(
        str(item.get("id"))
        for item in record.options_json
        if isinstance(item, dict) and str(item.get("id") or "") in requested
    )
    if len(ordered) != len(requested):
        raise FileSearchClarificationError("选择项不属于当前检索")
    if len(ordered) > 1 and record.relation_mode != "DOCUMENT_SELECTION":
        raise FileSearchClarificationError("当前选择卡只能选择一个选项")
    return ordered


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    """兼容 SQLite 返回朴素时间与 PostgreSQL 时区时间的过期比较。"""

    normalized_expires = expires_at
    normalized_now = now
    if normalized_expires.tzinfo is None:
        normalized_expires = normalized_expires.replace(tzinfo=timezone.utc)
    if normalized_now.tzinfo is None:
        normalized_now = normalized_now.replace(tzinfo=timezone.utc)
    return normalized_expires <= normalized_now
