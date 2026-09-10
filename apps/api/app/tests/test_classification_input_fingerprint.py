"""分类两层指纹测试。"""

from app.modules.classification.input_fingerprint import (
    ClassificationContentFingerprintInput,
    PrimarySelectionFingerprintInput,
    build_content_fingerprint,
    build_primary_selection_fingerprint,
)


def _content(**overrides) -> ClassificationContentFingerprintInput:
    """构造固定内容事实。"""

    value = {
        "document_version_id": "version-1",
        "content_sha256": "a" * 64,
        "extracted_content_digest": "text-1",
        "structure_digest": "structure-1",
        "parser_version": "parser-1",
        "taxonomy_key": "taxonomy",
        "taxonomy_version": "v10",
        "taxonomy_content_digest": "taxonomy-digest",
        "rule_policy_id": "workdata",
        "rule_policy_version": "v1",
        "summary_config_version": "summary-v1",
        "semantic_model_version": "disabled",
        "graph_policy_version": "off",
        "ingest_original_filename": "上传时名称.docx",
    }
    value.update(overrides)
    return ClassificationContentFingerprintInput(**value)


def test_ocr_policy_and_model_changes_invalidate_content_fingerprint():
    """T12：OCR 正文、taxonomy、规则和模型任一变化都必须使自动缓存失效。"""

    baseline = build_content_fingerprint(_content())

    assert baseline != build_content_fingerprint(_content(extracted_content_digest="ocr-page-2"))
    assert baseline != build_content_fingerprint(_content(taxonomy_version="v11"))
    assert baseline != build_content_fingerprint(_content(rule_policy_version="v2"))
    assert baseline != build_content_fingerprint(_content(semantic_model_version="model-v2"))
    assert baseline == build_content_fingerprint(_content())


def test_generated_filename_and_path_do_not_self_reinforce_classification():
    """T13：系统生成名和纯移动不进入内容指纹；原始导入名变化才改变事实。"""

    baseline = build_content_fingerprint(_content())
    same_after_move = build_content_fingerprint(_content())
    renamed_by_system = build_content_fingerprint(_content())
    new_ingest_name = build_content_fingerprint(
        _content(ingest_original_filename="另一上传名称.docx")
    )
    primary_a = build_primary_selection_fingerprint(
        PrimarySelectionFingerprintInput(content_fingerprint=baseline)
    )
    primary_b = build_primary_selection_fingerprint(
        PrimarySelectionFingerprintInput(
            content_fingerprint=baseline,
            purpose_package_digest="package-v2",
        )
    )

    assert baseline == same_after_move == renamed_by_system
    assert baseline != new_ingest_name
    assert primary_a != primary_b
