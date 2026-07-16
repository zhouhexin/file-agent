"""应用配置。

配置集中在这里读取，避免业务模块直接访问环境变量；后续接入更多部署环境时只需要调整配置层。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


DEFAULT_JWT_SECRET_KEY = "file-agent-dev-secret"
DEFAULT_JWT_ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
DEFAULT_FILE_STORAGE_ROOT = "./storage/uploads"
DEFAULT_LLM_TIMEOUT_SECONDS = 180
DEFAULT_LOG_DIR = "./logs"
DEFAULT_LOG_RETENTION_DAYS = 7
DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD = 0.68
DEFAULT_OCR_PADDLE_MODEL_SOURCE = "BOS"
DEFAULT_DOCLING_FORMATS = ("pdf", "docx")
DEFAULT_FILE_RENAME_EXECUTOR = "native"
DEFAULT_FILE_RENAME_PARSE_MODE = "hybrid"
DEFAULT_FILE_RENAME_MAX_BATCH_SIZE = 20
DEFAULT_FILE_RENAME_EXECUTION_TIMEOUT_SECONDS = 60
DEFAULT_F2_EXPECTED_VERSION = "2.2.2"
DEFAULT_F2_STDOUT_MAX_BYTES = 1024 * 1024
DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS = 3
DEFAULT_GRAPH_CLASSIFICATION_MAX_HOPS = 1
DEFAULT_GRAPH_CLASSIFICATION_TOP_K = 8
DEFAULT_GRAPH_CLASSIFICATION_MODE = "off"
DEFAULT_GRAPH_EMBEDDING_DIMENSION = 384
DEFAULT_GRAPH_VECTOR_TOP_K = 12
DEFAULT_GRAPH_PROJECTION_BATCH_SIZE = 500
DEFAULT_GRAPH_CLASSIFICATION_ROLLOUT_PERCENT = 10
DEFAULT_GRAPH_FEEDBACK_EVAL_MIN_SAMPLES = 100
DEFAULT_MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT = 20


class Settings(BaseModel):
    """File Agent 后端运行配置。"""

    database_url: str
    auto_create_tables: bool = False
    jwt_secret_key: str = DEFAULT_JWT_SECRET_KEY
    jwt_algorithm: str = DEFAULT_JWT_ALGORITHM
    access_token_expire_minutes: int = DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES
    file_storage_root: str = DEFAULT_FILE_STORAGE_ROOT
    llm_enabled: bool = False
    llm_provider: str = "openai_compatible"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_chat_model: str = ""
    llm_timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_classification_mode: str = "rule_only"
    llm_classification_allow_free_paths: bool = False
    log_dir: str = DEFAULT_LOG_DIR
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS
    log_level: str = "INFO"
    ocr_enabled: bool = True
    ocr_paddle_model_source: str = DEFAULT_OCR_PADDLE_MODEL_SOURCE
    ocr_llm_enabled: bool = False
    ocr_llm_fallback_quality_threshold: float = DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD
    docling_enabled: bool = True
    docling_formats: tuple[str, ...] = DEFAULT_DOCLING_FORMATS
    docling_ocr_enabled: bool = False
    file_rename_executor: str = DEFAULT_FILE_RENAME_EXECUTOR
    file_rename_parse_mode: str = DEFAULT_FILE_RENAME_PARSE_MODE
    file_rename_max_batch_size: int = DEFAULT_FILE_RENAME_MAX_BATCH_SIZE
    file_rename_execution_timeout_seconds: int = DEFAULT_FILE_RENAME_EXECUTION_TIMEOUT_SECONDS
    f2_binary_path: str = "f2"
    f2_expected_version: str = DEFAULT_F2_EXPECTED_VERSION
    f2_fallback_to_native: bool = False
    f2_stdout_max_bytes: int = DEFAULT_F2_STDOUT_MAX_BYTES
    graph_classification_enabled: bool = False
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_query_timeout_seconds: int = DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS
    neo4j_sync_enabled: bool = False
    graph_classification_max_hops: int = DEFAULT_GRAPH_CLASSIFICATION_MAX_HOPS
    graph_classification_top_k: int = DEFAULT_GRAPH_CLASSIFICATION_TOP_K
    graph_classification_mode: str = DEFAULT_GRAPH_CLASSIFICATION_MODE
    graph_embedding_enabled: bool = False
    graph_embedding_provider: str = "local"
    graph_embedding_model_path: str = ""
    graph_embedding_model_name: str = ""
    graph_embedding_version: str = "document-semantic-v1"
    graph_embedding_dimension: int = DEFAULT_GRAPH_EMBEDDING_DIMENSION
    graph_vector_index_name: str = "document_version_embedding_v1"
    graph_vector_top_k: int = DEFAULT_GRAPH_VECTOR_TOP_K
    graph_vector_min_score: float = 0.0
    graph_projection_worker_enabled: bool = False
    graph_projection_batch_size: int = DEFAULT_GRAPH_PROJECTION_BATCH_SIZE
    graph_feedback_collection_enabled: bool = True
    graph_classification_rollout_percent: int = DEFAULT_GRAPH_CLASSIFICATION_ROLLOUT_PERCENT
    graph_feedback_eval_min_samples: int = DEFAULT_GRAPH_FEEDBACK_EVAL_MIN_SAMPLES
    managed_path_classification_profile_dir: str = "./rules/managed-root-classification"
    managed_path_default_mode: str = "NONE"
    managed_path_vector_pilot_limit: int = 1000
    managed_file_classification_sync_limit: int = DEFAULT_MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT
    managed_file_classification_batch_size: int = 20


def find_dotenv_file() -> Path | None:
    """从当前目录开始向上查找 `.env`，兼容项目根目录和 apps/api 目录启动。"""

    for directory in [Path.cwd(), *Path.cwd().parents]:
        env_path = directory / ".env"
        if env_path.exists():
            return env_path
    return None


def load_dotenv_file() -> None:
    """读取最近的上级 `.env`，仅填充当前进程尚未设置的环境变量。"""

    env_path = find_dotenv_file()
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        # 跳过空行和注释，避免把说明文本误当作配置项。
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith("MANAGED_ROOT_"):
            # 受管目录以 .env 为本地开发和部署配置入口，reload 后必须允许新值覆盖旧进程环境。
            os.environ[key] = value
        elif key:
            os.environ.setdefault(key, value)


def require_postgresql_database_url() -> str:
    """读取并校验 PostgreSQL 数据库连接串，禁止静默回退到 SQLite。"""

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. 请在项目根目录 .env 中配置 PostgreSQL 连接。")
    if not database_url.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must use PostgreSQL，禁止使用 SQLite 作为服务数据库。")
    return database_url


@lru_cache
def get_settings() -> Settings:
    """读取环境变量并返回缓存后的配置对象。"""

    load_dotenv_file()

    return Settings(
        database_url=require_postgresql_database_url(),
        auto_create_tables=os.getenv("AUTO_CREATE_TABLES", "false").lower() == "true",
        jwt_secret_key=os.getenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET_KEY),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", DEFAULT_JWT_ALGORITHM),
        access_token_expire_minutes=int(
            os.getenv(
                "ACCESS_TOKEN_EXPIRE_MINUTES",
                str(DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES),
            ),
        ),
        file_storage_root=os.getenv("FILE_STORAGE_ROOT", DEFAULT_FILE_STORAGE_ROOT),
        llm_enabled=os.getenv("LLM_ENABLED", "false").lower() == "true",
        llm_provider=os.getenv("LLM_PROVIDER", "openai_compatible"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", ""),
        llm_chat_model=os.getenv("LLM_CHAT_MODEL", ""),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS))),
        llm_classification_mode=os.getenv("LLM_CLASSIFICATION_MODE", "rule_only").lower(),
        llm_classification_allow_free_paths=os.getenv("LLM_CLASSIFICATION_ALLOW_FREE_PATHS", "false").lower() == "true",
        log_dir=os.getenv("LOG_DIR", DEFAULT_LOG_DIR),
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", str(DEFAULT_LOG_RETENTION_DAYS))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        ocr_enabled=os.getenv("OCR_ENABLED", "true").lower() == "true",
        ocr_paddle_model_source=os.getenv("OCR_PADDLE_MODEL_SOURCE", DEFAULT_OCR_PADDLE_MODEL_SOURCE),
        ocr_llm_enabled=os.getenv("OCR_LLM_ENABLED", "false").lower() == "true",
        ocr_llm_fallback_quality_threshold=float(
            os.getenv(
                "OCR_LLM_FALLBACK_QUALITY_THRESHOLD",
                str(DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD),
            )
        ),
        docling_enabled=os.getenv("DOCLING_ENABLED", "true").lower() == "true",
        docling_formats=tuple(
            item.strip().lower().lstrip(".")
            for item in os.getenv("DOCLING_FORMATS", ",".join(DEFAULT_DOCLING_FORMATS)).split(",")
            if item.strip()
        ),
        docling_ocr_enabled=os.getenv("DOCLING_OCR_ENABLED", "false").lower() == "true",
        file_rename_executor=os.getenv("FILE_RENAME_EXECUTOR", DEFAULT_FILE_RENAME_EXECUTOR),
        file_rename_parse_mode=_choice(
            os.getenv("FILE_RENAME_PARSE_MODE", DEFAULT_FILE_RENAME_PARSE_MODE),
            allowed={"hybrid", "native", "docling"},
            default=DEFAULT_FILE_RENAME_PARSE_MODE,
        ),
        file_rename_max_batch_size=int(
            os.getenv("FILE_RENAME_MAX_BATCH_SIZE", str(DEFAULT_FILE_RENAME_MAX_BATCH_SIZE))
        ),
        file_rename_execution_timeout_seconds=int(
            os.getenv(
                "FILE_RENAME_EXECUTION_TIMEOUT_SECONDS",
                str(DEFAULT_FILE_RENAME_EXECUTION_TIMEOUT_SECONDS),
            )
        ),
        f2_binary_path=os.getenv("F2_BINARY_PATH", "f2"),
        f2_expected_version=os.getenv("F2_EXPECTED_VERSION", DEFAULT_F2_EXPECTED_VERSION),
        f2_fallback_to_native=os.getenv("F2_FALLBACK_TO_NATIVE", "false").lower() == "true",
        f2_stdout_max_bytes=int(os.getenv("F2_STDOUT_MAX_BYTES", str(DEFAULT_F2_STDOUT_MAX_BYTES))),
        graph_classification_enabled=os.getenv("GRAPH_CLASSIFICATION_ENABLED", "false").lower() == "true",
        neo4j_uri=os.getenv("NEO4J_URI", "").strip(),
        neo4j_username=os.getenv("NEO4J_USERNAME", "").strip(),
        neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
        neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j").strip() or "neo4j",
        neo4j_query_timeout_seconds=max(
            1,
            int(os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", str(DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS))),
        ),
        neo4j_sync_enabled=os.getenv("NEO4J_SYNC_ENABLED", "false").lower() == "true",
        graph_classification_max_hops=max(
            1,
            min(2, int(os.getenv("GRAPH_CLASSIFICATION_MAX_HOPS", str(DEFAULT_GRAPH_CLASSIFICATION_MAX_HOPS)))),
        ),
        graph_classification_top_k=max(
            1,
            min(20, int(os.getenv("GRAPH_CLASSIFICATION_TOP_K", str(DEFAULT_GRAPH_CLASSIFICATION_TOP_K)))),
        ),
        graph_classification_mode=_choice(
            os.getenv("GRAPH_CLASSIFICATION_MODE", DEFAULT_GRAPH_CLASSIFICATION_MODE),
            allowed={"off", "shadow", "enabled"},
            default=DEFAULT_GRAPH_CLASSIFICATION_MODE,
        ),
        graph_embedding_enabled=os.getenv("GRAPH_EMBEDDING_ENABLED", "false").lower() == "true",
        graph_embedding_provider=os.getenv("GRAPH_EMBEDDING_PROVIDER", "local").strip().lower() or "local",
        graph_embedding_model_path=os.getenv("GRAPH_EMBEDDING_MODEL_PATH", "").strip(),
        graph_embedding_model_name=os.getenv("GRAPH_EMBEDDING_MODEL_NAME", "").strip(),
        graph_embedding_version=os.getenv("GRAPH_EMBEDDING_VERSION", "document-semantic-v1").strip()
        or "document-semantic-v1",
        graph_embedding_dimension=max(
            1,
            int(os.getenv("GRAPH_EMBEDDING_DIMENSION", str(DEFAULT_GRAPH_EMBEDDING_DIMENSION))),
        ),
        graph_vector_index_name=os.getenv(
            "GRAPH_VECTOR_INDEX_NAME",
            "document_version_embedding_v1",
        ).strip()
        or "document_version_embedding_v1",
        graph_vector_top_k=max(
            1,
            min(50, int(os.getenv("GRAPH_VECTOR_TOP_K", str(DEFAULT_GRAPH_VECTOR_TOP_K)))),
        ),
        graph_vector_min_score=max(0.0, min(1.0, float(os.getenv("GRAPH_VECTOR_MIN_SCORE", "0.0")))),
        graph_projection_worker_enabled=os.getenv("GRAPH_PROJECTION_WORKER_ENABLED", "false").lower() == "true",
        graph_projection_batch_size=max(
            1,
            min(5000, int(os.getenv("GRAPH_PROJECTION_BATCH_SIZE", str(DEFAULT_GRAPH_PROJECTION_BATCH_SIZE)))),
        ),
        graph_feedback_collection_enabled=os.getenv("GRAPH_FEEDBACK_COLLECTION_ENABLED", "true").lower() == "true",
        graph_classification_rollout_percent=max(
            0,
            min(
                100,
                int(
                    os.getenv(
                        "GRAPH_CLASSIFICATION_ROLLOUT_PERCENT",
                        str(DEFAULT_GRAPH_CLASSIFICATION_ROLLOUT_PERCENT),
                    )
                ),
            ),
        ),
        graph_feedback_eval_min_samples=max(
            1,
            int(os.getenv("GRAPH_FEEDBACK_EVAL_MIN_SAMPLES", str(DEFAULT_GRAPH_FEEDBACK_EVAL_MIN_SAMPLES))),
        ),
        managed_path_classification_profile_dir=os.getenv(
            "MANAGED_PATH_CLASSIFICATION_PROFILE_DIR",
            "./rules/managed-root-classification",
        ).strip()
        or "./rules/managed-root-classification",
        managed_path_default_mode=_choice(
            os.getenv("MANAGED_PATH_DEFAULT_MODE", "NONE"),
            allowed={"NONE", "PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"},
            default="NONE",
            normalize=str.upper,
        ),
        managed_path_vector_pilot_limit=max(
            1,
            int(os.getenv("MANAGED_PATH_VECTOR_PILOT_LIMIT", "1000")),
        ),
        managed_file_classification_sync_limit=max(
            1,
            min(
                200,
                int(
                    os.getenv(
                        "MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT",
                        str(DEFAULT_MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT),
                    )
                ),
            ),
        ),
        managed_file_classification_batch_size=max(
            1,
            min(200, int(os.getenv("MANAGED_FILE_CLASSIFICATION_BATCH_SIZE", "20"))),
        ),
    )


def _choice(
    value: str,
    *,
    allowed: set[str],
    default: str,
    normalize=lambda item: str(item).strip().lower(),
) -> str:
    """把枚举型环境变量收敛到受控集合，非法值使用安全默认值。"""

    normalized = normalize(value)
    return normalized if normalized in allowed else default
