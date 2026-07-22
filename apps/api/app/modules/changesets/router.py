"""ChangeSet 查询路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import require_ops_or_admin
from app.modules.changesets.repository import ChangeSetRepository
from app.modules.changesets.schemas import ChangeItemResponse, ChangeSetResponse

router = APIRouter(prefix="/api/changesets", tags=["changesets"])


@router.get("/{changeset_id}", response_model=ChangeSetResponse)
def get_changeset(
    changeset_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> ChangeSetResponse:
    """允许 ops/admin 查询内部 ChangeSet 审计明细。"""

    repository = ChangeSetRepository(db)
    changeset = repository.get_by_id(changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    items = repository.list_items(changeset_id=changeset.id)
    return ChangeSetResponse(
        id=changeset.id,
        conversation_id=changeset.conversation_id,
        agent_run_id=changeset.agent_run_id,
        user_id=changeset.user_id,
        status=changeset.status,
        summary=changeset.summary,
        created_at=changeset.created_at,
        updated_at=changeset.updated_at,
        items=[
            ChangeItemResponse(
                id=item.id,
                target_type=item.target_type,
                target_id=item.target_id,
                target_document_id=item.target_document_id,
                change_type=item.change_type,
                before_value_json=item.before_value_json,
                after_value_json=item.after_value_json,
                source=item.source,
                confidence=item.confidence,
                evidence_json=item.evidence_json,
                execution_status=item.execution_status,
                created_at=item.created_at,
            )
            for item in items
        ],
    )
