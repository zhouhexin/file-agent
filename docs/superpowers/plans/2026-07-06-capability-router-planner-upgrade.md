# Capability Router Planner Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前主要依赖关键词匹配的工具入口，升级为“附件范围确定性解析 + LLM Planner 结构化意图 + Capability Catalog 能力路由 + ToolDispatcher schema 校验”的完整一阶段架构。

**Architecture:** 附件范围继续由 `ConversationAttachmentContextService` 确定，LLM 只能输出 `target_scope`、`intent`、`required_capabilities` 和 `tool_plan_hint`，不得猜测 `document_id`。`CapabilityRouter` 根据 intent、scope、文件类型和能力目录召回候选 Tool；`planner.py` 把路由转换为受控 `PlannerOutput`，再由 ToolDispatcher 校验白名单和 schema 后执行。

**Tech Stack:** Python 3.11、FastAPI、LangGraph、Pydantic、pytest、现有 `apps/api/app/modules/agent` 模块。

---

## File Structure

- Modify: `apps/api/app/modules/agent/capabilities/catalog.json`
  - 增加每个能力的 `intents`、`tool_names`、`capability_keys` 元数据，为后续能力路由提供 source of truth。
- Modify: `apps/api/app/modules/agent/capabilities/service.py`
  - 扩展 Pydantic schema，兼容旧字段，同时读取新增路由元数据。
- Create: `apps/api/app/modules/agent/capability_router.py`
  - 新增轻量能力路由器，把 intent/capability/tool hint 标准化为 `CapabilityRoute`。
- Modify: `apps/api/app/modules/agent/planner.py`
  - 在 `build_plan_from_user_intent()` 入口优先使用 `CapabilityRouter` 结果，旧关键词判断保留为兜底。
- Modify: `apps/api/app/tests/test_agent_runtime.py`
  - 增加路由优先级测试：分类总结、正文总结、表格汇总、能力帮助和分类目录必须映射到正确 Tool。

## One-Phase Scope

本阶段一次完成后端路由链路，不改前端，不删除旧关键词兜底逻辑，但新增路径必须成为 LLM Planner 的主入口。

本阶段完成后应满足：

- LLM 返回 `required_capabilities=["read_document_classifications"]` 时，默认路由到 `read-document-classifications`。
- 用户消息明确是“总结文件内容”或“汇总表格金额/列”时，确定性纠偏仍优先，避免误走分类读取。
- 用户问“你可以做什么”或 LLM 返回 `read_agent_capabilities` 时，路由到 `read-agent-capabilities`。
- 用户问“系统支持哪些分类目录”或 LLM 返回 `read_classification_taxonomy` 时，路由到 `read-classification-taxonomy`。
- 当前无法安全判定的请求继续走现有 Planner 兜底，不直接失败。
- `ConversationAttachmentContextService` 必须覆盖“上传的所有文件 / 所有上传的文件 / 全部上传的文件 / 已上传的所有文件”并解析为 `all_conversation`。
- `ConversationAttachmentContextService` 必须覆盖“上一个文件 / 上个附件”并解析为最近上下文附件中的第一个文件。
- `UserIntentPlan` 必须增加 `target_scope`，只表达用户自然语言中的范围意图，例如 `latest_upload_batch`、`all_conversation`、`ordinal_reference`、`filename_reference`、`current_message`、`unspecified`。
- `ConversationMessageService` 必须把后端解析出的 `scope` 写入 Agent Runtime 的附件 dict，Planner 只能消费后端解析后的附件边界。
- `CapabilityRouter` 必须支持文件类型信息，`.xlsx/.xlsm/.csv` 的统计汇总优先召回 `analyze-spreadsheet`。

## Task 1: Capability Catalog Metadata

**Files:**
- Modify: `apps/api/app/modules/agent/capabilities/catalog.json`
- Modify: `apps/api/app/modules/agent/capabilities/service.py`
- Test: `apps/api/app/tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing test**

Add a test that loads the fixed capability catalog and asserts each routed capability exposes at least one tool name:

```python
from app.modules.agent.capabilities.service import load_agent_capabilities


def test_capability_catalog_exposes_router_metadata():
    """能力目录必须暴露路由元数据，供 Planner 从能力选择 Tool。"""

    catalog = load_agent_capabilities(detail_level="full")
    routed = {
        capability["id"]: capability
        for capability in catalog["capabilities"]
        if capability["id"] in {"document_summary", "document_classification", "spreadsheet_analysis"}
    }

    assert routed["document_summary"]["tool_names"] == ["extract-document-text"]
    assert "read-document-classifications" in routed["document_classification"]["tool_names"]
    assert routed["spreadsheet_analysis"]["tool_names"] == ["analyze-spreadsheet"]
```

- [ ] **Step 2: Run failing test**

Run:

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_agent_runtime.py::test_capability_catalog_exposes_router_metadata
```

Expected: fails because catalog schema does not expose `tool_names`.

- [ ] **Step 3: Implement catalog metadata**

Update each capability item to include:

