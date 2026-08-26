"""完整 CPU 镜像模型预装器的离线与白名单测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]
PRELOADER_PATH = PROJECT_ROOT / "deploy" / "scripts" / "preload_models.py"


def _load_preloader(monkeypatch, tmp_path):
    """使用隔离模型根加载部署脚本模块。"""

    model_root = tmp_path / "models"
    local_cache = tmp_path / "local-cache"
    download_cache = tmp_path / "download-cache"
    monkeypatch.setenv("FILE_AGENT_MODEL_ROOT", str(model_root))
    monkeypatch.setenv("LOCAL_MODEL_CACHE_IMPORT_ROOT", str(local_cache))
    monkeypatch.setenv("MODEL_DOWNLOAD_CACHE_ROOT", str(download_cache))
    spec = importlib.util.spec_from_file_location("deploy_preload_models_test", PRELOADER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, model_root, local_cache


def test_local_cache_import_only_copies_approved_models(monkeypatch, tmp_path):
    """命名上下文中的非白名单文件不得进入生产模型目录。"""

    module, model_root, local_cache = _load_preloader(monkeypatch, tmp_path)
    approved = local_cache / "paddlex" / "official_models" / "PP-OCRv6_medium_det"
    rejected = local_cache / "paddlex" / "official_models" / "unapproved-private-model"
    embedding = (
        local_cache
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
    )
    approved.mkdir(parents=True)
    rejected.mkdir(parents=True)
    embedding.mkdir(parents=True)
    (approved / "model.pdparams").write_bytes(b"approved-model")
    (rejected / "secret.bin").write_bytes(b"must-not-copy")
    (embedding / "refs").write_text("main", encoding="utf-8")

    module.import_local_cache()

    assert (
        model_root
        / "paddlex"
        / "official_models"
        / "PP-OCRv6_medium_det"
        / "model.pdparams"
    ).is_file()
    assert not (
        model_root / "paddlex" / "official_models" / "unapproved-private-model"
    ).exists()
    assert (
        model_root
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
        / "refs"
    ).is_file()


def test_model_tree_rejects_unresolved_git_lfs_pointer(monkeypatch, tmp_path):
    """Git checkout 成功但 LFS 字节未下载时必须阻止镜像完成。"""

    module, _, _ = _load_preloader(monkeypatch, tmp_path)
    model_tree = tmp_path / "docling-model"
    model_tree.mkdir()
    (model_tree / "weights.safetensors").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:0123456789abcdef\n"
        "size 123456\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Git LFS 指针"):
        module._validate_model_tree(model_tree, label="test-docling")


def test_docling_repositories_and_runtime_versions_are_commit_locked(monkeypatch, tmp_path):
    """Docling 仓库和重量依赖必须使用不可漂移的完整版本。"""

    module, _, _ = _load_preloader(monkeypatch, tmp_path)
    assert len(module.DOCLING_REPOSITORIES) == 5
    assert all(len(revision) == 40 for _, revision, _ in module.DOCLING_REPOSITORIES)
    assert module.PACKAGE_VERSION_LOCKS == {
        "docling": "2.120.3",
        "huggingface-hub": "1.28.0",
        "paddlepaddle": "3.3.1",
        "paddleocr": "3.7.0",
        "paddlex": "3.7.2",
        "sentence-transformers": "5.7.0",
        "neo4j": "6.2.0",
        "neo4j-graphrag": "1.18.0",
    }
