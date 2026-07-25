"""工作副本高风险操作的确定性自然语言识别规则。

本模块只负责识别用户是否在表达“移除文件”的意图，不解析文件 ID，也不执行任何物理副作用。
命中后仍必须由会话附件上下文确定对象，并通过 OperationPlan 让用户确认后才能移入可恢复回收站。
"""

from __future__ import annotations

import re


_RECYCLE_BIN_PHRASES = (
    "移入回收站",
    "放入回收站",
    "移到回收站",
    "放到回收站",
    "丢进回收站",
)

_FILE_REMOVAL_VERBS = (
    "删除",
    "删掉",
    "删了",
    "移除",
    "去掉",
    "清除",
    "丢弃",
    "扔掉",
    "撤回",
    "撤销",
    "取消",
)

_FILE_TARGETS = (
    "文件",
    "附件",
    "文档",
    "材料",
    "表格",
    "上传内容",
)

_DELETE_NEGATIONS = (
    "不要删除",
    "不用删除",
    "不需要删除",
    "无需删除",
    "不要删掉",
    "不用删掉",
    "不需要删掉",
    "别删除",
    "别删",
    "不要移除",
    "不用移除",
    "不要移入回收站",
    "不用移入回收站",
    "别移入回收站",
    "别放入回收站",
    "别移到回收站",
)

_PRONOUN_REMOVAL_PHRASES = (
    "删除它",
    "删掉它",
    "把它删除",
    "把它删掉",
    "把它删了",
    "它不要了",
    "它不需要了",
    "它不用了",
)

_CONTEXT_REFERENCES = (
    "刚才",
    "刚刚",
    "刚上传",
    "已上传",
    "上传的",
    "上面",
    "上文",
    "前面",
    "之前",
    "这个",
    "这些",
    "这份",
    "该文件",
    "该附件",
    "那个文件",
    "那个附件",
    "所选",
    "选中",
    "它",
    "它们",
)

_CONTENT_EDIT_TARGETS = (
    "文件中",
    "文件里",
    "文档中",
    "文档里",
    "附件中",
    "附件里",
    "表格中",
    "表格里",
    "正文中",
    "正文里",
    "文件名",
    "文档名",
    "附件名",
    "文件内容",
    "文档内容",
    "附件内容",
    "文件分类",
    "文档分类",
    "文件标签",
    "文档标签",
    "文件关键词",
    "文件缓存",
)


def has_trash_working_copy_intent(message: str) -> bool:
    """识别用户是否要求移除文件，并统一收敛为可恢复回收站计划。

    规则兼容用户不知道“回收站”术语时常说的“删掉、移除、不要了”等表达。
    涉及文件内容、文件名、分类或聊天记录的删除请求必须排除，避免误生成物理文件计划。
    """

    compact = _normalize_message(message)
    if not compact:
        return False
    if _has_removal_negation(compact):
        return False
    if any(target in compact for target in _CONTENT_EDIT_TARGETS):
        return False
    if any(phrase in compact for phrase in _RECYCLE_BIN_PHRASES):
        return True
    if any(phrase in compact for phrase in _PRONOUN_REMOVAL_PHRASES):
        return True

    has_file_target = any(target in compact for target in _FILE_TARGETS)
    if not has_file_target:
        return False
    has_removal_action = any(verb in compact for verb in _FILE_REMOVAL_VERBS)
    no_longer_needed = _has_no_longer_needed_file(compact)
    return has_removal_action or no_longer_needed


def has_contextual_file_removal_reference(message: str) -> bool:
    """判断删除表达是否明确指向当前会话中的文件上下文。

    该结果只允许上下文服务尝试解析真实附件；若候选为空或存在歧义，后续服务仍必须停止并要求用户明确，
    不能由 Planner 或 LLM 猜测 document_id。
    """

    compact = _normalize_message(message)
    return has_trash_working_copy_intent(message) and any(
        reference in compact for reference in _CONTEXT_REFERENCES
    )


def _normalize_message(message: str) -> str:
    """移除不影响意图的空白和常见标点，保留中文语义词。"""

    return re.sub(r"""[\s，。！？、,.!?:：;；“”"'（）()]+""", "", message).lower()


def _has_removal_negation(compact: str) -> bool:
    """识别“别删除、不要把文件放入回收站”等否定指令。"""

    if any(phrase in compact for phrase in _DELETE_NEGATIONS):
        return True
    return bool(
        re.search(
            r"(?:不要|不用|不需要|无需|不必|别|请勿|禁止).{0,12}"
            r"(?:删除|删掉|删了|移除|去掉|清除|丢弃|扔掉|撤回|撤销|回收站)",
            compact,
        )
    )


def _has_no_longer_needed_file(compact: str) -> bool:
    """识别“这个文件不用了”，但排除“这个文件不用修改”等其他任务否定。"""

    target_pattern = r"(?:文件|附件|文档|材料|表格|上传内容)"
    reference_pattern = (
        r"(?:这个|这些|这份|该|刚才那个|刚刚那个|刚上传的|"
        r"刚刚上传的|刚才上传的|上面的|前面的|所选的|选中的)?"
    )
    before_target = re.search(
        rf"(?:不要|不需要|不用|不再需要|无需保留|不需要保留|不想保留|不保留)"
        rf"{reference_pattern}{target_pattern}(?:了|啦|了吧)?$",
        compact,
    )
    after_target = re.search(
        rf"{target_pattern}(?:我)?(?:不要了|不需要了|不用了|不再需要了|不保留了|不想保留了)$",
        compact,
    )
    return bool(before_target or after_target)
