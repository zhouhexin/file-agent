"""按当前清单打包 WorkBuddy 市场；排除缓存，校验版本与包内必需入口。"""

from pathlib import Path
import hashlib
import json
import shutil
import zipfile


def main():
    """只从两个固定插件目录生成升级包，不读取 MCP 凭证或宿主目录。"""
    root = Path(__file__).resolve().parent
    plugin = root / "file-agent-plugin"
    manifest = json.loads((plugin / ".codebuddy-plugin/plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    marketplace = json.loads((root / ".codebuddy-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert any(item.get("version") == version for item in marketplace["plugins"]), "市场与插件版本不一致"
    output = root / f"file-agent-marketplace-{version}.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_root in (root / ".codebuddy-plugin", plugin):
            for source in sorted(source_root.rglob("*")):
                if not source.is_file() or source.is_symlink():
                    continue
                if "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
                    continue
                archive.write(source, source.relative_to(root).as_posix())
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert ".codebuddy-plugin/marketplace.json" in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-structured-dataset/SKILL.md" not in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-structured-dataset/references/task-payload.md" not in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-a-i-scoped-excel/SKILL.md" in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-a-i-scoped-excel/references/a-i-source-map.md" in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-full-professor-excel/SKILL.md" not in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-sealed-item-extraction/SKILL.md" not in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-sealed-item-extraction/references/column-source-map.md" not in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-excel-grid-archive/SKILL.md" in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-excel-grid-archive/scripts/grid_to_payload.py" in archive.namelist()
        assert "file-agent-plugin/skills/file-agent-manual-excel-revision/SKILL.md" in archive.namelist()
        for entry in archive.namelist():
            assert archive.read(entry) == (root / entry).read_bytes(), entry
    shutil.copyfile(output, root / "file-agent-marketplace.zip")
    print(f"{output.name}\nSHA256={hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
