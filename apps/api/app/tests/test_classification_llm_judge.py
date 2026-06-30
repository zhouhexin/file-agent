"""分类 LLM 判定器测试。"""

from app.modules.classification.llm_judge import LLMClassificationJudge


class FakeLLMClient:
    """测试用 LLM 客户端，直接返回预设 JSON。"""

    def __init__(self, payload: dict) -> None:
        """保存预设模型输出。"""

        self.payload = payload

    def complete_json(self, *, system_prompt: str, user_payload: dict) -> dict:
        """返回预设模型输出，并保留调用形态与真实客户端一致。"""

        return self.payload


def _candidate() -> dict:
    """构造一个候选分类。"""

    return {
        "name": "学校/人事师资/考核聘任",
        "category_id": "school.hr.appointment-assessment",
        "category_path": ["学校", "人事师资", "考核聘任"],
        "confidence": 0.72,
        "status": "SUGGESTED",
        "source": "rule",
        "evidence": ["教师", "聘期", "续聘"],
        "taxonomy_key": "school_file_classification",
        "taxonomy_version": "2026-06-v2",
    }


def test_llm_judge_accepts_only_candidate_category_ids():
    """默认情况下，LLM 输出非候选 category_id 必须被拒绝。"""

    judge = LLMClassificationJudge(
        client=FakeLLMClient(
            {
                "labels": [
                    {
                        "category_id": "school.hr.appointment-assessment",
                        "confidence": 0.88,
                        "reason": "文档围绕教师聘期考核和续聘。",
                        "evidence": [
                            {
                                "quote": "请各学院组织专任教师完成岗位续聘材料提交。",
                                "signals": ["教师", "续聘"],
                            }
                        ],
                    },
                    {
                        "category_id": "model.created.path",
                        "category_path": ["模型", "自由分类"],
                        "confidence": 0.91,
                        "reason": "模型自行创建的路径。",
                        "evidence": [{"quote": "自由分类", "signals": ["自由分类"]}],
                    },
                ]
            }
        )
    )

    categories = judge.judge(
        filename="聘期考核.docx",
        document_text="请各学院组织专任教师完成岗位续聘材料提交。",
        candidates=[_candidate()],
    )

    assert len(categories) == 1
    assert categories[0]["category_id"] == "school.hr.appointment-assessment"
    assert categories[0]["source"] == "hybrid"
    assert categories[0]["confidence"] == 0.88
    assert categories[0]["evidence_items"][0]["quote"] == "请各学院组织专任教师完成岗位续聘材料提交。"


def test_llm_judge_marks_unlocated_quote_as_needs_review():
    """LLM 证据 quote 无法在原文定位时，结果必须降级为 NEEDS_REVIEW。"""

    judge = LLMClassificationJudge(
        client=FakeLLMClient(
            {
                "labels": [
                    {
                        "category_id": "school.hr.appointment-assessment",
                        "confidence": 0.86,
                        "reason": "模型给出无法定位的证据。",
                        "evidence": [{"quote": "原文不存在的句子", "signals": ["聘期"]}],
                    }
                ]
            }
        )
    )

    categories = judge.judge(
        filename="聘期考核.docx",
        document_text="请各学院组织专任教师完成岗位续聘材料提交。",
        candidates=[_candidate()],
    )

    assert categories[0]["status"] == "NEEDS_REVIEW"
    assert categories[0]["evidence_items"] == []


def test_llm_judge_can_allow_free_paths_as_review_only():
    """开启自由路径时，LLM 自建分类只能作为 NEEDS_REVIEW 建议保留。"""

    judge = LLMClassificationJudge(
        client=FakeLLMClient(
            {
                "labels": [
                    {
                        "category_id": "free.new.path",
                        "category_path": ["学校", "新增分类", "临时建议"],
                        "confidence": 0.77,
                        "reason": "模型认为候选不足。",
                        "evidence": [{"quote": "候选分类无法覆盖该临时事项。", "signals": ["临时事项"]}],
                    }
                ]
            }
        ),
        allow_free_category_paths=True,
    )

    categories = judge.judge(
        filename="临时事项.docx",
        document_text="候选分类无法覆盖该临时事项。",
        candidates=[_candidate()],
    )

    assert categories[0]["name"] == "学校/新增分类/临时建议"
    assert categories[0]["category_id"] is None
    assert categories[0]["source"] == "llm_free_path"
    assert categories[0]["status"] == "NEEDS_REVIEW"
    assert categories[0]["evidence_items"][0]["source"] == "llm_free_path"