```json
{
  "intents": ["SUMMARIZE_DOCUMENTS"],
  "capability_keys": ["extract_document_text", "extract-document-text"],
  "tool_names": ["extract-document-text"]
}
```

Extend `AgentCapability` with default list fields:

```python
intents: list[str] = Field(default_factory=list)
capability_keys: list[str] = Field(default_factory=list)
tool_names: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run test**

Expected: test passes.

## Task 2: Capability Router

**Files:**
- Create: `apps/api/app/modules/agent/capability_router.py`
- Test: `apps/api/app/tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing tests**

Add tests:

```python
from app.modules.agent.capability_router import route_user_intent


def test_capability_router_maps_classification_summary_to_read_tool():
    """读取已有分类建议必须路由到分类读取 Tool。"""

    route = route_user_intent(
        intent="SUMMARIZE_CLASSIFICATIONS",
        required_capabilities=["read_document_classifications"],
        tool_plan_hint=["read-document-classifications"],
    )

    assert route.intent == "SUMMARIZE_CLASSIFICATIONS"
    assert route.tool_name == "read-document-classifications"


def test_capability_router_maps_spreadsheet_analysis_to_table_tool():
    """表格汇总能力必须路由到表格分析 Tool。"""

    route = route_user_intent(
        intent="ANALYZE_SPREADSHEET",
        required_capabilities=["analyze_spreadsheet"],
        tool_plan_hint=[],
    )

    assert route.intent == "ANALYZE_SPREADSHEET"
    assert route.tool_name == "analyze-spreadsheet"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_agent_runtime.py::test_capability_router_maps_classification_summary_to_read_tool app/tests/test_agent_runtime.py::test_capability_router_maps_spreadsheet_analysis_to_table_tool
```

Expected: import fails because `capability_router.py` does not exist.

- [ ] **Step 3: Implement router**

Create `CapabilityRoute` and `route_user_intent()`:

```python
class CapabilityRoute(BaseModel):
    """Planner 能力路由结果。"""

    intent: str
    tool_name: str
    selected_skill: str


def route_user_intent(*, intent: str, required_capabilities: list[str], tool_plan_hint: list[str]) -> CapabilityRoute | None:
    """根据标准 intent 和能力 hint 返回受控 Tool 路由。"""
```

- [ ] **Step 4: Run tests**

Expected: router tests pass.

## Task 3: Planner Integration

**Files:**
- Modify: `apps/api/app/modules/agent/planner.py`
- Test: `apps/api/app/tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing test**

Add a test that verifies LLM capability hints are routed through `CapabilityRouter` without requiring every branch to hand-write keywords:

```python
def test_llm_capability_route_prefers_router_for_classification_summary():
    """LLM 能力 hint 应先经过 CapabilityRouter，再生成受控 ToolPlan。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="帮我总结刚刚上传文件的分类",
        needs_file_context=True,
        referenced_document_ids=["doc-1"],
        required_capabilities=["read_document_classifications"],
        tool_plan_hint=["read-document-classifications"],
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="帮我总结刚刚上传文件的分类",
        attachments=[{"document_id": "doc-1"}],
    )

    assert plan.intent == "SUMMARIZE_CLASSIFICATIONS"
    assert [step.tool_name for step in plan.steps] == ["read-document-classifications"]
    assert plan.slots["route_source"] == "capability_router"
```

- [ ] **Step 2: Run failing test**

Expected: fails because `route_source` is not written.

- [ ] **Step 3: Integrate router**

At the top of `build_plan_from_user_intent()`, call `route_user_intent()`. For safe routes, construct existing PlannerOutput forms and include:

```python
"route_source": "capability_router"
```

Keep existing correction branches before unsafe routes when message semantics are stronger, especially:

- spreadsheet analysis beats classification hint;
- plain document summary beats classification hint;
- explicit re-classification beats classification read.

- [ ] **Step 4: Run focused tests**

Run:

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_agent_runtime.py::test_llm_capability_route_prefers_router_for_classification_summary app/tests/test_agent_runtime.py::test_llm_classification_hint_is_overridden_for_plain_file_summary app/tests/test_agent_runtime.py::test_llm_classification_hint_is_overridden_for_table_column_summary app/tests/test_agent_runtime.py::test_llm_classification_hint_is_overridden_for_explicit_file_classification
```

Expected: all pass.

## Task 4: Guardrail Tests

**Files:**
- Test: `apps/api/app/tests/test_agent_runtime.py`

- [ ] **Step 1: Add regression tests**

Add tests for:

- capability help -> `read-agent-capabilities`
- classification taxonomy -> `read-classification-taxonomy`
- unknown intent with no file context -> `intent-summary`

- [ ] **Step 2: Run focused tests**

Run:

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_agent_runtime.py
```

Expected: planner and runtime tests pass.

## Self-Review

- Spec coverage: 本计划覆盖能力目录、路由器、Planner 接入和关键回归测试。
- Placeholder scan: 没有使用 TBD/TODO；后续迁移旧关键词分支不在本阶段范围内。
- Type consistency: `CapabilityRoute.intent/tool_name/selected_skill`、`route_source` 和现有 `PlannerOutput.slots` 一致。
