"""知识图谱健康检查。"""

from __future__ import annotations

from typing import Any

from app.modules.knowledge_graph.classification_context import build_graph_classification_context
from app.modules.knowledge_graph.graphrag_adapter import graphrag_capability_status


def graph_health(settings: Any) -> dict[str, str]:
    """返回图谱开关、配置或连接状态。"""

    result = build_graph_classification_context(settings).health_check()
    return {**result, "graphrag_package": graphrag_capability_status()}
