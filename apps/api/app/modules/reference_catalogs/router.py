"""Authenticated read-only access to deployed shared reference catalogs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from .service import CATALOG_KEY, ReferenceCatalogError, catalog_info, query_catalog


router = APIRouter(prefix="/api/reference-catalogs", tags=["reference-catalogs"])


def _require_catalog(catalog_key: str) -> None:
    if catalog_key != CATALOG_KEY:
        raise HTTPException(status_code=404, detail={"code": "REFERENCE_CATALOG_NOT_FOUND", "message": "参考目录不存在"})


def _error(exc: ReferenceCatalogError) -> HTTPException:
    return HTTPException(status_code=503 if exc.code in {
        "REFERENCE_CATALOG_UNAVAILABLE", "REFERENCE_CATALOG_INDEX_CHANGED"} else 422,
        detail={"code": exc.code, "message": str(exc)})


@router.get("/{catalog_key}")
def get_reference_catalog(catalog_key: str, db=Depends(get_db), user=Depends(get_current_user)):
    _require_catalog(catalog_key)
    try:
        return catalog_info(db)
    except ReferenceCatalogError as exc:
        raise _error(exc) from exc


@router.get("/{catalog_key}/query")
def query_reference_catalog(
    catalog_key: str,
    issn: str | None = Query(default=None, max_length=120),
    journal_name: str | None = Query(default=None, max_length=500),
    include_rules: bool = False,
    db=Depends(get_db), user=Depends(get_current_user),
):
    _require_catalog(catalog_key)
    try:
        return query_catalog(db, issn=issn, journal_name=journal_name, include_rules=include_rules)
    except ReferenceCatalogError as exc:
        raise _error(exc) from exc
