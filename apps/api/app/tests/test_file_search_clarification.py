"""文件检索同义短语与歧义选择回归测试。

这些测试保护用户表达不会被静默拆成宽泛 OR，并确保选择记录可以跨刷新恢复、
不能跨用户处理且重复提交保持幂等。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import AgentRun, Base, Conversation, Message, utcnow
from app.modules.retrieval.clarification_service import (
    FileSearchClarificationError,
    FileSearchClarificationService,
)
from app.modules.retrieval.clarification_planner import (
    FileSearchClarificationPlanner,
)
from app.modules.retrieval.phrase_strategy import FileSearchPhraseStrategyService
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.synonym_service import FileSearchSynonymService
from app.modules.agent.tool_registry import (
    _execute_controlled_file_search,
    _require_large_search_result_confirmation,
)
from app.modules.conversations.schemas import SendMessageRequest
from app.modules.conversations.service import ConversationMessageService


class _Tokenizer:
    """测试分词器只返回完整输入，避免依赖本机 Jieba 版本。"""

    def tokenize(self, text: str) -> list[str]:
        return [text] if text else []


@dataclass(frozen=True)
class _Parsed:
    """PhraseStrategy 只需替换 cleaned 和 terms 的最小查询对象。"""

    cleaned: str
    terms: list[str]
    relation_mode: str = "LITERAL"


class _FakeTwoStageSearch:
    """返回固定正文与文件级命中，验证最终证据门槛。"""

    def search(self, *, exact_phrase: str, **_kwargs):
        if exact_phrase == "任职通知":
            return {
                "partial": False,
                "results": [
                    {
                        "working_copy_id": "wc-body",
                        "document_id": "doc-body",
                        "filename": "干部材料.docx",
                        "overview": "",
                        "category_path": [],
                        "_body_phrase_hit": True,
                    },
                    {
                        "working_copy_id": "wc-noise",
                        "document_id": "doc-noise",
                        "filename": "普通述职报告.docx",
                        "overview": "分别出现任职和通知",
                        "category_path": [],
                        "_body_phrase_hit": False,
                    },
                ],
            }
        return {"partial": False, "results": []}


class _FakeSynonymSearch:
    """为原短语和同义短语返回不同文件集合，触发真实选择卡分支。"""

    def search(self, *, exact_phrase: str, **_kwargs):
        mapping = {
            "任职通知": ("wc-exact", "任职通知汇编.docx"),
            "任职通告": ("wc-alias", "任职通告汇编.docx"),
        }
        match = mapping.get(exact_phrase)
        if match is None:
            return {"partial": False, "results": []}
        return {
            "partial": False,
            "results": [
                {
                    "working_copy_id": match[0],
                    "document_id": f"doc-{match[0]}",
                    "filename": match[1],
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                }
            ],
        }


class _FakeManySearch:
    """返回超过直接展示阈值的稳定候选集合。"""

    def search(self, **_kwargs):
        return {
            "partial": False,
            "results": [
                {
                    "working_copy_id": f"wc-{index:02d}",
                    "document_id": f"doc-{index:02d}",
                    "filename": f"工作总结-{index:02d}.docx",
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                }
                for index in range(25)
            ],
        }


class _FakeCollegeAliasSearch:
    """分别返回机构和主题候选，用于验证最终必须取交集。"""

    def search(self, *, exact_phrase: str, **_kwargs):
        mapping = {
            "计算机科学与工程学院": [
                {
                    "working_copy_id": "wc-computer-college",
                    "document_id": "doc-computer-college",
                    "filename": "计算机科学与工程学院2025年工作总结.docx",
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                },
                {
                    "working_copy_id": "wc-entity-only",
                    "document_id": "doc-entity-only",
                    "filename": "计算机科学与工程学院会议通知.docx",
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                },
            ],
            "工作总结": [
                {
                    "working_copy_id": "wc-computer-college",
                    "document_id": "doc-computer-college",
                    "filename": "计算机科学与工程学院2025年工作总结.docx",
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                },
                {
                    "working_copy_id": "wc-topic-only",
                    "document_id": "doc-topic-only",
                    "filename": "人文学院2025年工作总结.docx",
                    "overview": "",
                    "category_path": [],
                    "_body_phrase_hit": True,
                },
            ],
        }
        return {
            "partial": False,
            "results": mapping.get(exact_phrase, []),
        }


def _db():
    """创建启用完整 ORM 表的 SQLite 会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _options():
    """生成包含执行参数和公开标签的固定选择项。"""

    return [
        {
            "id": "exact",
            "label": "只查“任职通知”",
            "description": "完整短语",
            "examples": ["任职通知"],
            "estimated_count": 0,
            "phrases": ["任职通知"],
            "match_mode": "LITERAL",
            "require_body_evidence": True,
            "display_content": "只按“任职通知”继续查找",
        },
        {
            "id": "synonyms",
            "label": "包含相近表达",
            "description": "同义短语",
            "examples": ["任职通告"],
            "estimated_count": 2,
            "phrases": ["任职通知", "任职通告"],
            "match_mode": "RELATED",
            "require_body_evidence": True,
            "display_content": "按同义表达继续查找",
        },
        {
            "id": "custom",
            "label": "使用其他关键词",
            "description": "",
            "examples": [],
            "estimated_count": None,
            "phrases": [],
            "match_mode": "LITERAL",
            "require_body_evidence": True,
        },
    ]


