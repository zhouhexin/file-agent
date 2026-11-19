"""Build a code-only delivery ZIP and its SHA-256 sidecar."""

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT_FILES = {"Dockerfile", "compose.yml", "compose.fileagent-server.yml", "requirements.txt",
              "README.md", ".env.example", ".dockerignore", ".gitignore"}


def build_release(root: Path, output: Path, version: str):
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", version):
        raise ValueError("Version must use lowercase letters, numbers, dots, underscores or hyphens.")
    files = {}
    for name in sorted(ROOT_FILES):
        if (root / name).is_symlink():
            raise ValueError("Release sources must not contain symbolic links.")
        files[name] = (root / name).read_bytes()
    for folder, suffixes in [("app", {".py", ".html", ".css", ".js"}),
                             ("scripts", {".py", ".ps1"})]:
        if (root / folder).is_symlink():
            raise ValueError("Release directories must not be symbolic links.")
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                if path.is_symlink():
                    raise ValueError("Release sources must not contain symbolic links.")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    manifest = {"format": 1, "service": "teacher-funding-portal", "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(), "files": [
                    {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, data in sorted(files.items())]}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"teacher-funding-portal-{version}.zip"
    if archive.exists():
        raise FileExistsError(f"Already exists: {archive}; use a new version.")
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
            for name, data in sorted(files.items()):
                package.writestr(name, data)
            package.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        with zipfile.ZipFile(temporary) as package:
            if package.testzip() is not None:
                raise ValueError("Release ZIP integrity check failed.")
        temporary.rename(archive)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "\n", encoding="ascii")
    return {"package": str(archive.resolve()), "sha256_file": str(archive.with_suffix('.zip.sha256').resolve()),
            "version": version, "files": len(files), "sha256": digest}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--output", type=Path, default=root.parents[1] / "release" / "teacher-funding-portal")
    args = parser.parse_args()
    print(json.dumps(build_release(root, args.output, args.version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
