"""容器启动前验证全功能 CPU 镜像依赖和模型清单。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil


REQUIRED_PACKAGES = {
    "fastapi": "fastapi",
    "docling": "docling",
    "paddleocr": "paddleocr",
    "paddlex": "paddlex",
    "sentence-transformers": "sentence_transformers",
    "neo4j": "neo4j",
}
REQUIRED_MODEL_COMPONENTS = {
    "docling",
    "paddleocr",
    "pp_structure_v3",
    "paddleocr_vl",
    "document_embedding",
}
REQUIRED_PACKAGE_VERSIONS = {
    "docling": "2.120.3",
    "huggingface-hub": "1.28.0",
    "paddlepaddle": "3.3.1",
    "paddleocr": "3.7.0",
    "paddlex": "3.7.2",
    "sentence-transformers": "5.7.0",
    "neo4j": "6.2.0",
    "neo4j-graphrag": "1.18.0",
}


def main() -> int:
    """失败时返回非零状态，使缺依赖容器不能接受生产任务。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", action="store_true")
    args = parser.parse_args()
    missing_packages = [
        display_name
        for display_name, import_name in REQUIRED_PACKAGES.items()
        if importlib.util.find_spec(import_name) is None
    ]
    if missing_packages:
        raise RuntimeError(f"镜像缺少 Python 依赖：{', '.join(missing_packages)}")
    if shutil.which("soffice") is None:
        raise RuntimeError("镜像缺少 LibreOffice soffice。")

    require_models = os.getenv("REQUIRE_PRELOADED_MODELS", "true").lower() == "true"
    model_root = Path(os.getenv("FILE_AGENT_MODEL_ROOT", "/opt/file-agent/models"))
    if require_models:
        manifest_path = model_root / "model-manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("完整 CPU 镜像缺少 model-manifest.json。")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        components = payload.get("components") or {}
        missing_models = sorted(REQUIRED_MODEL_COMPONENTS - set(components))
        if missing_models:
            raise RuntimeError(f"镜像缺少预下载模型：{', '.join(missing_models)}")
        failed_components = sorted(
            name
            for name in REQUIRED_MODEL_COMPONENTS
            if (components.get(name) or {}).get("status") != "ready"
        )
        if failed_components:
            raise RuntimeError(f"模型组件清单状态无效：{', '.join(failed_components)}")
        content = payload.get("content") or {}
        digest = str(content.get("sha256") or "")
        if len(digest) != 64 or int(content.get("file_count") or 0) <= 0:
            raise RuntimeError("模型清单缺少有效的内容 SHA-256 或文件数量。")
        package_versions = payload.get("package_versions") or {}
        version_mismatches = {
            name: {"expected": expected, "actual": package_versions.get(name)}
            for name, expected in REQUIRED_PACKAGE_VERSIONS.items()
            if package_versions.get(name) != expected
        }
        if version_mismatches:
            raise RuntimeError(f"模型镜像依赖版本不匹配：{version_mismatches}")
        for required_path in (
            model_root / "docling",
            model_root / "paddlex" / "official_models",
            model_root / "document-embedding",
        ):
            if not required_path.is_dir():
                raise RuntimeError(f"模型目录不存在：{required_path}")

    if args.managed_root:
        root_path = Path(os.getenv("MANAGED_ROOT_WORKDATA", "/managed/workdata"))
        if not root_path.is_dir():
            raise RuntimeError(f"受管目录挂载不存在：{root_path}")
    print("runtime-verification=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
