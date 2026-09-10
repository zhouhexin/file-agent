"""WorkBuddy 对话式文件能力的受控参数构造与后端转接。"""

from __future__ import annotations

import hashlib
from pathlib import PurePath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .client import FileAgentIntegrationClient


class ExplicitRenameInput(BaseModel):
    """WorkBuddy 明确重命名的严格单项输入。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=100)
    source_filename: str = Field(min_length=1, max_length=255)
    target_filename: str = Field(min_length=1, max_length=255)


class ExplicitClassificationPlacementInput(BaseModel):
    """WorkBuddy 分类主类更正/移动的受限结构化命令，不接受物理路径或权限字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    working_copy_id: UUID
    action: Literal["SET_PRIMARY", "MOVE"]
    expected_revision: int = Field(gt=0)
    expected_document_version_id: UUID
    target_category_id: str | None = Field(default=None, min_length=1, max_length=255)
    taxonomy_version: str = Field(min_length=1, max_length=80)
    container_segments: list[str] = Field(default_factory=list, max_length=20)
    target_root_key: str | None = Field(default=None, min_length=1, max_length=100)
    target_directory_segments: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("container_segments", "target_directory_segments")
    @classmethod
    def validate_segments(cls, values: list[str]) -> list[str]:
        """目录目标仅由安全逻辑段构成，不能携带绝对路径或跨目录标记。"""

        normalized: list[str] = []
        for value in values:
            segment = str(value or "").strip()
            if (
                not segment
                or segment in {".", ".."}
                or segment != value
                or segment.startswith(".")
                or "/" in segment
                or "\\" in segment
                or "\x00" in segment
            ):
                raise ValueError("目录段必须是单个安全名称")
            normalized.append(segment)
        return normalized

    @model_validator(mode="after")
    def validate_target_mode(self) -> "ExplicitClassificationPlacementInput":
        """SET_PRIMARY 固定分类目标；MOVE 只能在分类或受控目录两种目标中二选一。"""

        category_mode = self.target_category_id is not None
        directory_mode = self.target_root_key is not None
        if self.action == "SET_PRIMARY":
            if not category_mode or directory_mode or self.target_directory_segments:
                raise ValueError("SET_PRIMARY 只能提供 target_category_id")
            return self
        if category_mode == directory_mode:
            raise ValueError("MOVE 必须且只能提供分类目标或受控目录目标之一")
        if directory_mode and not self.target_directory_segments:
            raise ValueError("目录形式 MOVE 必须提供完整 target_directory_segments")
        return self

    def command_payload(self) -> dict[str, Any]:
        """序列化为后端 PlacementCommand 所需的 JSON，不增加任何宿主本地信息。"""

        return self.model_dump(mode="json")


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

    async def submit_classification_placement(
        self,
        *,
        command: ExplicitClassificationPlacementInput | dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """向后端提交用户明确的分类落位，不经聊天文本或任意本机文件路径。"""

        parsed = (
            command
            if isinstance(command, ExplicitClassificationPlacementInput)
            else ExplicitClassificationPlacementInput.model_validate(command)
        )
        return await self.client.classification_placement_submit(
            command=parsed.command_payload(),
            request_id=_validate_request_id(request_id),
        )

    async def get_classification_placement_status(self, *, operation_id: str) -> dict[str, Any]:
        """读取一次已提交分类落位的真实后端状态，不触发文件移动或分类写入。"""

        normalized = str(operation_id or "").strip()
        if not normalized or len(normalized) > 100 or "/" in normalized or "\\" in normalized:
            raise ValueError("operation_id 格式不合法")
        return await self.client.classification_placement_status(operation_id=normalized)


def _validate_text(value: str, *, label: str, max_length: int) -> str:
    """限制对话文本长度并拒绝控制字符，正文仍只被当作用户数据处理。"""

    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} 不能为空且长度不能超过 {max_length}")
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        raise ValueError(f"{label} 不能包含控制字符")
    return normalized


def _validate_request_id(value: str) -> str:
    """约束 MCP 请求追踪标识，避免把多行文本或路径写入后端授权审计。"""

    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 120 or any(ord(item) < 32 for item in normalized):
        raise ValueError("request_id 必须是 1-120 位的非控制字符标识")
    return normalized
