"""在 Docker 构建阶段下载、校验并登记全功能 CPU 部署所需模型。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable, Iterable


MODEL_ROOT = Path(os.getenv("FILE_AGENT_MODEL_ROOT", "/opt/file-agent/models"))
MANIFEST_PATH = MODEL_ROOT / "model-manifest.json"
DOWNLOAD_CACHE_ROOT = Path(
    os.getenv("MODEL_DOWNLOAD_CACHE_ROOT", "/var/cache/file-agent-model-downloads")
)
LOCAL_CACHE_ROOT = Path(
    os.getenv("LOCAL_MODEL_CACHE_IMPORT_ROOT", "/mnt/local-model-cache")
)
DOCLING_GIT_BASE = os.getenv("DOCLING_MODEL_GIT_BASE", "https://hf-mirror.com").rstrip("/")

# revision 使用已核对的 commit SHA，不能用 main 漂移生产镜像内容。
DOCLING_REPOSITORIES = (
    (
        "docling-project/docling-layout-heron",
        "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
        "docling-project--docling-layout-heron",
    ),
    (
        "docling-project/docling-layout-heron-onnx",
        "40bde044036bb181c130ddf6c51792187268748f",
        "docling-project--docling-layout-heron-onnx",
    ),
    (
        "docling-project/docling-models",
        "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
        "docling-project--docling-models",
    ),
    (
        "docling-project/DocumentFigureClassifier-v2.5",
        "f859dfbff5c9916cd996942d4b0db7fa25808220",
        "docling-project--DocumentFigureClassifier-v2.5",
    ),
    (
        "docling-project/CodeFormulaV2",
        "ecedbe111d15c2dc60bfd4a823cbe80127b58af4",
        "docling-project--CodeFormulaV2",
    ),
)

PADDLEX_ALLOWED_MODEL_DIRS = {
    # PaddleX 3.7.2 的 PP-StructureV3 实际把图表模型固化到该目录名；
    # 使用旧名称会在模型已经下载后仍被完整性校验误判为缺失。
    "PP-Chart2Table_safetensors",
    "PP-DocBlockLayout",
    "PP-DocLayout_plus-L",
    "PP-DocLayoutV3",
    "PP-FormulaNet_plus-L",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_table_cls",
    "PP-LCNet_x1_0_textline_ori",
    "PP-OCRv4_server_seal_det",
    "PP-OCRv5_server_det",
    "PP-OCRv5_server_rec",
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
    "PaddleOCR-VL-1.6-0.9B",
    "RT-DETR-L_wired_table_cell_det",
    "RT-DETR-L_wireless_table_cell_det",
    "SLANeXt_wired",
    "SLANet_plus",
    "UVDoc",
}

PACKAGE_VERSION_LOCKS = {
    "docling": "2.120.3",
    "huggingface-hub": "1.28.0",
    "paddlepaddle": "3.3.1",
    "paddleocr": "3.7.0",
    "paddlex": "3.7.2",
    "sentence-transformers": "5.7.0",
    "neo4j": "6.2.0",
    "neo4j-graphrag": "1.18.0",
}


def _package_version(name: str) -> str:
    """返回构建镜像中已安装包的版本。"""

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _read_manifest() -> dict[str, object]:
    """读取现有模型清单或创建空清单。"""

    if not MANIFEST_PATH.is_file():
        return {"schema_version": 2, "components": {}}
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    return payload


def _write_manifest(payload: dict[str, object]) -> None:
    """原子写入模型清单。"""

    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = MANIFEST_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(MANIFEST_PATH)


def _record(component: str, facts: dict[str, object]) -> None:
    """幂等更新模型组件清单。"""

    payload = _read_manifest()
    components = payload.setdefault("components", {})
    if not isinstance(components, dict):
        raise RuntimeError("模型清单 components 结构无效。")
    components[component] = {"status": "ready", **facts}
    _write_manifest(payload)


def _record_cache_import(imported: list[str]) -> None:
    """记录本次只读本地缓存白名单导入结果。"""

    payload = _read_manifest()
    payload["local_cache_import"] = {
        "status": "used" if imported else "empty",
        "items": sorted(imported),
    }
    _write_manifest(payload)


def _copy_tree(source: Path, target: Path) -> None:
    """合并复制已验证目录，不删除目标中的其他模型。"""

    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def import_local_cache() -> None:
    """只导入声明白名单内的 PaddleX 和 embedding 本地缓存。"""

    imported: list[str] = []
    if not LOCAL_CACHE_ROOT.is_dir():
        _record_cache_import(imported)
        return

    paddle_source = LOCAL_CACHE_ROOT / "paddlex" / "official_models"
    paddle_target = MODEL_ROOT / "paddlex" / "official_models"
    for model_name in sorted(PADDLEX_ALLOWED_MODEL_DIRS):
        source = paddle_source / model_name
        if source.is_dir():
            _copy_tree(source, paddle_target / model_name)
            imported.append(f"paddlex/{model_name}")

    embedding_cache_name = (
        "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
    )
    hf_source = LOCAL_CACHE_ROOT / "huggingface" / "hub" / embedding_cache_name
    if hf_source.is_dir():
        _copy_tree(
            hf_source,
            MODEL_ROOT / "huggingface" / "hub" / embedding_cache_name,
        )
        imported.append(f"huggingface/{embedding_cache_name}")

    direct_embedding = LOCAL_CACHE_ROOT / "document-embedding"
    if direct_embedding.is_dir():
        _copy_tree(direct_embedding, MODEL_ROOT / "document-embedding")
        imported.append("document-embedding")
    _record_cache_import(imported)


def _run_with_retry(command: list[str], *, cwd: Path | None = None, attempts: int = 3) -> None:
    """对模型网络下载执行有限重试并保留原始错误日志。"""

    for attempt in range(1, attempts + 1):
        completed = subprocess.run(command, cwd=cwd, check=False)
        if completed.returncode == 0:
            return
        if attempt == attempts:
            raise subprocess.CalledProcessError(completed.returncode, command)
        time.sleep(min(5 * attempt, 15))


def _contains_lfs_pointer(root: Path) -> list[str]:
    """返回仍是 Git LFS 指针而非真实模型字节的文件。"""

    pointers: list[str] = []
    signature = b"version https://git-lfs.github.com/spec/v1"
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            with path.open("rb") as handle:
                prefix = handle.read(200)
            if prefix.startswith(signature):
                pointers.append(path.relative_to(root).as_posix())
        except OSError as exc:
            raise RuntimeError(f"模型文件无法读取：{path}") from exc
    return pointers


def _validate_model_tree(root: Path, *, label: str) -> dict[str, int]:
    """验证模型目录非空、没有 LFS 指针，并返回规模事实。"""

    if not root.is_dir():
        raise RuntimeError(f"{label} 模型目录不存在：{root}")
    pointers = _contains_lfs_pointer(root)
    if pointers:
        raise RuntimeError(f"{label} 仍包含 Git LFS 指针：{', '.join(pointers[:5])}")
    files = [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
    total_bytes = sum(path.stat().st_size for path in files)
    if not files or total_bytes < 1024:
        raise RuntimeError(f"{label} 模型目录为空或内容不完整。")
    return {"file_count": len(files), "total_bytes": total_bytes}


def _checkout_docling_repository(*, repo_id: str, revision: str, target: Path) -> None:
    """通过国内镜像 Git LFS 固定提交下载一个 Docling 模型仓库。"""

    safe_name = repo_id.replace("/", "--")
    checkout = DOWNLOAD_CACHE_ROOT / "docling-git" / safe_name
    checkout.mkdir(parents=True, exist_ok=True)
    git_dir = checkout / ".git"
    if not git_dir.is_dir():
        _run_with_retry(["git", "init", str(checkout)])
        _run_with_retry(["git", "-C", str(checkout), "lfs", "install", "--local"])
        _run_with_retry(
            [
                "git",
                "-C",
                str(checkout),
                "remote",
                "add",
                "origin",
                f"{DOCLING_GIT_BASE}/{repo_id}.git",
            ]
        )
    else:
        _run_with_retry(
            [
                "git",
                "-C",
                str(checkout),
                "remote",
                "set-url",
                "origin",
                f"{DOCLING_GIT_BASE}/{repo_id}.git",
            ]
        )
    env = os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    # fetch/checkout 需要显式传 env，因此保留独立循环。
    for command in (
        ["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", revision],
        ["git", "-C", str(checkout), "checkout", "--force", "--detach", "FETCH_HEAD"],
        ["git", "-C", str(checkout), "lfs", "fetch", "origin", revision],
        ["git", "-C", str(checkout), "lfs", "checkout"],
    ):
        for attempt in range(1, 4):
            completed = subprocess.run(command, env=env, check=False)
            if completed.returncode == 0:
                break
            if attempt == 3:
                raise subprocess.CalledProcessError(completed.returncode, command)
            time.sleep(min(5 * attempt, 15))
    facts = _validate_model_tree(checkout, label=repo_id)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(checkout, target, ignore=shutil.ignore_patterns(".git"))
    _validate_model_tree(target, label=repo_id)
    print(f"docling-git-lfs=ok repo={repo_id} revision={revision} bytes={facts['total_bytes']}")


def preload_docling() -> None:
    """通过 Git LFS 固定提交下载 Docling，并从 ModelScope 下载 RapidOCR。"""

    target = MODEL_ROOT / "docling"
    target.mkdir(parents=True, exist_ok=True)
    repository_facts: list[dict[str, object]] = []
    for repo_id, revision, folder_name in DOCLING_REPOSITORIES:
        repository_target = target / folder_name
        try:
            facts = _validate_model_tree(repository_target, label=repo_id)
        except RuntimeError:
            _checkout_docling_repository(
                repo_id=repo_id,
                revision=revision,
                target=repository_target,
            )
            facts = _validate_model_tree(repository_target, label=repo_id)
        repository_facts.append(
            {"repo_id": repo_id, "revision": revision, "folder": folder_name, **facts}
        )

    # Docling 2.120.3 的默认 OCR 模型来自国内 ModelScope，不经过 Hugging Face HEAD API。
    rapidocr_target = target / "RapidOcr"
    if not rapidocr_target.is_dir() or not any(rapidocr_target.rglob("*")):
        _run_with_retry(
            [
                "docling-tools",
                "models",
                "download",
                "rapidocr",
                "--output-dir",
                str(target),
                "--quiet",
            ]
        )
    rapidocr_facts = _validate_model_tree(rapidocr_target, label="Docling RapidOCR")
    _record(
        "docling",
        {
            "version": _package_version("docling"),
            "relative_path": "docling",
            "download_transport": "git-lfs+modelscope",
            "repositories": repository_facts,
            "rapidocr": rapidocr_facts,
        },
    )


def _require_paddlex_models(names: Iterable[str]) -> dict[str, int]:
    """确认生产 Pipeline 初始化后所有声明模型都已写入缓存。"""

    root = MODEL_ROOT / "paddlex" / "official_models"
    missing = sorted(name for name in names if not (root / name).is_dir())
    if missing:
        raise RuntimeError(f"PaddleX 模型缓存不完整：{', '.join(missing)}")
    return _validate_model_tree(root, label="PaddleX")


def preload_paddleocr() -> None:
    """初始化项目实际使用的中文 PaddleOCR CPU Pipeline。"""

    from app.modules.ocr.service import PaddleOcrProvider

    provider = PaddleOcrProvider(model_source=os.getenv("PADDLE_PDX_MODEL_SOURCE", "BOS"))
    pipeline = provider._load_ocr()  # noqa: SLF001 - 构建期必须复用生产初始化入口。
    close = getattr(pipeline, "close", None)
    if callable(close):
        close()
    facts = _require_paddlex_models(
        {"PP-LCNet_x1_0_textline_ori", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"}
    )
    _record(
        "paddleocr",
        {
            "version": _package_version("paddleocr"),
            "detection_model": "PP-OCRv6_medium_det",
            "recognition_model": "PP-OCRv6_medium_rec",
            **facts,
        },
    )


def preload_pp_structure() -> None:
    """初始化启用全部子能力的 PP-StructureV3 CPU Pipeline。"""

    from app.modules.structured_extraction.pp_structure_provider import _load_pipeline

    pipeline = _load_pipeline(
        device="cpu",
        pipeline_config="PP-StructureV3",
        model_source=os.getenv("PADDLE_PDX_MODEL_SOURCE", "BOS"),
        use_doc_preprocessor=True,
        use_table_recognition=True,
        use_formula_recognition=True,
        use_chart_recognition=True,
        use_seal_recognition=True,
        use_region_detection=True,
        text_detection_model="PP-OCRv6_medium_det",
        text_recognition_model="PP-OCRv6_medium_rec",
    )
    close = getattr(pipeline, "close", None)
    if callable(close):
        close()
    required = PADDLEX_ALLOWED_MODEL_DIRS - {
        "PP-DocLayoutV3",
        "PaddleOCR-VL-1.6-0.9B",
    }
    facts = _require_paddlex_models(required)
    _record(
        "pp_structure_v3",
        {
            "version": _package_version("paddlex"),
            "pipeline": "PP-StructureV3",
            "all_subfeatures": True,
            **facts,
        },
    )


def preload_paddleocr_vl() -> None:
    """初始化 CPU Autonomous Loop 使用的 PaddleOCR-VL 0.9B Pipeline。"""

    from app.modules.structured_extraction.vision_provider import _load_paddleocr_vl_pipeline

    model_name = os.getenv("PADDLEOCR_VL_MODEL_NAME", "PaddleOCR-VL-1.6-0.9B")
    pipeline_version = os.getenv("PADDLEOCR_VL_PIPELINE_VERSION", "v1.6")
    pipeline = _load_paddleocr_vl_pipeline(
        pipeline_version=pipeline_version,
        model_name=model_name,
        backend=os.getenv("PADDLEOCR_VL_BACKEND", "native"),
        device="cpu",
        model_source=os.getenv("PADDLE_PDX_MODEL_SOURCE", "BOS"),
    )
    close = getattr(pipeline, "close", None)
    if callable(close):
        close()
    facts = _require_paddlex_models(
        {"PP-DocLayoutV3", "PP-LCNet_x1_0_doc_ori", "PaddleOCR-VL-1.6-0.9B", "UVDoc"}
    )
    _record(
        "paddleocr_vl",
        {
            "version": _package_version("paddleocr"),
            "pipeline_version": pipeline_version,
            "model_name": model_name,
            **facts,
        },
    )


def preload_embedding() -> None:
    """复用本地白名单缓存或下载并保存 384 维多语言文档向量模型。"""

    from sentence_transformers import SentenceTransformer

    model_name = os.getenv(
        "GRAPH_EMBEDDING_BUILD_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    target = MODEL_ROOT / "document-embedding"
    load_source = str(target) if (target / "modules.json").is_file() else model_name
    model = SentenceTransformer(load_source)
    dimension = int(model.get_sentence_embedding_dimension() or 0)
    if dimension != 384:
        raise RuntimeError(f"部署 embedding 模型维度必须为 384，实际为 {dimension}。")
    target.mkdir(parents=True, exist_ok=True)
    model.save(str(target))
    facts = _validate_model_tree(target, label="document embedding")
    _record(
        "document_embedding",
        {
            "version": _package_version("sentence-transformers"),
            "model_name": model_name,
            "dimension": dimension,
            "relative_path": "document-embedding",
            **facts,
        },
    )


def _aggregate_model_digest() -> tuple[str, int, int]:
    """计算模型文件集合的稳定 SHA-256，不包含清单自身。"""

    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(MODEL_ROOT.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path == MANIFEST_PATH or path.suffix == ".tmp":
            continue
        relative = path.relative_to(MODEL_ROOT).as_posix().encode("utf-8")
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(block)
                total_bytes += len(block)
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_digest.digest())
        file_count += 1
    return digest.hexdigest(), file_count, total_bytes


def finalize_manifest() -> None:
    """确认完整镜像具备所有模型、固定包版本和内容摘要。"""

    required = {
        "docling",
        "paddleocr",
        "pp_structure_v3",
        "paddleocr_vl",
        "document_embedding",
    }
    payload = _read_manifest()
    components = payload.get("components") or {}
    missing = sorted(required - set(components))
    if missing:
        raise RuntimeError(f"模型预下载不完整：{', '.join(missing)}")
    actual_versions = {name: _package_version(name) for name in PACKAGE_VERSION_LOCKS}
    mismatches = {
        name: {"expected": expected, "actual": actual_versions[name]}
        for name, expected in PACKAGE_VERSION_LOCKS.items()
        if actual_versions[name] != expected
    }
    if mismatches:
        raise RuntimeError(f"生产依赖版本不匹配：{json.dumps(mismatches, ensure_ascii=False)}")
    pointers = _contains_lfs_pointer(MODEL_ROOT)
    if pointers:
        raise RuntimeError(f"模型根目录仍包含 Git LFS 指针：{', '.join(pointers[:5])}")
    content_sha256, file_count, total_bytes = _aggregate_model_digest()
    payload["profile"] = "windows11-full-cpu"
    payload["hardware_baseline"] = {"cpu_cores": 6, "memory_gb": 32}
    payload["package_versions"] = actual_versions
    payload["content"] = {
        "sha256": content_sha256,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    _write_manifest(payload)
    print(f"model-manifest=ok sha256={content_sha256} files={file_count} bytes={total_bytes}")


COMPONENTS: dict[str, Callable[[], None]] = {
    "import-cache": import_local_cache,
    "docling": preload_docling,
    "paddleocr": preload_paddleocr,
    "pp-structure": preload_pp_structure,
    "paddleocr-vl": preload_paddleocr_vl,
    "embedding": preload_embedding,
    "finalize": finalize_manifest,
}


def main() -> int:
    """执行一个独立预下载阶段，避免重量级模型同时驻留构建内存。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=sorted(COMPONENTS))
    args = parser.parse_args()
    COMPONENTS[args.component]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
