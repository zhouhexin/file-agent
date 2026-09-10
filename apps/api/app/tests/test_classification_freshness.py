"""D2/T12 分类输入与 PRIMARY 决策缓存新鲜度测试。"""

from app.modules.classification.input_fingerprint import (
    ClassificationContentFingerprintInput,
    PrimarySelectionFingerprintInput,
    build_content_fingerprint,
    build_primary_selection_fingerprint,
    digest_text,
)
from app.modules.classification.freshness import (
    ClassificationRuntimeIdentity,
    classification_refresh_deduplication_key,
)
from app.modules.classification.classifier_service import DocumentClassificationService


def _content_fingerprint(**overrides: str) -> str:
    """构造一个不含当前路径和系统生成名的内容层指纹。"""

    values = {
        "document_version_id": "version-1",
        "content_sha256": "a" * 64,
        "extracted_content_digest": digest_text("第一页正文"),
        "structure_digest": digest_text("1::5"),
        "parser_version": "parser-v1",
        "taxonomy_key": "unified_school_file_classification",
        "taxonomy_version": "2026-09-v10",
        "taxonomy_content_digest": "b" * 64,
        "rule_policy_id": "workdata-classification",
        "rule_policy_version": "workdata-v1",
        "summary_config_version": "extractive:v1:v1",
        "semantic_model_version": "disabled",
        "graph_policy_version": "off",
        "ingest_original_filename": "原始名称.docx",
    }
    values.update(overrides)
    return build_content_fingerprint(ClassificationContentFingerprintInput(**values))


def test_t12_same_facts_reuse_and_ocr_policy_or_model_change_invalidates():
    """相同事实指纹稳定；OCR 补页、规则、taxonomy 或模型变化均失效。"""

    baseline = _content_fingerprint()
    assert _content_fingerprint() == baseline
    assert _content_fingerprint(
        extracted_content_digest=digest_text("第一页正文\nOCR 补出的第二页")
    ) != baseline
    assert _content_fingerprint(rule_policy_version="workdata-v2") != baseline
    assert _content_fingerprint(taxonomy_version="2026-09-v11") != baseline
    assert _content_fingerprint(semantic_model_version="embedding-v2") != baseline


def test_t12_primary_cache_tracks_package_and_human_revision_separately():
    """用途包或人工 PRIMARY 变化只失效决策层，不改变内容层事实。"""

    content = _content_fingerprint()
    baseline = build_primary_selection_fingerprint(
        PrimarySelectionFingerprintInput(content_fingerprint=content)
    )
    package_changed = build_primary_selection_fingerprint(
        PrimarySelectionFingerprintInput(
            content_fingerprint=content,
            purpose_package_digest="package-v2",
        )
    )
    human_changed = build_primary_selection_fingerprint(
        PrimarySelectionFingerprintInput(
            content_fingerprint=content,
            existing_human_primary_id="school.finance",
            existing_human_primary_revision=2,
        )
    )

    assert package_changed != baseline
    assert human_changed != baseline
    assert content == _content_fingerprint()


def test_refresh_key_tracks_arbitrary_taxonomy_and_classifier_versions():
    """保留既有刷新任务契约：任意未来版本变化都必须生成新任务键。"""

    baseline = ClassificationRuntimeIdentity(
        taxonomy_key="school-taxonomy",
        taxonomy_version="release-a",
        classifier_version="classifier-a",
    )
    future_taxonomy = ClassificationRuntimeIdentity(
        taxonomy_key="school-taxonomy",
        taxonomy_version="future-release-without-fixed-name",
        classifier_version="classifier-a",
    )
    future_classifier = ClassificationRuntimeIdentity(
        taxonomy_key="school-taxonomy",
        taxonomy_version="release-a",
        classifier_version="future-classifier",
    )

    baseline_key = classification_refresh_deduplication_key(
        revision_id="revision-1",
        identity=baseline,
    )
    assert baseline_key == classification_refresh_deduplication_key(
        revision_id="revision-1",
        identity=baseline,
    )
    assert baseline_key != classification_refresh_deduplication_key(
        revision_id="revision-1",
        identity=future_taxonomy,
    )
    assert baseline_key != classification_refresh_deduplication_key(
        revision_id="revision-1",
        identity=future_classifier,
    )
    assert "release-a" not in baseline_key


def test_classify_validates_primary_fingerprint_before_cache_reuse(monkeypatch):
    """人工 PRIMARY 版本变化时，分类入口必须用新的决策指纹查询缓存。"""

    service = DocumentClassificationService(db=None, graph_mode="off")
    seen_primary_fingerprints: list[str] = []

    def fake_load_cached_categories(**kwargs):
        seen_primary_fingerprints.append(kwargs["primary_input_fingerprint"])
        return (
            [
                {
                    "name": "其他",
                    "category_id": "system.other",
                    "category_path": ["其他"],
                    "confidence": 0.0,
                    "status": "SUGGESTED",
                    "source": "fallback",
                    "evidence_items": [],
                    "relation_role": "PRIMARY",
                }
            ],
            {
                "classification_outcome": "OTHER",
                "classification_quality": "SUFFICIENT",
                "selection_basis": "FALLBACK_OTHER",
                "reason_codes": ["NO_RELIABLE_BUSINESS_PRIMARY"],
                "primary_input_fingerprint": kwargs["primary_input_fingerprint"],
            },
        )

    monkeypatch.setattr(service, "_load_cached_categories", fake_load_cached_categories)
    common = {
        "document_id": "document-1",
        "document_version_id": "version-1",
        "extraction_run_id": "extraction-1",
        "filename": "原始名称.docx",
        "fallback_text": "普通正文",
        "content_sha256": "a" * 64,
    }

    first = service.classify(
        **common,
        existing_human_primary={"category_id": "school.finance", "revision": 1},
    )
    second = service.classify(
        **common,
        existing_human_primary={"category_id": "school.finance", "revision": 2},
    )

    assert first["classification_reused"] is True
    assert second["classification_reused"] is True
    assert len(seen_primary_fingerprints) == 2
    assert seen_primary_fingerprints[0] != seen_primary_fingerprints[1]
