"""分类运行时身份与受管源分类新鲜度判断。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    DocumentClassificationRun,
    ManagedFileRevision,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.runtime_factory import ClassificationRuntimeFactory


@dataclass(frozen=True, slots=True)
class ClassificationRuntimeIdentity:
    """唯一标识会影响分类结果的 taxonomy 与分类器组合。"""

    taxonomy_key: str
    taxonomy_version: str
    classifier_version: str

    @property
    def fingerprint(self) -> str:
        """生成可用于任务幂等键的稳定短指纹。"""

        payload = "\0".join(
            (self.taxonomy_key, self.taxonomy_version, self.classifier_version)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class ClassificationFreshness(str, Enum):
    """受管源分类相对于当前运行时身份的状态。"""

    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING = "MISSING"


def current_classification_identity(
    *,
    db: Session,
    settings: Settings,
    user_id: str,
) -> ClassificationRuntimeIdentity:
    """从当前部署动态读取分类身份，不绑定任何固定版本名称。"""

    taxonomy = load_default_taxonomy()
    factory = ClassificationRuntimeFactory(settings)
    return ClassificationRuntimeIdentity(
        taxonomy_key=taxonomy.key,
        taxonomy_version=taxonomy.version,
        classifier_version=factory.classifier_version_for_user(user_id=user_id),
    )


def inspect_managed_source_classification(
    *,
    db: Session,
    revision: ManagedFileRevision,
    identity: ClassificationRuntimeIdentity,
) -> ClassificationFreshness:
    """判断当前源修订是否已经按当前分类身份成功处理。"""

    if not revision.analysis_document_id:
        return ClassificationFreshness.MISSING
    latest_run = (
        db.query(DocumentClassificationRun)
        .filter(
            DocumentClassificationRun.document_id == revision.analysis_document_id,
            DocumentClassificationRun.status == "COMPLETED",
        )
        .order_by(DocumentClassificationRun.created_at.desc())
        .first()
    )
    if latest_run is None:
        return ClassificationFreshness.MISSING
    if (
        latest_run.taxonomy_key == identity.taxonomy_key
        and latest_run.taxonomy_version == identity.taxonomy_version
        and latest_run.classifier_version == identity.classifier_version
    ):
        return ClassificationFreshness.CURRENT
    return ClassificationFreshness.STALE


def classification_refresh_deduplication_key(
    *,
    revision_id: str,
    identity: ClassificationRuntimeIdentity,
) -> str:
    """生成随任意未来分类身份变化而自然变化的任务幂等键。"""

    return f"managed-source-classification:{revision_id}:{identity.fingerprint}"


def classification_refresh_priority(settings: Settings) -> int:
    """让轻量分类刷新略早于完整源解析，又不占用用户前台高优先级区间。"""

    return max(1, int(settings.managed_source_analysis_background_priority) - 10)
