"""生成有限源码上下文及真实差异，AST白名单禁止候选引入任意执行行为。"""

import ast
import difflib
import hashlib
import re
from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement.schemas import PatchProposal, LegacyPatchProposal

TARGET = "app/modules/agent/planner.py"
FUNCTION = "build_plan_from_user_intent"


def source_hash(source: str) -> str:
    """源码使用字节散列，与输入JSON散列分开标识。"""
    return hashlib.sha256(source.encode()).hexdigest()


def target_node(source: str):
    """精确定位唯一前置搜索判断，源码结构变化时拒绝套用补丁。"""
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == FUNCTION]
    if len(functions) != 1:
        raise ValueError("PATCH_FUNCTION_NOT_UNIQUE")
    nodes = [n for n in functions[0].body if isinstance(n, ast.If)
             and isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And)
             and "_has_file_search_intent" in ast.unparse(n.test.values[0])]
    if len(nodes) != 1:
        raise ValueError("PATCH_CONDITION_NOT_UNIQUE")
    return tree, functions[0], nodes[0]


def source_context(source: str) -> dict:
    """只提供本函数和相关枚举，模型无需读取整个仓库。"""
    tree, function, node = target_node(source)
    constants = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in {"EVIDENCE_ANSWER_INTENTS", "EVIDENCE_ANSWER_HINTS"}
                         for t in n.targets)]
    return {"path": TARGET, "function": FUNCTION, "baseline_sha256": source_hash(source),
            "original_condition": ast.unparse(node.test), "function_source": ast.get_source_segment(source, function),
            "constants": [ast.unparse(n) for n in constants],
            "guard_rules": {"names": ["intent_plan", "EVIDENCE_ANSWER_INTENTS", "EVIDENCE_ANSWER_HINTS",
                                        "attachments", "requested_capabilities"],
                            "attributes": ["intent_plan.intent", "intent_plan.required_capabilities", "intent_plan.tool_plan_hint"],
                            "operations": "and/or/not、in/not in、==/!=、集合/元组；禁止调用、下标和任意样本字符串"},
            "allowed_change": "只给原判断增加and保护条件；不能删除原条件，不能新增调用、权限或样本特例"}


def validate_guard(guard: str):
    """允许组合现有语义变量，不允许调用、下标、动态属性或任意文字特例。"""
    expression = ast.parse(guard, mode="eval")
    allowed_types = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
                     ast.In, ast.NotIn, ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Attribute, ast.Set,
                     ast.Tuple, ast.Constant)
    names = {"intent_plan", "EVIDENCE_ANSWER_INTENTS", "EVIDENCE_ANSWER_HINTS", "attachments",
             "requested_capabilities"}
    strings = {"ANSWER_DOCUMENTS", "ANSWER_FROM_FILES", "ANSWER_MANAGED_FILE", "ANSWER_QUESTION_FROM_FILES",
               "EVIDENCE_ANSWER", "SEARCH_FILES", "LIST_MANAGED_FILES", "SUMMARIZE_FILES",
               "evidence-answer", "evidence_answer"}
    for node in ast.walk(expression):
        if not isinstance(node, allowed_types):
            raise ValueError("PATCH_GUARD_EXECUTION_BLOCKED")
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError("PATCH_GUARD_NAME_BLOCKED")
        if isinstance(node, ast.Attribute) and not (isinstance(node.value, ast.Name)
            and node.value.id == "intent_plan" and node.attr in {"intent", "required_capabilities", "tool_plan_hint"}):
            raise ValueError("PATCH_GUARD_ATTRIBUTE_BLOCKED")
        if isinstance(node, ast.Constant) and not (type(node.value) is bool or node.value is None
                                                  or isinstance(node.value, str) and node.value in strings):
            raise ValueError("PATCH_SAMPLE_LITERAL_BLOCKED")
    if len(list(ast.walk(expression))) > 100:
        raise ValueError("PATCH_GUARD_TOO_LARGE")
    return expression.body


