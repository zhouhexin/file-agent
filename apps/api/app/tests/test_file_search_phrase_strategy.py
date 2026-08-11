"""文件检索主题分级策略测试。

这些测试保护“核心业务主题”和“宽泛动作词”必须被后端分别验证，防止仅凭
“发放”等泛词把文件展示为已经确认相关，也防止候选文件进入事实回答证据链。
"""

from app.modules.retrieval.phrase_strategy import (
    FileSearchPhraseStrategyService,
    mark_metadata_results_as_possible,
)
from app.modules.retrieval.query_parser import ParsedQuery


class _Tokenizer:
    """确定性分词 fake，避免测试依赖本地 Jieba 词典。"""

    def tokenize(self, text: str) -> list[str]:
        """按测试短语返回单个词，验证策略使用完整受控短语调用。"""

        return [text]


class _TieredSearchService:
    """模拟每个短语的正文命中，验证 Tool 侧而不是 LLM 侧的分级。"""

    def search(self, *, exact_phrase: str, **_kwargs):
        """返回固定的正文命中文件，不访问数据库或文件系统。"""

        def item(identifier: str, filename: str) -> dict:
            return {
                "working_copy_id": identifier,
                "document_id": f"doc-{identifier}",
                "document_version_id": f"ver-{identifier}",
                "filename": filename,
                "match_reasons": ["原文 Chunk 命中查询词"],
                "_body_phrase_hit": True,
            }

        rows = {
            "劳务费发放": [],
            "劳务费": [item("wc-supported", "劳务费发放说明.docx")],
            "发放": [
                item("wc-supported", "劳务费发放说明.docx"),
                item("wc-possible", "单项奖发放决定.doc"),
            ],
        }.get(exact_phrase, [])
        return {"ok": True, "partial": False, "results": rows}


def test_topic_tiers_keep_generic_action_hit_as_possible_only():
    """仅命中“发放”的文件必须降级为可能相关，不能标记为已验证相关。"""

    result = FileSearchPhraseStrategyService(
        search_service=_TieredSearchService(), tokenizer=_Tokenizer()
    ).search_with_topic_tiers(
        original_query="列出涉及劳务费发放的文件",
        parsed_query=ParsedQuery(cleaned="劳务费发放", original="列出涉及劳务费发放的文件", terms=["劳务费", "发放"]),
        scope=object(),
        exact_phrase="劳务费发放",
        required_topic_terms=["劳务费"],
        supporting_topic_terms=["发放"],
    )

    assert result["supported_count"] == 1
    assert result["possible_count"] == 1
    assert [item["relevance_tier"] for item in result["results"]] == [
        "SUPPORTED",
        "POSSIBLE",
    ]
    assert "尚未同时确认“劳务费”与“发放”" in result["results"][1]["match_reasons"][-1]


def test_metadata_fallback_never_marks_literal_topic_query_as_original_evidence():
    """正文索引降级时，即使摘要命中也只能作为可能相关候选。"""

    result = mark_metadata_results_as_possible(
        result={
            "ok": True,
            "results": [
                {
                    "document_id": "doc-summary-only",
                    "filename": "劳务费发放汇总.xlsx",
                    "match_reasons": ["摘要命中：劳务费发放"],
                }
            ],
        },
        parsed_query=ParsedQuery(
            original="列出涉及劳务费发放的文件",
            cleaned="劳务费发放",
            relation_mode="LITERAL",
            required_topic_terms=["劳务费"],
            supporting_topic_terms=["发放"],
        ),
    )

    assert result["supported_count"] == 0
    assert result["possible_count"] == 1
    assert result["partial"] is True
    assert result["results"][0]["relevance_tier"] == "POSSIBLE"