def test_query_parser_distinguishes_literal_related_and_unspecified():
    """关系词必须决定证据门槛，但不能改变清洗后的核心短语。"""

    parser = FileSearchQueryParser(tokenizer=_Tokenizer())

    literal = parser.parse("哪些文件提到了任职通知")
    related = parser.parse("查找与任职通知有关的文件")
    unspecified = parser.parse("查找任职通知文件")

    assert (literal.cleaned, literal.relation_mode) == ("任职通知", "LITERAL")
    assert (related.cleaned, related.relation_mode) == ("任职通知", "RELATED")
    assert (unspecified.cleaned, unspecified.relation_mode) == ("任职通知", "UNSPECIFIED")


def test_synonym_service_returns_complete_phrases_and_broad_topics_separately():
    """同义短语和宽泛主题必须分开，不能默认生成“任职 OR 通知”。"""

    group = FileSearchSynonymService().find_group("任职通告")

    assert group is not None
    assert group.canonical == "任职通知"
    assert "任职告示" in group.phrases
    assert group.broad_topics == ("任职", "通知")


def test_computer_college_short_name_expands_inside_full_query_without_choice():
    """学院简称等价扩展后应与主题取交集，并允许年份位于两者之间。"""

    synonym_service = FileSearchSynonymService()
    assert synonym_service.expand_equivalent_mentions(
        "计算机学院的工作总结"
    ) == (
        "计算机学院的工作总结",
        "计算机科学与工程学院的工作总结",
    )
    db = _db()
    db.add(Conversation(id="conv-college-alias", user_id="user-1", title=""))
    db.flush()

    result = _execute_controlled_file_search(
        db=db,
        user_id="user-1",
        conversation_id="conv-college-alias",
        agent_run_id=None,
        tool_input=SimpleNamespace(
            match_mode="AUTO",
            phrases=[],
            require_body_evidence=False,
        ),
        search_query="帮我找2025年计算机学院的工作总结",
        parsed=_Parsed(
            cleaned="计算机学院的工作总结",
            terms=["计算机学院", "工作总结"],
            relation_mode="UNSPECIFIED",
        ),
        scope=object(),
        tokenizer=_Tokenizer(),
        search_service=_FakeCollegeAliasSearch(),
    )

    assert [item["document_id"] for item in result["results"]] == [
        "doc-computer-college"
    ]
    assert result["total_returned"] == 1
    assert "search_clarification" not in result


def test_unspecified_query_with_different_synonym_results_creates_selection():
    """未说明匹配范围且结果集合不同，必须持久化选择卡而不是静默扩展。"""

    db = _db()
    db.add(Conversation(id="conv-choice", user_id="user-1", title=""))
    db.flush()
    parsed = _Parsed(
        cleaned="任职通知",
        terms=["任职通知"],
        relation_mode="UNSPECIFIED",
    )

    result = _execute_controlled_file_search(
        db=db,
        user_id="user-1",
        conversation_id="conv-choice",
        agent_run_id=None,
        tool_input=SimpleNamespace(
            match_mode="AUTO",
            phrases=[],
            require_body_evidence=False,
        ),
        search_query="查找任职通知文件",
        parsed=parsed,
        scope=object(),
        tokenizer=_Tokenizer(),
        search_service=_FakeSynonymSearch(),
    )

    assert result["results"] == []
    clarification = result["search_clarification"]
    assert clarification["status"] == "WAITING_SELECTION"
    assert [item["id"] for item in clarification["options"]] == [
        "exact",
        "synonyms",
        "broad-1",
        "broad-2",
        "custom",
    ]