def rejection_code(exc):
    """只返回固定错误码，不把Pydantic的输入值、异常全文写进回执。"""
    value = str(exc)
    return value if re.fullmatch(r"[A-Z_]{1,90}", value) else "PATCH_SCHEMA_INVALID"


def validate_explanation(proposal, feedback, history):
    """检查明显自相矛盾的依据；不替模型选择补丁，也不强迫它修改。"""
    selected = [item for item in feedback["items"] if item["feedback_id"] in proposal.feedback_refs]
    latest = {}
    for item in feedback["items"]:
        latest[(item.get("case_ref"), item.get("arm"), item.get("repetition"))] = item
    if proposal.diagnosis == "NO_ACTIONABLE_FAILURE" and any(
        item.get("task_outcome") == "INCORRECT" for item in [*selected, *latest.values()]
    ):
        raise ValueError("NO_CHANGE_CONTRADICTS_TASK_FAILURE")
    if proposal.diagnosis == "NO_ACTIONABLE_FAILURE" and any(
        item.get("task_outcome") != "CORRECT" for item in latest.values()
    ):
        raise ValueError("NO_CHANGE_REQUIRES_TASK_VERDICT")
    if proposal.no_change_reason == "VERIFIED_NO_GAIN" and not any(
        attempt.get("patch_ref") == proposal.no_change_attempt_ref
        and attempt.get("change_kind") == "CODE_PATCH"
        and attempt.get("strategy_evaluable") is True
        and attempt.get("decision") in {"NO_GAIN", "REGRESSION"} for attempt in history
    ):
        raise ValueError("NO_CHANGE_VERIFIED_ATTEMPT_MISSING")


def materialize(source: str, raw: dict, feedback: dict, *, history=None) -> dict:
    """构造补丁但不写当前源码，原搜索判断保持为必需前提。"""
    schema = PatchProposal if feedback.get("schema_version") == "root-cause-feedback-v2" else LegacyPatchProposal
    if feedback.get("schema_version") not in {"root-cause-feedback-v1", "root-cause-feedback-v2"}:
        raise ValueError("PATCH_FEEDBACK_VERSION_INVALID")
    proposal = schema.model_validate(raw)
    refs = {item["feedback_id"] for item in feedback["items"]}
    if proposal.baseline_sha256 != source_hash(source) or not set(proposal.feedback_refs) <= refs:
        raise ValueError("PATCH_BASELINE_OR_FEEDBACK_MISMATCH")
    if schema is PatchProposal:
        validate_explanation(proposal, feedback, history or [])
    if proposal.decision == "NO_CHANGE":
        return {"stage": "NO_CHANGE", "proposal": proposal.model_dump(), "patch": "", "source": source}
    tree, _, node = target_node(source)
    guard = validate_guard(proposal.guard)
    original = ast.dump(node.test, include_attributes=False)
    replacement = ast.unparse(ast.BoolOp(op=ast.And(), values=[node.test, guard]))
    # AST列号是UTF-8字节偏移，中文注释不能用字符下标切割。
    lines = source.encode().splitlines(keepends=True)
    start = sum(map(len, lines[:node.test.lineno - 1])) + node.test.col_offset
    end = sum(map(len, lines[:node.test.end_lineno - 1])) + node.test.end_col_offset
    candidate = (source.encode()[:start] + replacement.encode() + source.encode()[end:]).decode()
    modified, _, target = target_node(candidate)
    target.test = node.test
    if ast.dump(tree, include_attributes=False) != ast.dump(modified, include_attributes=False):
        raise ValueError("PATCH_AST_OUTSIDE_CONDITION")
    if not original:
        raise ValueError("PATCH_EMPTY_BASELINE")
    compile(candidate, TARGET, "exec")
    patch = "".join(difflib.unified_diff(source.splitlines(True), candidate.splitlines(True),
                                      fromfile="a/" + TARGET, tofile="b/" + TARGET))
    return {"stage": "VALIDATED", "proposal": proposal.model_dump(), "patch": patch, "source": candidate,
            "patch_ref": digest(patch), "candidate_sha256": source_hash(candidate)}
