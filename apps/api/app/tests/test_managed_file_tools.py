"""受管文件 Agent Tool 测试。"""

from datetime import datetime, timezone

from app.db.models import ManagedFile, ManagedRoot
from app.modules.agent.tool_registry import ToolRegistry
from app.tests.helpers import clear_overrides, client_with_database


def test_managed_file_list_tool_returns_logical_paths_only():
    """managed-file-list Tool 只能返回逻辑路径和元数据，不能暴露容器路径。"""

    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path="/managed/student-affairs")
        db.add(root)
        db.flush()
        db.add(
            ManagedFile(
                root_id=root.id,
                relative_path="2026/a.pdf",
                filename="a.pdf",
                extension=".pdf",
                size_bytes=100,
                modified_at=datetime.now(timezone.utc),
                fingerprint="fp",
                status="ACTIVE",
            )
        )
        db.commit()

        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "student_affairs", "extension": "pdf"},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert result.output_json["files"][0]["relative_path"] == "2026/a.pdf"
        assert "container_path" not in result.output_json["files"][0]
    finally:
        db.close()
        clear_overrides()
