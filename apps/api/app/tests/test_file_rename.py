"""受管文件智能重命名测试。"""

from pathlib import Path

from app.db.models import ChangeItem, ManagedFile, OperationPlan
from app.modules.agent.planner import DeterministicPlanner
from app.modules.file_rename.metadata_extractor import FilenameMetadataExtractor
from app.modules.file_rename.schemas import RenameFieldStatus
from app.tests.helpers import clear_overrides, client_with_database


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册并登录测试用户。"""

    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return register_response.json()["id"], login_response.json()["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    """构造认证请求头。"""

    return {"Authorization": f"Bearer {token}"}


def test_filename_metadata_extractor_reads_official_document_fields():
    """规范公文应提取年份、完整文号和正文标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="扫描件.pdf",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": "校发〔2026〕12号\n关于做好奖学金评审工作的通知\n各学院：\n现将有关事项通知如下。",
            }
        ],
    )

    assert result.year.value == "2026"
    assert result.document_number.value == "校发〔2026〕12号"
    assert result.title.value == "关于做好奖学金评审工作的通知"
    assert result.year.status == RenameFieldStatus.RESOLVED
    assert result.document_number.status == RenameFieldStatus.RESOLVED
    assert result.title.status == RenameFieldStatus.RESOLVED


def test_filename_metadata_extractor_allows_missing_document_number():
    """普通材料没有文号时仍可按年份和标题生成降级名称。"""

    result = FilenameMetadataExtractor().extract(
        filename="活动总结.docx",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": "2026年春季学生活动总结\n本学期组织了多项活动。",
            }
        ],
    )

    assert result.year.value == "2026"
    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.title.value == "春季学生活动总结"
    assert result.can_build_filename is True


def test_deterministic_planner_routes_managed_rename_request():
    """确定性 Planner 应把受管目录改名请求路由到建议 Tool。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-1",
        user_id="user-1",
        message_id="message-1",
        message="按年份、文号和正文标题重命名党办目录下的文件",
        attachments=[],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.steps[0].tool_name == "generate-rename-suggestions"
    assert plan.steps[0].input["path_prefix"] == "党办"
    assert plan.confirmation_policy["operation_plan_required"] is True


def test_managed_rename_chat_plan_and_confirm_executes_native_rename(monkeypatch, tmp_path):
    """聊天生成计划后文件保持不变，本人确认后才真实重命名并写 ChangeSet。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "扫描件.txt"
    source_path.write_text(
        "校发〔2026〕12号\n关于做好奖学金评审工作的通知\n各学院：\n现将有关事项通知如下。",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(managed_root))
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES_ALLOW_RENAME", "true")

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-owner")
    response = client.post(
        "/api/conversations/rename-conversation/messages",
        headers=_auth_header(token),
        json={
            "content": "按年份、文号和正文标题重命名党办目录下的文件",
            "attachments": [],
        },
    )

    assert response.status_code == 200
    agent_run = response.json()["agent_run"]
    assert agent_run["intent"] == "SUGGEST_RENAME"
    assert agent_run["operation_plan_id"]
    assert source_path.exists()

    plan_response = client.get(
        f"/api/operations/plans/{agent_run['operation_plan_id']}",
        headers=_auth_header(token),
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["status"] == "WAITING_CONFIRMATION"
    assert plan["items"][0]["after"]["filename"] == (
        "2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.txt"
    )

    confirm_response = client.post(
        f"/api/operations/plans/{plan['id']}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    confirmed = confirm_response.json()
    assert confirmed["status"] == "EXECUTED"
    assert confirmed["changeset_id"]
    renamed_path = source_dir / "2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.txt"
    assert renamed_path.exists()
    assert not source_path.exists()

    db = SessionLocal()
    try:
        managed_file = db.query(ManagedFile).one()
        assert managed_file.relative_path == renamed_path.relative_to(managed_root).as_posix()
        operation_plan = db.get(OperationPlan, plan["id"])
        assert operation_plan is not None
        assert operation_plan.status == "EXECUTED"
        change_item = db.query(ChangeItem).filter(ChangeItem.change_type == "FILENAME_CHANGED").one()
        assert change_item.before_value_json["filename"] == "扫描件.txt"
        assert change_item.after_value_json["filename"] == renamed_path.name
    finally:
        db.close()
        clear_overrides()


def test_needs_review_item_is_skipped_from_operation_plan(monkeypatch, tmp_path):
    """缺少年份或标题的文件应保留回执但不进入可执行计划。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    (source_dir / "可重命名.txt").write_text(
        "2026年春季学生活动总结\n本学期组织了多项活动。",
        encoding="utf-8",
    )
    (source_dir / "待复核.txt").write_text("没有明确年份", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(managed_root))
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES_ALLOW_RENAME", "true")

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-review")
    response = client.post(
        "/api/conversations/rename-review-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )

    assert response.status_code == 200
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    assert invocation["output_json"]["ready_count"] == 1
    assert invocation["output_json"]["needs_review_count"] == 1
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    plan_response = client.get(f"/api/operations/plans/{plan_id}", headers=_auth_header(token))
    assert len(plan_response.json()["items"]) == 1
    clear_overrides()


def test_confirmed_rename_isolates_stale_file_failure(monkeypatch, tmp_path):
    """批次中一个源文件变化时应只失败该项，其他文件仍可完成改名。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    stable_path = source_dir / "稳定文件.txt"
    stale_path = source_dir / "变化文件.txt"
    stable_path.write_text("2026年春季学生活动总结\n本学期组织了多项活动。", encoding="utf-8")
    stale_path.write_text("2026年秋季资助工作报告\n本年度资助工作已经完成。", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(managed_root))
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES_ALLOW_RENAME", "true")

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-partial")
    response = client.post(
        "/api/conversations/rename-partial-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    stale_path.write_text("2026年秋季资助工作报告\n内容在确认前发生变化。", encoding="utf-8")

    confirm_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "PARTIAL"
    result = confirm_response.json()["result"]
    assert result["completed_count"] == 1
    assert result["failed_count"] == 1
    assert stale_path.exists()
    assert not stable_path.exists()
    db = SessionLocal()
    try:
        change_types = {item.change_type for item in db.query(ChangeItem).all()}
        assert {"FILENAME_CHANGED", "FILE_OPERATION_FAILED"}.issubset(change_types)
    finally:
        db.close()
        clear_overrides()
