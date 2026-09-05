"""受管源文件扫描过滤与材料包路径保留策略测试。"""

from pathlib import PurePosixPath

from app.modules.managed_files.source_path_policy import (
    managed_source_container_path,
    should_ignore_managed_source,
)


def test_recruitment_package_strips_collection_anchor_and_preserves_inner_path():
    """外来应聘不重复进入 taxonomy，但年度、人员和材料类型必须保留。"""

    assert managed_source_container_path(
        "外来应聘/2026/博士/张三/证明材料/学历证书.pdf"
    ) == PurePosixPath("2026/博士/张三/证明材料")


def test_direct_recruitment_root_preserves_entire_relative_parent():
    """受管根直接挂载外来应聘目录时，相对路径已经不含集合锚点。"""

    assert managed_source_container_path(
        "2014应聘人员/张三/照片/IMG_0198.JPG",
        root_container_name="外来应聘",
    ) == PurePosixPath("2014应聘人员/张三/照片")


def test_department_archive_is_flattened_after_taxonomy_placement():
    """普通旧部门和年份目录只保留为来源元数据，不套入工作副本路径。"""

    assert managed_source_container_path(
        "人事处/职称评定/2025/通知.docx"
    ) == PurePosixPath()


def test_uploaded_archive_never_copies_internal_upload_path():
    """上传归档的内部 uploads 路径不得进入用户可见的 taxonomy 目录。"""

    assert managed_source_container_path(
        "uploads/2026/09/个人简历.pdf",
        is_uploaded_archive=True,
    ) == PurePosixPath()


def test_nested_project_keeps_project_name_but_strips_department_name():
    """审计等具体项目保留项目锚点和包内结构，不重复旧部门目录。"""

    assert managed_source_container_path(
        "审计处/2025李军怀（2022-2024）/凭证/目录.xls"
    ) == PurePosixPath("2025李军怀（2022-2024）/凭证")


def test_nested_project_is_preserved_when_department_is_managed_root():
    """部门本身作为受管根时，多级材料包规则仍应保留项目目录。"""

    assert managed_source_container_path(
        "院庆工作/照片/合影.jpg",
        root_container_name="办公",
    ) == PurePosixPath("院庆工作/照片")


def test_path_policy_rejects_traversal_and_ignores_known_noise():
    """异常路径不参与保留，已确认非业务内容必须被扫描过滤。"""

    assert managed_source_container_path("../外来应聘/张三/简历.pdf") == PurePosixPath()
    assert should_ignore_managed_source("信息化处/install/data/setup.exe") is True
    assert should_ignore_managed_source("外来应聘/2025/张三/driver/device.dll") is True
    assert should_ignore_managed_source("人事处/Thumbs.db") is True
    assert should_ignore_managed_source("test1/测试文件.pdf") is True
    assert should_ignore_managed_source("外来应聘/2025/张三/个人简历.pdf") is False


def test_workdata_filters_do_not_affect_system_upload_archive_root():
    """其他 ManagedRoot 中的 uploads 路径必须继续被扫描和修复。"""

    assert (
        should_ignore_managed_source(
            "uploads/2026/09/version/document.pdf",
            root_container_name="managed-originals",
        )
        is False
    )
