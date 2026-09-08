"""受管源文件的硬编码扫描与工作副本路径策略。

当前策略依据 ``E:/workdata`` 的实际目录结构固化。源文件的完整相对路径始终
保存在 ``ManagedFile.relative_path``；这里只决定哪些非业务文件不进入索引，
以及首次分类落位时需要保留哪一段材料包内部路径。
"""

from __future__ import annotations

from pathlib import PurePosixPath


# ``strip_parts`` 表示工作副本落位时从源相对路径开头移除多少级。
# 顶层集合目录（例如“外来应聘”）本身不重复进入 taxonomy 路径；具体项目
# 目录（例如某次审计）则只移除其上层部门名，保留项目名称和包内结构。
_PRESERVED_PACKAGE_RULES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("外来应聘",), 1),
    (("学院照片",), 1),
    (("专业认证",), 1),
    (("学院接待",), 1),
    (("学院搬家－曲江校区",), 0),
    (("曲江3期",), 0),
    (("2016年学科评估",), 0),
    (("教学评估",), 0),
    (("综合治理", "2021创建“平安校园”工作"), 1),
    (("综合治理", "2016创建平安校园"), 1),
    (("审计处", "2025李军怀（2022-2024）"), 1),
    (("审计处", "计算机学院审计专用"), 1),
    (("校财务处", "电子发票及承诺书"), 1),
    (("纪委+廉政工作", "2018年廉政风险防控制度建设"), 1),
    (("纪委+廉政工作", "四个专项治理201709"), 1),
    (("办公", "院庆工作"), 1),
    (("办公", "24年学院文化建设"), 1),
    (("办公", "2020新冠肺炎防控"), 1),
    (("宣传部", "西安理工大学视觉识别系统"), 1),
    # 人事处职称材料属于一个完整业务包；taxonomy 已表达“学校/人事师资/职称”，
    # 因而移除前两级锚点，仅保留年度、系列、评审类型、人员和包内结构。
    (("人事处", "职称评定"), 2),
)

_IGNORED_TOP_LEVEL_DIRECTORIES = frozenset({"test1", "uploads", "我的文档"})
_IGNORED_DIRECTORY_NAMES = frozenset({"install", "driver"})
_IGNORED_FILENAMES = frozenset({"thumbs.db", "desktop.ini", "debug.log"})
_IGNORED_EXTENSIONS = frozenset(
    {
        ".exe",
        ".msi",
        ".dll",
        ".sys",
        ".iso",
        ".cat",
        ".inf",
        ".da_",
        ".pp_",
        ".dl_",
        ".ch_",
    }
)
_HARDCODED_POLICY_ROOT_NAMES = frozenset(
    {
        "workdata",
        "外来应聘",
        "学院照片",
        "专业认证",
        "学院接待",
        "学院搬家－曲江校区",
        "曲江3期",
        "2016年学科评估",
        "教学评估",
        "综合治理",
        "审计处",
        "校财务处",
        "纪委+廉政工作",
        "办公",
        "宣传部",
        "信息化处",
    }
)


def managed_source_container_path(
    relative_path: str,
    *,
    root_container_name: str | None = None,
    is_uploaded_archive: bool = False,
) -> PurePosixPath:
    """返回首次分类落位时应保留的材料包父路径。

    普通部门归档默认扁平化。若受管根本身就是一个需要保留的顶层集合目录，
    由于集合锚点不再出现在 ``relative_path`` 中，保留完整父路径。
    """

    if is_uploaded_archive:
        return PurePosixPath()
    parts = _safe_relative_parts(relative_path)
    if not parts:
        return PurePosixPath()
    parent_parts = parts[:-1]
    if not parent_parts:
        return PurePosixPath()

    normalized_root_name = str(root_container_name or "").strip().casefold()
    for prefix, strip_parts in _PRESERVED_PACKAGE_RULES:
        if normalized_root_name == prefix[0].casefold():
            relative_prefix = prefix[1:]
            if not relative_prefix or _starts_with(parent_parts, relative_prefix):
                retained = parent_parts[max(strip_parts - 1, 0) :]
                return PurePosixPath(*retained) if retained else PurePosixPath()
        if _starts_with(parent_parts, prefix):
            retained = parent_parts[strip_parts:]
            return PurePosixPath(*retained) if retained else PurePosixPath()
    return PurePosixPath()


def should_ignore_managed_source(
    relative_path: str,
    *,
    root_container_name: str | None = None,
) -> bool:
    """判断文件是否属于已确认的安装包、测试目录或系统垃圾文件。"""

    parts = _safe_relative_parts(relative_path)
    if not parts:
        return True
    folded_parts = tuple(part.casefold() for part in parts)
    normalized_root_name = str(root_container_name or "").strip().casefold()
    if (
        normalized_root_name
        and normalized_root_name not in _HARDCODED_POLICY_ROOT_NAMES
    ):
        # 规则来自 E:/workdata 的真实目录，只能作用于该根或明确拆分出的业务
        # 子根。系统上传归档等其他 ManagedRoot 必须保持原有扫描语义。
        return False
    if normalized_root_name in _IGNORED_TOP_LEVEL_DIRECTORIES:
        return True
    if folded_parts[0] in _IGNORED_TOP_LEVEL_DIRECTORIES:
        return True
    if normalized_root_name in _IGNORED_DIRECTORY_NAMES:
        return True
    if any(part in _IGNORED_DIRECTORY_NAMES for part in folded_parts[:-1]):
        return True

    filename = folded_parts[-1]
    if filename in _IGNORED_FILENAMES or filename.startswith("~$"):
        return True
    return PurePosixPath(filename).suffix.casefold() in _IGNORED_EXTENSIONS


def _safe_relative_parts(relative_path: str) -> tuple[str, ...]:
    """规范化可信相对路径；异常输入按空路径处理。"""

    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return ()
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return ()
    return tuple(part for part in path.parts if part not in {"", "."})


def _starts_with(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """以 Windows 语义大小写不敏感地匹配目录前缀。"""

    if len(parts) < len(prefix):
        return False
    return tuple(part.casefold() for part in parts[: len(prefix)]) == tuple(
        part.casefold() for part in prefix
    )
