"""Explicit deployment command for the bundled 2019 school journal catalog."""

from __future__ import annotations

import json

from app.core.database import SessionLocal, init_database
from app.modules.files.office_conversion import OfficeConversionError
from .service import ASSET_PATH, CATALOG_KEY, ReferenceCatalogError, seed_catalog


def main() -> int:
    init_database()
    try:
        with SessionLocal.begin() as db:
            result = seed_catalog(db)
            receipt = {"catalog_key": CATALOG_KEY, "catalog_version_id": result.id,
                       "document_version_id": result.document_version_id,
                       "source_sha256": result.source_sha256,
                       "entry_count": result.entry_count,
                       "source_asset": str(ASSET_PATH),
                       "index_storage_path": result.index_storage_path}
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    except (ReferenceCatalogError, OfficeConversionError) as exc:
        print(json.dumps({"code": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
