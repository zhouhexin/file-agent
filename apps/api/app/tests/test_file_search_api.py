"""阶段四只读文件搜索 API 测试。

保护普通用户入口只返回共享工作区的活动工作副本安全投影，不要求 GPU、LLM 或真实 PostgreSQL。
Document.user_id 记录导入审计来源，不能再次切分所有用户共享的文件检索范围。
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.models import (
    Conversation,
    Document,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    RelevantFileSet,
    User,
    WorkingCopy,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.retrieval.router import FileSearchRequest, search_files
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


def test_search_api_returns_shared_workspace_safe_file_projection():
    """已认证用户可以检索共享工作区文件，但结果不得包含内部检索字段。"""

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
        other_document_id = _add_profile(
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
    assert payload["total_returned"] == 2
    assert {item["document_id"] for item in payload["files"]} == {
        owner_document_id,
        other_document_id,
    }
    assert all(item["revision"] == 1 for item in payload["files"])
    assert all(
        {"search_text", "score", "tool_name", "absolute_path"}.isdisjoint(item)
        for item in payload["files"]
    )


def test_search_api_requires_authentication():
    """文件搜索必须通过当前用户 JWT 取得工作区，不能匿名读取投影。"""

    client, _ = client_with_database()
    response = client.post("/api/search", json={"query": "奖学金"})
    assert response.status_code == 401


def test_workbuddy_style_search_creates_owned_conversation_only_for_final_results(monkeypatch):
    """首次 wb-* 搜索有最终结果时，应先创建会话再写相关文件集合。"""

    client, SessionLocal = client_with_database()
    user_id, _token = _register_and_login(client, "workbuddy-search-owner")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        document_id = _add_profile(
            db,
            user=user,
            filename="国家励志奖学金申请.txt",
            summary_text="奖学金申请材料",
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.document_id == document_id).one()
        conversation_id = "wb-1234567890abcdef1234567890abcdef"
        _force_final_result(monkeypatch, document_id=document_id, working_copy_id=working_copy.id)
        response = search_files(
            FileSearchRequest(query="奖学金", conversation_id=conversation_id),
            db=db,
            current_user=user,
        )
        assert response["total_returned"] == 1
        repeated_response = search_files(
            FileSearchRequest(query="奖学金", conversation_id=conversation_id),
            db=db,
            current_user=user,
        )
        assert repeated_response["total_returned"] == 1
        db.commit()
    finally:
        db.close()

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.user_id == user_id
        assert (
            db.query(Conversation)
            .filter(Conversation.id == conversation_id)
            .count()
            == 1
        )
        relevant_file_sets = (
            db.query(RelevantFileSet)
            .filter(RelevantFileSet.conversation_id == conversation_id)
            .all()
        )
        assert len(relevant_file_sets) == 2
        assert all(item.user_id == user_id for item in relevant_file_sets)


def test_workbuddy_style_empty_search_does_not_create_conversation():
    """空搜索不应为了满足外键而创建无意义的 WorkBuddy 会话占位。"""

    client, SessionLocal = client_with_database()
    _user_id, token = _register_and_login(client, "workbuddy-empty-search")
    conversation_id = "wb-abcdef1234567890abcdef1234567890"

    response = client.post(
        "/api/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "不会命中的唯一检索词", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert response.json()["total_returned"] == 0
    with SessionLocal() as db:
        assert db.get(Conversation, conversation_id) is None
        assert (
            db.query(RelevantFileSet)
            .filter(RelevantFileSet.conversation_id == conversation_id)
            .count()
            == 0
        )


def test_workbuddy_search_rejects_another_users_conversation_before_persisting(monkeypatch):
    """猜测到其他用户 wb-* ID 时不得新增集合或泄漏会话所有权。"""

    client, SessionLocal = client_with_database()
    owner_id, _owner_token = _register_and_login(client, "workbuddy-conversation-owner")
    _other_id, _other_token = _register_and_login(client, "workbuddy-conversation-other")
    db = SessionLocal()
    try:
        owner = db.get(User, owner_id)
        assert owner is not None
        document_id = _add_profile(
            db,
            user=owner,
            filename="国家励志奖学金申请.txt",
            summary_text="奖学金申请材料",
        )
        db.commit()
    finally:
        db.close()

    conversation_id = "wb-fedcba0987654321fedcba0987654321"
    db = SessionLocal()
    try:
        owner = db.get(User, owner_id)
        other = db.get(User, _other_id)
        assert owner is not None
        assert other is not None
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.document_id == document_id).one()
        _force_final_result(monkeypatch, document_id=document_id, working_copy_id=working_copy.id)
        search_files(
            FileSearchRequest(query="奖学金", conversation_id=conversation_id),
            db=db,
            current_user=owner,
        )
        db.commit()

        with pytest.raises(HTTPException) as error:
            search_files(
                FileSearchRequest(query="奖学金", conversation_id=conversation_id),
                db=db,
                current_user=other,
            )
        assert error.value.status_code == 403
    finally:
        db.close()

    with SessionLocal() as db:
        assert (
            db.query(RelevantFileSet)
            .filter(RelevantFileSet.conversation_id == conversation_id)
            .count()
            == 1
        )


def _force_final_result(monkeypatch, *, document_id: str, working_copy_id: str) -> None:
    """构造带稳定工作副本 ID 的最终结果，覆盖相关文件集合的外键写入路径。"""

    result = {
        "query": "奖学金",
        "total_returned": 1,
        "supported_count": 1,
        "possible_count": 0,
        "partial": False,
        "results": [
            {
                "document_id": document_id,
                "working_copy_id": working_copy_id,
                "relevance_tier": "SUPPORTED",
                "filename": "国家励志奖学金申请.txt",
            }
        ],
    }
    monkeypatch.setattr(
        "app.modules.retrieval.router.TwoStageFileSearchService.search",
        lambda *_args, **_kwargs: dict(result),
    )
    monkeypatch.setattr(
        "app.modules.retrieval.router.FileSearchPhraseStrategyService.search_with_topic_tiers",
        lambda *_args, **_kwargs: dict(result),
    )
    monkeypatch.setattr(
        "app.modules.retrieval.router.SearchCompletenessService.attach_safely",
        lambda _self, *, result, **_kwargs: result,
    )
