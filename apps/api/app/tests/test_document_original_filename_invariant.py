"""Document 原始文件名不可变约束测试。"""

import pytest

from app.db.models import Document


def test_document_original_filename_cannot_be_reassigned() -> None:
    """工作副本改名不得污染上传时保存的原始名称。"""

    document = Document(
        user_id="user-1",
        original_filename="上传原名.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1,
        sha256="a" * 64,
    )

    with pytest.raises(ValueError, match="original_filename is immutable"):
        document.original_filename = "标准化新名称.docx"

    assert document.original_filename == "上传原名.docx"
