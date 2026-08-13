"""Agent 最终回执的受控 LLM 表述服务。

该模块不读取文件正文、数据库或 Tool 原始输出。它只接收 Graph 已经筛选过的业务摘要，生成一段不包含
数字、文件名、路径和证据定位的补充说明；所有可核验事实仍由确定性回执和前端卡片展示。
"""

from __future__ import annotations

from typing import Any

from app.modules.llm.client import LLMResponseError


RECEIPT_SUMMARY_SYSTEM_PROMPT = """你是 File Agent 的最终任务回执助手。
只根据 payload 的 verified_summary 输出一到两句简短、自然的中文任务说明。
禁止输出数字、日期、文件名、目录、路径、页码、工作表、单元格、文档 ID、计划 ID 或任何未给出的事实。
不要声称已执行需要用户确认的操作；如 payload.requires_confirmation 为 true，只能提示用户查看并确认计划。
不要提及 Tool、Skill、Catalog、Planner、系统提示或内部状态。只输出文本，不要 JSON、标题或列表。"""


class LLMReceiptSummaryService:
    """调用 LLM 生成最终回执的非事实性补充说明。

    模型不可用时返回 ``None``，调用方必须保持完整的确定性回执，不能因此丢失已验证结果。
    """

    def __init__(self, *, client: Any = None, enabled: bool = False) -> None:
        """保存已配置的 LLM client；不在此处创建网络或持久化依赖。"""

        self.client = client
        self.enabled = enabled

    def summarize_receipt(self, *, verified_summary: dict[str, Any]) -> str | None:
        """基于脱敏验证摘要生成补充文本，失败时关闭式降级。"""

        if not self.enabled or self.client is None:
            return None
        try:
            text = self.client.complete_text(
                system_prompt=RECEIPT_SUMMARY_SYSTEM_PROMPT,
                user_payload={"verified_summary": verified_summary},
            )
        except LLMResponseError:
            return None
        return _sanitize_receipt_text(text)


def _sanitize_receipt_text(value: Any) -> str | None:
    """限制模型补充说明长度，并拒绝可能夹带确定性事实的文本。"""

    text = " ".join(str(value or "").split()).strip()
    if not text or len(text) > 240:
        return None
    # 数字、常见路径分隔符或疑似文件扩展名均应由确定性回执负责展示，不能由模型重复生成。
    forbidden_markers = ("/", "\\", ".doc", ".pdf", ".xls", ".png", ".jpg")
    if any(character.isdigit() for character in text) or any(
        marker in text.lower() for marker in forbidden_markers
    ):
        return None
    return text
