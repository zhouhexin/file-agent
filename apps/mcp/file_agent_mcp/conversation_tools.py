"""WorkBuddy 对话式文件能力的受控参数构造与后端转接。"""

from __future__ import annotations

import hashlib
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .client import FileAgentIntegrationClient


class ExplicitRenameInput(BaseModel):
    """WorkBuddy 明确重命名的严格单项输入。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=100)
    source_filename: str = Field(min_length=1, max_length=255)
    target_filename: str = Field(min_length=1, max_length=255)


def conversation_id_for_workbuddy(conversation_ref: str) -> str:
    """把外部会话引用映射成稳定内部 ID，不暴露或信任宿主原始主键格式。"""

    normalized = conversation_ref.strip()
    if not normalized or len(normalized) > 500 or "\x00" in normalized:
        raise ValueError("conversation_ref 必须是非空的 WorkBuddy 会话稳定引用")
    return f"wb-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]}"


def validate_document_ids(document_ids: list[str], *, required: bool) -> list[str]:
    """限制文件范围为去重后的稳定 ID；最终所有权仍由后端校验。"""

    values = list(dict.fromkeys(str(value).strip() for value in document_ids if str(value).strip()))
    if required and not values:
        raise ValueError("必须提供 file_search 返回的 document_id")
    if len(values) > 50 or any(len(value) > 100 or "\x00" in value for value in values):
        raise ValueError("document_ids 超过数量或长度限制")
    return values


def validate_filename(filename: str, *, label: str) -> str:
    """校验明确重命名的单个 basename，目录移动必须使用独立文件动作。"""

    normalized = filename.strip()
    if (
        not normalized
        or len(normalized) > 255
        or normalized in {".", ".."}
        or PurePath(normalized).name != normalized
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise ValueError(f"{label} 必须是单个安全文件名")
    return normalized


class WorkBuddyConversationService:
    """复用 File Agent 聊天主入口执行搜索、读取、问答和明确重命名。"""

    def __init__(self, client: FileAgentIntegrationClient) -> None:
        """注入只持有用户访问令牌的后端客户端。"""

        self.client = client

    async def search(self, *, conversation_ref: str, query: str) -> dict[str, Any]:
        """执行只读文件搜索；查询文字没有机会路由到文件写操作。"""

        return await self.client.file_search(
            conversation_id=conversation_id_for_workbuddy(conversation_ref),
            query=_validate_text(query, label="query", max_length=500),
        )

    async def read(
        self,
        *,
        conversation_ref: str,
        document_ids: list[str],
        read_mode: str,
    ) -> dict[str, Any]:
        """读取用户已选定文件；正文只由后端受控 Tool 和完整 DocumentPage 消费。"""

        prompts = {
            "READ": "读取这些文件",
            "SUMMARY": "总结这些文件",
            "EXPLAIN": "讲解这些文件的主要内容",
        }
        if read_mode not in prompts:
            raise ValueError("read_mode 只能是 READ、SUMMARY 或 EXPLAIN")
        return await self.client.conversation_task(
            conversation_id=conversation_id_for_workbuddy(conversation_ref),
            content=prompts[read_mode],
            document_ids=validate_document_ids(document_ids, required=True),
        )

    async def answer(
        self,
        *,
        conversation_ref: str,
        question: str,
        document_ids: list[str],
    ) -> dict[str, Any]:
        """执行带后端引用校验的证据回答，不接受 WorkBuddy 自行拼装 Evidence。"""

        return await self.client.evidence_answer(
            conversation_id=conversation_id_for_workbuddy(conversation_ref),
            question=_validate_text(question, label="question", max_length=4000),
            document_ids=validate_document_ids(document_ids, required=False),
        )

    async def rename(
        self,
        *,
        conversation_ref: str,
        renames: list[ExplicitRenameInput | dict[str, Any]],
    ) -> dict[str, Any]:
        """把明确映射交给现有重命名 Agent 链路，保留 OperationPlan 和执行审计。"""

        if not renames or len(renames) > 50:
            raise ValueError("renames 必须包含 1 到 50 个明确文件映射")
        document_ids: list[str] = []
        commands: list[str] = []
        for raw in renames:
            try:
                item = raw if isinstance(raw, ExplicitRenameInput) else ExplicitRenameInput.model_validate(raw)
            except ValueError as exc:
                raise ValueError(
                    "每个重命名项必须且只能包含 document_id、source_filename、target_filename"
                ) from exc
            document_id = validate_document_ids([item.document_id], required=True)[0]
            source = validate_filename(item.source_filename, label="source_filename")
            target = validate_filename(item.target_filename, label="target_filename")
            if source == target:
                raise ValueError("目标文件名不能与当前文件名相同")
            document_ids.append(document_id)
            commands.append(f"把“{source}”重命名为“{target}”")
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("同一请求不能重复重命名同一个 document_id")
        return await self.client.conversation_task(
            conversation_id=conversation_id_for_workbuddy(conversation_ref),
            content="\n".join(commands),
            document_ids=document_ids,
        )


def _validate_text(value: str, *, label: str, max_length: int) -> str:
    """限制对话文本长度并拒绝控制字符，正文仍只被当作用户数据处理。"""

    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} 不能为空且长度不能超过 {max_length}")
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        raise ValueError(f"{label} 不能包含控制字符")
    return normalized
