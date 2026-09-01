"""校验文件夹上传携带的客户端相对路径元数据。"""

from __future__ import annotations

import re

from fastapi import HTTPException


def normalize_upload_relative_path(value: str | None, *, filename: str | None = None) -> str | None:
    """规范化浏览器文件夹选择产生的相对路径，禁止绝对路径和目录穿越。

    相对路径只作为批次展示和消息上下文元数据，绝不能直接作为服务端存储目标；即便如此，
    仍在进入数据库前执行路径边界校验，避免把不可信客户端字符串投影到回执中。
    """

    if value is None or not value.strip():
        return filename
    raw_value = value.strip().replace("\\", "/")
    if len(raw_value) > 1024:
        raise HTTPException(status_code=400, detail="文件夹相对路径超过 1024 个字符")
    if "\x00" in raw_value or raw_value.startswith("/") or re.match(r"^[A-Za-z]:", raw_value):
        raise HTTPException(status_code=400, detail="文件夹相对路径无效")
    parts = raw_value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="文件夹相对路径无效")
    normalized = "/".join(parts)
    if filename is not None and parts[-1] != filename:
        raise HTTPException(status_code=400, detail="文件夹相对路径与上传文件名不一致")
    return normalized
