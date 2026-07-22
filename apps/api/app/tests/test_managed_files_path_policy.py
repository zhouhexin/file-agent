"""受管目录 PathPolicy 测试。

P0 只允许 root_key + relative_path，不允许用户或 LLM 传入宿主机绝对路径。
"""

import errno
import os
from pathlib import Path

import pytest

from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path


@pytest.mark.parametrize(
    "relative_path",
    [
        "../secret.pdf",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "2026/\x00/a.pdf",
    ],
)
def test_path_policy_rejects_unsafe_relative_paths(tmp_path: Path, relative_path: str):
    """危险路径必须被拒绝，避免从受管目录逃逸。"""

    with pytest.raises(PathPolicyError):
        resolve_managed_relative_path(root_path=tmp_path, relative_path=relative_path)


def test_path_policy_allows_normal_relative_path(tmp_path: Path):
    """普通相对路径应解析到受管根目录之内。"""

    resolved = resolve_managed_relative_path(
        root_path=tmp_path,
        relative_path="2026/inbox/a.pdf",
        must_exist=False,
    )

    assert resolved == tmp_path / "2026" / "inbox" / "a.pdf"


def test_path_policy_rejects_symlink_escape(tmp_path: Path):
    """符号链接指向受管根目录外部时必须拒绝。"""

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (outside / "secret.pdf").write_text("secret", encoding="utf-8")
        try:
            (tmp_path / "link.pdf").symlink_to(outside / "secret.pdf")
        except OSError as exc:
            # 未启用 Windows 开发者模式且当前进程无管理员权限时，系统会拒绝创建
            # 测试前置符号链接；这不是 PathPolicy 业务失败，必须明确记录环境跳过。
            windows_privilege_error = os.name == "nt" and getattr(exc, "winerror", None) == 1314
            permission_error = exc.errno in {errno.EPERM, errno.EACCES}
            if windows_privilege_error or permission_error:
                pytest.skip("当前 Windows 环境无创建符号链接权限，跳过真实 symlink 前置条件。")
            raise

        with pytest.raises(PathPolicyError):
            resolve_managed_relative_path(root_path=tmp_path, relative_path="link.pdf")
    finally:
        (outside / "secret.pdf").unlink(missing_ok=True)
        outside.rmdir()
