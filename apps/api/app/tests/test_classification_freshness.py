"""分类运行时身份与刷新任务幂等键测试。"""

from app.modules.classification.freshness import (
    ClassificationRuntimeIdentity,
    classification_refresh_deduplication_key,
)


def test_refresh_key_tracks_arbitrary_taxonomy_and_classifier_versions():
    """任意未来版本发生变化时都应自然生成新的刷新任务键。"""

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
