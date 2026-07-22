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


def test_settings_loads_legacy_office_conversion_options(monkeypatch, tmp_path):
    """旧版 Office 转换配置必须由统一 Settings 读取。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("LEGACY_OFFICE_CONVERSION_ENABLED", "false")
    monkeypatch.setenv("LIBREOFFICE_EXECUTABLE", "/opt/libreoffice/program/soffice")
    monkeypatch.setenv("LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("LEGACY_OFFICE_MAX_FILE_SIZE_MB", "64")
    monkeypatch.setenv("LEGACY_OFFICE_DERIVATIVE_DIR", "derived/legacy-office")
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.legacy_office_conversion_enabled is False
    assert settings.legacy_office_converter == "libreoffice"
    assert settings.libreoffice_executable == "/opt/libreoffice/program/soffice"
    assert settings.legacy_office_conversion_timeout_seconds == 45
    assert settings.legacy_office_max_file_size_mb == 64
    assert settings.legacy_office_derivative_dir == "derived/legacy-office"


def test_settings_defaults_document_index_to_cpu_lexical_mode(monkeypatch, tmp_path):
    """第三阶段默认不得启用 embedding 或要求 GPU。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    for name in (
        "RETRIEVAL_MODE",
        "CHINESE_TOKENIZER",
        "DOCUMENT_INDEX_MAX_CHARS",
        "DOCUMENT_INDEX_MAX_CHUNKS",
        "EMBEDDING_ENABLED",
        "EMBEDDING_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.retrieval_mode == "lexical"
    assert settings.chinese_tokenizer == "jieba"
    assert settings.document_index_max_chars == 50_000_000
    assert settings.document_index_max_chunks == 50_000
    assert settings.embedding_enabled is False
    assert settings.embedding_provider == "disabled"


@pytest.mark.parametrize("mode", ["hybrid", "native", "docling"])
def test_settings_loads_file_rename_parse_mode(monkeypatch, tmp_path, mode):
    """重命名解析模式必须接受三个受控配置值。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("FILE_RENAME_PARSE_MODE", mode)
    _reset_settings_cache()

    assert config.get_settings().file_rename_parse_mode == mode


def test_settings_defaults_unknown_file_rename_parse_mode_to_hybrid(monkeypatch, tmp_path):
    """未知解析模式必须回退 hybrid，不能进入重命名业务层。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("FILE_RENAME_PARSE_MODE", "unsupported")
    _reset_settings_cache()

    assert config.get_settings().file_rename_parse_mode == "hybrid"


def test_settings_loads_file_rename_llm_validation(monkeypatch, tmp_path):
    """重命名模型校验配置必须集中由 Settings 读取。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_MODE", "all")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_THRESHOLD", "0.72")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH", "8")
    config.get_settings.cache_clear()

    settings = config.get_settings()

    assert settings.file_rename_llm_validation_enabled is True
    assert settings.file_rename_llm_validation_mode == "all"
    assert settings.file_rename_llm_validation_threshold == 0.72
    assert settings.file_rename_llm_validation_timeout_seconds == 12
    assert settings.file_rename_llm_validation_max_items_per_batch == 8


def test_settings_defaults_graph_classification_to_disabled(monkeypatch, tmp_path):
    """图谱分类必须默认关闭，未部署 Neo4j 时不能影响后端启动。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    # 该测试验证“没有配置时”的默认值，必须隔离 IDE、Shell 和真实 .env 中所有被断言的图谱变量。
    for name in (
        "GRAPH_CLASSIFICATION_ENABLED",
        "GRAPH_CLASSIFICATION_MODE",
        "NEO4J_SYNC_ENABLED",
        "GRAPH_CLASSIFICATION_MAX_HOPS",
        "GRAPH_CLASSIFICATION_TOP_K",
        "GRAPH_EMBEDDING_ENABLED",
        "GRAPH_CLASSIFICATION_ROLLOUT_PERCENT",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.graph_classification_enabled is False
    assert settings.neo4j_sync_enabled is False
    assert settings.graph_classification_max_hops == 1
    assert settings.graph_classification_top_k == 8
    assert settings.graph_classification_mode == "off"
    assert settings.graph_embedding_enabled is False
    assert settings.graph_classification_rollout_percent == 10


def test_settings_loads_graph_classification_options(monkeypatch, tmp_path):
    """图谱连接和候选限制必须通过显式配置启用。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j.internal:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "file_agent")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "file_agent")
    monkeypatch.setenv("NEO4J_QUERY_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("NEO4J_SYNC_ENABLED", "true")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_MAX_HOPS", "2")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_TOP_K", "6")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_MODE", "shadow")
    monkeypatch.setenv("GRAPH_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("GRAPH_EMBEDDING_DIMENSION", "768")
    monkeypatch.setenv("GRAPH_VECTOR_TOP_K", "15")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ROLLOUT_PERCENT", "25")
    _reset_settings_cache()

    settings = config.get_settings()

    assert settings.graph_classification_enabled is True
    assert settings.neo4j_uri == "bolt://neo4j.internal:7687"
    assert settings.neo4j_username == "file_agent"
    assert settings.neo4j_password == "secret"
    assert settings.neo4j_database == "file_agent"
    assert settings.neo4j_query_timeout_seconds == 4
    assert settings.neo4j_sync_enabled is True
    assert settings.graph_classification_max_hops == 2
    assert settings.graph_classification_top_k == 6
    assert settings.graph_classification_mode == "shadow"
    assert settings.graph_embedding_enabled is True
    assert settings.graph_embedding_dimension == 768
    assert settings.graph_vector_top_k == 15
    assert settings.graph_classification_rollout_percent == 25
