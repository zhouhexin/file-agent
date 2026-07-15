"""文档分类服务图谱增强接入测试。"""

from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.knowledge_graph.schemas import GraphCandidateSupport, GraphClassificationResult


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


def test_document_classification_service_adds_graph_scores_without_passing_full_text():
    """分类服务应只把候选标识交给图谱，并保留正文证据链。"""

    graph_context = SupportingGraphContext()
    result = DocumentClassificationService(graph_context=graph_context).classify(
        document_id="document-graph",
        extraction_run_id="run-graph",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert graph_context.seeds
    assert not hasattr(graph_context.seeds[0], "full_text")
    assert result["graph_status"] == "COMPLETED"
    assert result["categories"][0]["candidate_scores"]["graph"] == 0.7
    assert result["categories"][0]["evidence_items"][0]["quote"]


def test_document_classification_service_degrades_when_graph_query_fails():
    """Neo4j 查询失败时，现有分类必须继续完成并返回降级警告。"""

    result = DocumentClassificationService(graph_context=FailingGraphContext()).classify(
        document_id="document-graph-fallback",
        extraction_run_id="run-graph-fallback",
        filename="职称申报材料.txt",
        fallback_text="本文件涉及教师职称申报材料。",
    )

    assert result["status"] == "COMPLETED"
    assert result["graph_status"] == "DEGRADED"
    assert result["graph_warnings"] == ["GRAPH_UNAVAILABLE"]
    assert result["categories"][0]["name"] == "学校/人事师资/职称"