def test_literal_phrase_strategy_drops_file_level_noise_without_body_evidence():
    """原文查询只能返回正文连续命中的文件。"""

    result = FileSearchPhraseStrategyService(
        search_service=_FakeTwoStageSearch(),
        tokenizer=_Tokenizer(),
    ).search(
        original_query="哪些文件提到了任职通知",
        parsed_query=_Parsed(cleaned="任职通知", terms=["任职通知"]),
        scope=object(),
        phrases=["任职通知"],
        require_body_evidence=True,
    )

    assert [item["document_id"] for item in result["results"]] == ["doc-body"]


def test_large_phrase_result_is_not_silently_truncated_to_twenty():
    """检索层必须保留全部命中，20 条只作为对话确认阈值。"""

    result = FileSearchPhraseStrategyService(
        search_service=_FakeManySearch(),
        tokenizer=_Tokenizer(),
    ).search(
        original_query="查找工作总结",
        parsed_query=_Parsed(cleaned="工作总结", terms=["工作总结"]),
        scope=object(),
        phrases=["工作总结"],
        require_body_evidence=False,
    )

    assert result["total_returned"] == 25
    assert len(result["results"]) == 25


def test_large_result_requires_confirmation_and_yes_resumes_with_all_files():
    """超过 20 条先询问；自然语言确认后同一查询可以展示全部结果。"""

    db = _db()
    db.add(Conversation(id="conv-show-all", user_id="user-1", title=""))
    db.flush()
    full_result = FileSearchPhraseStrategyService(
        search_service=_FakeManySearch(),
        tokenizer=_Tokenizer(),
    ).search(
        original_query="查找工作总结",
        parsed_query=_Parsed(cleaned="工作总结", terms=["工作总结"]),
        scope=object(),
        phrases=["工作总结"],
        require_body_evidence=False,
    )

    pending = _require_large_search_result_confirmation(
        db=db,
        user_id="user-1",
        conversation_id="conv-show-all",
        agent_run_id=None,
        search_query="查找工作总结",
        core_phrase="工作总结",
        result=full_result,
        show_all_results=False,
    )

    assert pending["results"] == []
    assert pending["total_returned"] == 25
    clarification = pending["search_clarification"]
    assert clarification["selection_type"] == "RESULT_LIMIT_CONFIRMATION"
    assert clarification["prompt"] == "找到 25 个相关文件，查询结果较多，是否全部展示？"

    selection = FileSearchClarificationService(db).resolve_from_text(
        conversation_id="conv-show-all",
        user_id="user-1",
        message="是",
    )

    assert selection is not None
    assert selection.show_all_results is True
    plan = FileSearchClarificationPlanner(selection).plan()
    assert plan.steps[0].input["show_all_results"] is True
    displayed = _require_large_search_result_confirmation(
        db=db,
        user_id="user-1",
        conversation_id="conv-show-all",
        agent_run_id=None,
        search_query="查找工作总结",
        core_phrase="工作总结",
        result=full_result,
        show_all_results=True,
    )
    assert len(displayed["results"]) == 25
    assert displayed["show_all_results"] is True


def test_clarification_is_user_scoped_and_same_selection_is_idempotent():
    """其他用户不能处理选择，同一选项重复提交必须返回同一执行参数。"""

    db = _db()
    db.add(Conversation(id="conv-1", user_id="user-1", title=""))
    db.flush()
    service = FileSearchClarificationService(db)
    record = service.create(
        conversation_id="conv-1",
        user_id="user-1",
        agent_run_id=None,
        original_query="哪些文件提到了任职通知",
        core_phrase="任职通知",
        relation_mode="LITERAL",
        options=_options(),
    )

    with pytest.raises(FileSearchClarificationError):
        service.resolve(
            clarification_id=record.id,
            user_id="user-2",
            option_id="synonyms",
        )

    first = service.resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_id="synonyms",
    )
    second = service.resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_id="synonyms",
    )

    assert first == second
    assert first.phrases == ("任职通知", "任职通告")


