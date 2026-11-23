"""Harness-local Windows long paths, without changing registry or global pathlib."""

import os
import pathlib
import shutil
import stat


def _logical(value):
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


if os.name == "nt":
    class Path(pathlib.WindowsPath):
        """Keep portable string identities; use extended paths only for OS calls."""

        def __new__(cls, *args):
            return super().__new__(cls, *(_logical(os.fspath(p)) for p in args))

        def __fspath__(self):
            value = str(self)
            if len(value) < 240 or value.startswith("\\\\?\\"):
                return value
            value = os.path.abspath(value)
            return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value

        def resolve(self, strict=False):
            return Path(super().resolve(strict=strict))
else:
    Path = pathlib.Path


def tree(root):
    """Reject symlinks and Windows reparse points before entering a directory."""
    root = Path(root)
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("SNAPSHOT_SYMLINK_BLOCKED")
    with os.scandir(root) as entries:
        children = sorted(entries, key=lambda e: e.name)
    for entry in children:
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("SNAPSHOT_SYMLINK_BLOCKED")
        path = root / entry.name
        yield path
        if stat.S_ISDIR(info.st_mode):
            yield from tree(path)


def copy_tree(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for path in tree(source):
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir()
        else:
            shutil.copy2(path, target)
