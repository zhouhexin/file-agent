"""LLM 意图理解服务。"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.llm.prompts import USER_INTENT_SYSTEM_PROMPT
from app.modules.llm.schemas import UserIntentPlan


class LLMIntentService:
    """把用户消息和文件上下文交给 LLM，返回结构化用户意图。"""

    def __init__(self, settings: Settings | None = None, client: OpenAICompatibleLLMClient | None = None) -> None:
        """允许测试注入假客户端，避免单元测试访问真实模型。"""

        self.settings = settings or get_settings()
        self.enabled = self.settings.llm_enabled
        self.client = client

    def understand_user_request(
        self,
        *,
        message: str,
        attachments: List[Dict[str, Any]],
        context_documents: List[Dict[str, Any]],
    ) -> UserIntentPlan:
        """调用 LLM 解析用户需求。"""

        client = self.client or OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )
        payload = {
            "message": message,
            "attachments": attachments,
            "context_documents": context_documents,
            "output_schema": UserIntentPlan.model_json_schema(),
        }
        parsed = client.complete_json(system_prompt=USER_INTENT_SYSTEM_PROMPT, user_payload=payload)
        try:
            return UserIntentPlan.model_validate(parsed)
        except ValidationError as exc:
            # LLM 可能返回合法 JSON 但不符合受控 schema；必须转成可兜底错误，不能让 Pydantic 异常穿透到 API。
            raise LLMResponseError(f"LLM 意图响应不符合 schema：{exc}") from exc
