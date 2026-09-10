"""文档分类服务图谱增强接入测试。"""

from types import SimpleNamespace

from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.knowledge_graph.schemas import (
    GraphCandidateSupport,
    GraphClassificationResult,
    GraphSemanticResult,
    SemanticCategorySupport,
)


class SupportingGraphContext:
    """返回候选支持的测试图谱上下文。"""

    def __init__(self) -> None:
        self.seeds = []

    def expand_candidates(self, *, candidates, document_id, document_version_id, limit):
        """记录输入并支持第一个候选。"""

        self.seeds = list(candidates)
        first = self.seeds[0]
        return GraphClassificationResult(
            status="COMPLETED",
            candidates=[
                GraphCandidateSupport(
                    category_id=first.category_id,
                    graph_key=first.graph_key,
                    category_path=list(first.category_path),
                    graph_score=0.7,
                    confirmed_support_score=0.5,
                    support_count=2,
                    paths=[{"type": "CONFIRMED_NEIGHBOR", "support_count": 2}],
                )
            ],
        )

    def health_check(self):
        """返回测试健康状态。"""

        return {"status": "ok"}


class FailingGraphContext:
    """模拟 Neo4j 查询异常。"""

    def expand_candidates(self, **kwargs):
        """抛出连接异常。"""

        raise ConnectionError("neo4j unavailable")

    def health_check(self):
        """返回测试故障状态。"""

        return {"status": "unavailable"}


class SupportingSemanticContext:
    """根据规则候选稳定返回语义支持。"""

    def __init__(self) -> None:
        self.full_text = ""

    def retrieve(self, *, full_text, **kwargs):
        self.full_text = full_text
        return GraphSemanticResult(
            status="COMPLETED",
            candidates=[
                SemanticCategorySupport(
                    category_id="school.hr.title-review",
                    graph_key=(
                        "unified_school_file_classification:2026-09-v10:"
                        "school.hr.title-review"
                    ),
                    category_path=["学校", "人事师资", "职称"],
                    semantic_score=0.9,
                    support_count=2,
                )
            ],
        )


def _service_with_located_page(service, *, text: str):
    """为图谱单测提供可定位正文，满足新版业务分类证据硬门槛。"""

    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content=text,
            page_number=1,
            sheet_name=None,
        )
    ]
    return service


def test_document_classification_service_adds_graph_scores_without_passing_full_text():
    """分类服务应只把候选标识交给图谱，并保留正文证据链。"""

    graph_context = SupportingGraphContext()
    result = _service_with_located_page(
        DocumentClassificationService(graph_context=graph_context),
        text="校属各单位教师职称申报材料。",
    ).classify(
        document_id="document-graph",
        extraction_run_id="run-graph",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert graph_context.seeds
    assert not hasattr(graph_context.seeds[0], "full_text")
    assert result["graph_status"] == "COMPLETED"
    assert result["taxonomy_key"] == "unified_school_file_classification"
    assert result["taxonomy_version"]
    assert result["classifier_version"]
    assert result["categories"][0]["candidate_scores"]["graph"] == 0.7
    assert result["categories"][0]["evidence_items"][0]["quote"]


def test_document_classification_service_degrades_when_graph_query_fails():
    """Neo4j 查询失败时，现有分类必须继续完成并返回降级警告。"""

    result = _service_with_located_page(
        DocumentClassificationService(graph_context=FailingGraphContext()),
        text="校属各单位教师职称申报材料。",
    ).classify(
        document_id="document-graph-fallback",
        extraction_run_id="run-graph-fallback",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert result["status"] == "COMPLETED"
    assert result["graph_status"] == "DEGRADED"
    assert result["graph_warnings"] == ["GRAPH_UNAVAILABLE"]
    assert result["categories"][0]["name"] == "学校/人事师资/职称"


def test_shadow_mode_runs_semantic_retrieval_without_changing_visible_candidates():
    """Shadow 必须执行完整正文语义召回，但用户结果仍保持基础候选。"""

    semantic_context = SupportingSemanticContext()
    result = _service_with_located_page(
        DocumentClassificationService(
            graph_mode="shadow",
            semantic_context=semantic_context,
        ),
        text="校属各单位教师职称申报材料。",
    ).classify(
        document_id="document-shadow",
        extraction_run_id="run-shadow",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert semantic_context.full_text == "校属各单位教师职称申报材料。"
    assert result["semantic_status"] == "COMPLETED"
    assert result["graph_mode"] == "shadow"
    assert "semantic" not in result["categories"][0].get("candidate_scores", {})


def test_enabled_mode_adds_semantic_score_to_suggested_category():
    """enabled 模式只增强建议分量，不自动形成正式分类。"""

    result = _service_with_located_page(
        DocumentClassificationService(
            graph_mode="enabled",
            semantic_context=SupportingSemanticContext(),
        ),
        text="校属各单位教师职称申报材料。",
    ).classify(
        document_id="document-enabled",
        extraction_run_id="run-enabled",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert result["categories"][0]["candidate_scores"]["semantic"] == 0.9
    assert result["categories"][0]["status"] != "CONFIRMED"
