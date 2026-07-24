"""阶段四只读文件搜索 API 测试。

保护普通用户入口只返回自己的活动工作副本安全投影，不要求 GPU、LLM 或真实 PostgreSQL。
"""

from uuid import uuid4

from app.db.models import Document, DocumentSearchProfile, DocumentSummary, DocumentVersion, User, WorkingCopy
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.tests.helpers import client_with_database


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册并登录测试用户，返回稳定业务 ID 与 Bearer token。"""

    registered = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return registered.json()["id"], token.json()["access_token"]


def _add_profile(db, *, user: User, filename: str, summary_text: str) -> str:
    """写入 API 词法检索所需的最小当前工作副本事实与瘦投影。"""

    document_id = str(uuid4())
    version_id = str(uuid4())
    working_copy_id = str(uuid4())
    shared_workspace_id = get_shared_workspace_id(db)
    document = Document(
        id=document_id,
        user_id=user.id,
        workspace_id=shared_workspace_id,
        original_filename=filename,
        content_type="text/plain",
        size_bytes=12,
        sha256=uuid4().hex * 2,
    )
    version = DocumentVersion(
        id=version_id,
        document_id=document_id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path=f"work/{filename}",
        filename=filename,
        content_type="text/plain",
        size_bytes=12,
        sha256=document.sha256,
        source_type="IMPORT",
    )
    working_copy = WorkingCopy(
        id=working_copy_id,
        working_copy_root_id=str(uuid4()),
        workspace_id=shared_workspace_id,
        managed_file_id=str(uuid4()),
        document_id=document_id,
        current_version_id=version_id,
        relative_path=filename,
        relative_path_hash=uuid4().hex * 2,
        filename=filename,
        extension="txt",
        size_bytes=12,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status="ACTIVE",
    )
    summary = DocumentSummary(
        id=str(uuid4()),
        document_id=document_id,
        document_version_id=version_id,
        extraction_run_id=str(uuid4()),
        input_sha256=document.sha256,
        summary_text=summary_text,
        summary_json={"year": 2025},
        coverage_json={},
        prompt_version="test-v1",
        schema_version="test-v1",
        status="COMPLETED",
    )
    profile = DocumentSearchProfile(
        id=str(uuid4()),
        user_id=user.id,
        workspace_id=shared_workspace_id,
        working_copy_id=working_copy_id,
        document_id=document_id,
        document_version_id=version_id,
        status="ACTIVE",
        normalized_filename="国家励志奖学金申请txt",
        filename_search_text="国家 励志 奖学金 申请",
        summary_search_text="国家 励志 奖学金 申请 材料",
        combined_search_text=f"国家 励志 奖学金 申请 {summary_text}",
    )
    db.add_all([document, version, working_copy, summary, profile])
    return document_id


def test_search_api_returns_only_current_users_safe_file_projection():
    """HTTP 搜索结果不得包含内部检索字段，也不得返回其他用户同主题文件。"""

    client, SessionLocal = client_with_database()
    owner_id, owner_token = _register_and_login(client, "stage4-search-owner")
    other_id, _ = _register_and_login(client, "stage4-search-other")
    db = SessionLocal()
    try:
        owner = db.get(User, owner_id)
        other = db.get(User, other_id)
        owner_document_id = _add_profile(
            db, user=owner, filename="国家励志奖学金申请.txt", summary_text="奖学金申请材料",
        )
        _add_profile(
            db, user=other, filename="国家励志奖学金申请.txt", summary_text="奖学金申请材料",
        )
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/search",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"query": "找我的奖学金材料", "top_k": 20},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_returned"] == 1
    assert payload["files"][0]["document_id"] == owner_document_id
    assert {"search_text", "score", "tool_name", "absolute_path"}.isdisjoint(payload["files"][0])


def test_search_api_requires_authentication():
    """文件搜索必须通过当前用户 JWT 取得工作区，不能匿名读取投影。"""

    client, _ = client_with_database()
    response = client.post("/api/search", json={"query": "奖学金"})
    assert response.status_code == 401
