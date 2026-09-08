"""目标文件名冲突时的可读消歧候选测试。"""

from app.modules.file_rename.collision_naming import (
    readable_collision_filename_candidates,
)


def test_collision_prefers_semantic_filename_suffix() -> None:
    candidates = readable_collision_filename_candidates(
        standard_filename="2025_年工作任务计划表.xls",
        original_filename="附件2：2025年工作任务计划表-科研.xls",
        source_relative_path="办公/2025年/分管提交/附件2：2025年工作任务计划表-科研.xls",
    )

    assert candidates[0] == "2025_年工作任务计划表_科研.xls"


def test_collision_uses_original_filename_difference() -> None:
    candidates = readable_collision_filename_candidates(
        standard_filename="宣传教育.pdf",
        original_filename="宣传教育198期.pdf",
        source_relative_path="办公/中心组学习/宣传教育198期.pdf",
    )

    assert candidates[0] == "宣传教育_198期.pdf"


def test_collision_uses_nearest_meaningful_source_directory_last() -> None:
    candidates = readable_collision_filename_candidates(
        standard_filename="会议议程.docx",
        original_filename="会议议程.docx",
        source_relative_path=(
            "办公/学院承办会议/20260407碑林分局网络安全大队调研/会议议程.docx"
        ),
    )

    assert candidates == ["会议议程_20260407碑林分局网络安全大队调研.docx"]


def test_collision_without_readable_difference_stays_unresolved() -> None:
    candidates = readable_collision_filename_candidates(
        standard_filename="同名文件.docx",
        original_filename="同名文件.docx",
        source_relative_path="同名文件.docx",
    )

    assert candidates == []