def test_clarification_retry_reuses_bound_message_and_agent_run():
    """选择首次执行后必须保存结果标识，后续重试不能再生成第二份回答。"""

    db = _db()
    db.add(Conversation(id="conv-result", user_id="user-1", title=""))
    db.flush()
    service = FileSearchClarificationService(db)
    record = service.create(
        conversation_id="conv-result",
        user_id="user-1",
        agent_run_id=None,
        original_query="查找任职通知",
        core_phrase="任职通知",
        relation_mode="UNSPECIFIED",
        options=_options(),
    )
    service.resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_id="exact",
    )
    message = Message(
        conversation_id="conv-result",
        user_id="user-1",
        role="user",
        content="只查任职通知",
        attachments_json=[],
    )
    db.add(message)
    db.flush()
    run = AgentRun(
        conversation_id="conv-result",
        message_id=message.id,
        user_id="user-1",
        status="COMPLETED",
    )
    db.add(run)
    db.flush()

    service.mark_execution_result(
        clarification_id=record.id,
        user_id="user-1",
        message_id=message.id,
        agent_run_id=run.id,
    )
    retried = service.resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_id="exact",
    )

    assert retried.result_message_id == message.id
    assert retried.result_agent_run_id == run.id

    execution = ConversationMessageService(db)._execute_message(
        conversation_id="conv-result",
        request=SendMessageRequest(content="只查任职通知", attachments=[]),
        user_id="user-1",
        clarification_selection=retried,
    )

    assert execution.message.id == message.id
    assert execution.agent_run.agent_run_id == run.id
    assert db.query(Message).count() == 1
    assert db.query(AgentRun).count() == 1


def test_custom_phrase_is_persisted_and_cannot_change_on_retry():
    """自定义短语必须进入持久化解决记录，重试不能偷换另一个短语。"""

    db = _db()
    db.add(Conversation(id="conv-custom", user_id="user-1", title=""))
    db.flush()
    service = FileSearchClarificationService(db)
    record = service.create(
        conversation_id="conv-custom",
        user_id="user-1",
        agent_run_id=None,
        original_query="任职通知",
        core_phrase="任职通知",
        relation_mode="UNSPECIFIED",
        options=_options(),
    )

    resolved = service.resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_id="custom",
        custom_phrase="任职公示",
    )

    assert resolved.phrases == ("任职公示",)
    with pytest.raises(FileSearchClarificationError):
        service.resolve(
            clarification_id=record.id,
            user_id="user-1",
            option_id="custom",
            custom_phrase="干部公示",
        )


def test_natural_language_resolution_only_consumes_explicit_selection_reply():
    """普通新问题不能误消费待选择项，明确“按同义表达”才能续跑。"""

    db = _db()
    db.add(Conversation(id="conv-text", user_id="user-1", title=""))
    db.flush()
    service = FileSearchClarificationService(db)
    record = service.create(
        conversation_id="conv-text",
        user_id="user-1",
        agent_run_id=None,
        original_query="任职通知",
        core_phrase="任职通知",
        relation_mode="UNSPECIFIED",
        options=_options(),
    )

    assert service.resolve_from_text(
        conversation_id="conv-text",
        user_id="user-1",
        message="再找一下奖学金文件",
    ) is None
    resolved = service.resolve_from_text(
        conversation_id="conv-text",
        user_id="user-1",
        message="按同义表达查",
    )

    assert resolved is not None
    assert resolved.option_id == "synonyms"
    assert record.status == "RESOLVED"


def test_expired_clarification_cannot_be_resolved():
    """过期选择不能执行旧范围，用户必须重新发起检索。"""

    db = _db()
    db.add(Conversation(id="conv-expired", user_id="user-1", title=""))
    db.flush()
    service = FileSearchClarificationService(db)
    record = service.create(
        conversation_id="conv-expired",
        user_id="user-1",
        agent_run_id=None,
        original_query="任职通知",
        core_phrase="任职通知",
        relation_mode="UNSPECIFIED",
        options=_options(),
    )
    record.expires_at = utcnow() - timedelta(seconds=1)

    with pytest.raises(FileSearchClarificationError):
        service.resolve(
            clarification_id=record.id,
            user_id="user-1",
            option_id="exact",
        )
    assert record.status == "EXPIRED"
