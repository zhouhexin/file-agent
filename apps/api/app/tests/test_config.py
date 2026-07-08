"""应用配置行为测试。"""

from __future__ import annotations

import pytest

from app.core import config


def _reset_settings_cache() -> None:
    """清理配置缓存，确保每个测试都重新读取环境变量。"""

    config.get_settings.cache_clear()


def test_settings_requires_database_url(monkeypatch, tmp_path):
    """未配置 DATABASE_URL 时，服务配置必须关闭式失败。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _reset_settings_cache()

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config.get_settings()


def test_settings_rejects_sqlite_database_url(monkeypatch, tmp_path):
    """正式服务配置不得继续使用 SQLite 连接。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///./storage/file_agent_dev.db")
    _reset_settings_cache()

    with pytest.raises(RuntimeError, match="PostgreSQL"):
        config.get_settings()


def test_settings_loads_dotenv_from_parent_directory(monkeypatch, tmp_path):
    """从子目录启动时也必须能读取项目根目录 `.env`。"""

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
                "AUTO_CREATE_TABLES=false",
            ],
        ),
        encoding="utf-8",
    )
    nested_dir = tmp_path / "apps" / "api"
    nested_dir.mkdir(parents=True)

    monkeypatch.chdir(nested_dir)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTO_CREATE_TABLES", raising=False)
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.database_url == "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent"
    assert settings.auto_create_tables is False


def test_dotenv_managed_root_overrides_stale_process_env(monkeypatch, tmp_path):
    """受管目录配置必须允许 `.env` 覆盖旧进程值，适配本地 reload 后的目录变更。"""

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
                "MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES=/new/root",
            ],
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES", "/old/root")

    config.load_dotenv_file()

    assert config.os.environ["MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES"] == "/new/root"


def test_settings_loads_classification_llm_options(monkeypatch, tmp_path):
    """分类 LLM 判定开关必须通过配置显式启用。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("LLM_CLASSIFICATION_MODE", "hybrid")
    monkeypatch.setenv("LLM_CLASSIFICATION_ALLOW_FREE_PATHS", "true")
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.llm_classification_mode == "hybrid"
    assert settings.llm_classification_allow_free_paths is True


def test_settings_defaults_paddleocr_model_source_to_baidu_bos(monkeypatch, tmp_path):
    """PaddleOCR 模型下载源默认必须使用百度 BOS，适配国内服务器部署。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.delenv("OCR_PADDLE_MODEL_SOURCE", raising=False)
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.ocr_paddle_model_source == "BOS"
