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
DEFAULT_ADAPTIVE_PLANNER_MODE = "shadow"
DEFAULT_ADAPTIVE_PLANNER_ROLLOUT_PERCENT = 0
DEFAULT_ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT = 100
DEFAULT_ADAPTIVE_PLANNER_SCHEMA_VERSION = "planner-decision-v1"
DEFAULT_LOG_DIR = "./logs"
DEFAULT_LOG_RETENTION_DAYS = 7
DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD = 0.68
DEFAULT_PP_STRUCTURE_MAX_IMAGE_PIXELS = 24_000_000
DEFAULT_PP_STRUCTURE_MAX_PDF_PAGES = 50
DEFAULT_PP_STRUCTURE_TEXT_DETECTION_MODEL = "PP-OCRv6_medium_det"
DEFAULT_PP_STRUCTURE_TEXT_RECOGNITION_MODEL = "PP-OCRv6_medium_rec"
DEFAULT_STRUCTURED_EXTRACTION_MAX_FIELDS = 40
DEFAULT_STRUCTURED_EXTRACTION_MAX_RETRY_FIELDS = 20
DEFAULT_STRUCTURED_EXTRACTION_MAX_RECORDS = 1000
DEFAULT_OCR_PADDLE_MODEL_SOURCE = "BOS"
DEFAULT_OCR_PROVIDER = "tencent_cloud"
DEFAULT_TENCENT_CLOUD_OCR_REGION = "ap-guangzhou"
DEFAULT_TENCENT_CLOUD_OCR_ENDPOINT = "ocr.tencentcloudapi.com"
DEFAULT_TENCENT_CLOUD_OCR_ACTION = "GeneralAccurateOCR"
DEFAULT_TENCENT_CLOUD_OCR_TIMEOUT_SECONDS = 30
DEFAULT_TENCENT_CLOUD_OCR_MAX_RETRIES = 2
DEFAULT_TENCENT_CLOUD_OCR_MAX_QPS = 2
DEFAULT_TENCENT_CLOUD_OCR_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TENCENT_CLOUD_TABLE_OCR_MAX_QPS = 2
DEFAULT_DOCLING_FORMATS = ("pdf", "docx")
DEFAULT_FILE_RENAME_EXECUTOR = "native"
DEFAULT_FILE_RENAME_PARSE_MODE = "hybrid"
DEFAULT_FILE_RENAME_MAX_BATCH_SIZE = 20
DEFAULT_FILE_RENAME_EXECUTION_TIMEOUT_SECONDS = 60
DEFAULT_FILE_RENAME_LLM_VALIDATION_THRESHOLD = 0.60
DEFAULT_FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS = 30
DEFAULT_FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH = 20
DEFAULT_FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION = "rename-validation-v1"
DEFAULT_F2_EXPECTED_VERSION = "2.2.2"
DEFAULT_F2_STDOUT_MAX_BYTES = 1024 * 1024
DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS = 3
DEFAULT_GRAPH_CLASSIFICATION_MAX_HOPS = 1
DEFAULT_GRAPH_CLASSIFICATION_TOP_K = 8
DEFAULT_GRAPH_CLASSIFICATION_MODE = "shadow"
DEFAULT_GRAPH_EMBEDDING_DIMENSION = 384
DEFAULT_GRAPH_VECTOR_TOP_K = 12
DEFAULT_GRAPH_PROJECTION_BATCH_SIZE = 500
DEFAULT_GRAPH_CLASSIFICATION_ROLLOUT_PERCENT = 10
DEFAULT_GRAPH_FEEDBACK_EVAL_MIN_SAMPLES = 100
DEFAULT_MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT = 20
DEFAULT_MANAGED_FILE_INITIALIZATION_MODE = "source_index_first"
DEFAULT_MANAGED_SOURCE_ANALYSIS_BACKGROUND_PRIORITY = 100
DEFAULT_MANAGED_SOURCE_ANALYSIS_ON_DEMAND_PRIORITY = 10
DEFAULT_MANAGED_SOURCE_ANALYSIS_BATCH_SIZE = 20
DEFAULT_MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY = 1
DEFAULT_MATERIALIZE_WORKING_COPY_PRIORITY = 20
DEFAULT_MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY = 100
DEFAULT_MATERIALIZE_RELEVANT_FILES_BATCH_SIZE = 50
DEFAULT_LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS = 90
DEFAULT_LEGACY_OFFICE_MAX_FILE_SIZE_MB = 100
DEFAULT_LEGACY_OFFICE_DERIVATIVE_DIR = "derivatives/office"
DEFAULT_MANAGED_ROOT_RECONCILE_INTERVAL_SECONDS = 300
DEFAULT_MANAGED_ROOT_SCAN_BATCH_SIZE = 100
DEFAULT_MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS = 5
DEFAULT_UPLOAD_ARCHIVE_RETRY_INTERVAL_SECONDS = 300
DEFAULT_UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD = 0.90
DEFAULT_UPLOAD_DUPLICATE_MAX_CANDIDATES = 5
DEFAULT_UPLOAD_DUPLICATE_CONFIRMATION_TTL_HOURS = 168
DEFAULT_FILESYSTEM_JOB_LEASE_SECONDS = 120
DEFAULT_WORKING_COPY_IMPORT_BATCH_SIZE = 100
DEFAULT_WORKING_COPY_OPERATION_BATCH_SIZE = 20
DEFAULT_TRASH_RETENTION_DAYS = 30
DEFAULT_INITIAL_ORGANIZATION_CONFIDENCE = 0.60
DEFAULT_AUTO_CLASSIFICATION_POLICY_VERSION = "auto-placement-top1-test-v1"
DEFAULT_AUTO_CLASSIFICATION_CALIBRATION_VERSION = "unpublished"
DEFAULT_AUTO_CLASSIFICATION_TARGET_PRECISION = 0.99
DEFAULT_AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY = "conservative-v1"
DEFAULT_AUTO_CLASSIFICATION_FALLBACK_THRESHOLD = 0.90
DEFAULT_AUTO_CLASSIFICATION_FALLBACK_MARGIN = 0.20
DEFAULT_UPLOAD_MAX_FILE_SIZE_MB = 1024
DEFAULT_UPLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
DEFAULT_DOCUMENT_CHUNK_MAX_CHARS = 1200
DEFAULT_DOCUMENT_CHUNK_OVERLAP_CHARS = 120
DEFAULT_DOCUMENT_INDEX_MAX_CHARS = 50_000_000
DEFAULT_DOCUMENT_INDEX_MAX_CHUNKS = 50_000
DEFAULT_DOCUMENT_SUMMARY_PROVIDER = "extractive"
DEFAULT_CLASSIFICATION_SUMMARY_PROVIDER = "extractive"
DEFAULT_CHAT_DOCUMENT_SUMMARY_PROVIDER = "llm"
DEFAULT_AGENT_RECEIPT_SUMMARY_PROVIDER = "llm"
DEFAULT_EVIDENCE_ANSWER_PROVIDER = "llm"
DEFAULT_EVIDENCE_ANSWER_PROMPT_VERSION = "evidence-answer-v1"
DEFAULT_EVIDENCE_ANSWER_SCHEMA_VERSION = "evidence-answer-schema-v1"
DEFAULT_EVIDENCE_ANSWER_MAX_DOCUMENTS = 12
DEFAULT_EVIDENCE_ANSWER_MAX_ITEMS = 48
DEFAULT_EVIDENCE_ANSWER_MAX_INPUT_CHARS = 120_000
DEFAULT_EVIDENCE_ANSWER_MAX_CALLS = 3
DEFAULT_EVIDENCE_ANSWER_REPAIR_CALLS = 1
DEFAULT_UPLOAD_ALLOWED_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".txt",
    ".md",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tiff",
)


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
    adaptive_planner_mode: str = DEFAULT_ADAPTIVE_PLANNER_MODE
    adaptive_planner_rollout_percent: int = DEFAULT_ADAPTIVE_PLANNER_ROLLOUT_PERCENT
    adaptive_planner_shadow_sample_percent: int = (
        DEFAULT_ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT
    )
    adaptive_planner_schema_version: str = DEFAULT_ADAPTIVE_PLANNER_SCHEMA_VERSION
    llm_classification_mode: str = "rule_only"
    llm_classification_allow_free_paths: bool = False
    document_summary_enabled: bool = True
    document_summary_provider: str = DEFAULT_DOCUMENT_SUMMARY_PROVIDER
    document_summary_prompt_version: str = "document-summary-v1"
    document_summary_schema_version: str = "document-summary-schema-v1"
    llm_classification_summary_enabled: bool = True
    classification_summary_provider: str = DEFAULT_CLASSIFICATION_SUMMARY_PROVIDER
    llm_classification_summary_prompt_version: str = "classification-topic-summary-v1"
    classification_summary_schema_version: str = "classification-topic-summary-schema-v1"
    chat_document_summary_provider: str = DEFAULT_CHAT_DOCUMENT_SUMMARY_PROVIDER
    # 对话最终回执与后台导入摘要隔离；该 Provider 只读取后端验证后的业务摘要。
    agent_receipt_summary_provider: str = DEFAULT_AGENT_RECEIPT_SUMMARY_PROVIDER
    evidence_answer_enabled: bool = True
    evidence_answer_provider: str = DEFAULT_EVIDENCE_ANSWER_PROVIDER
    evidence_answer_prompt_version: str = DEFAULT_EVIDENCE_ANSWER_PROMPT_VERSION
    evidence_answer_schema_version: str = DEFAULT_EVIDENCE_ANSWER_SCHEMA_VERSION
    evidence_answer_max_documents: int = DEFAULT_EVIDENCE_ANSWER_MAX_DOCUMENTS
    evidence_answer_max_items: int = DEFAULT_EVIDENCE_ANSWER_MAX_ITEMS
    evidence_answer_max_input_chars: int = DEFAULT_EVIDENCE_ANSWER_MAX_INPUT_CHARS
    evidence_answer_max_calls: int = DEFAULT_EVIDENCE_ANSWER_MAX_CALLS
    evidence_answer_repair_calls: int = DEFAULT_EVIDENCE_ANSWER_REPAIR_CALLS
    evidence_answer_cache_enabled: bool = True
    initial_working_copy_organization_enabled: bool = True
    initial_organization_confidence: float = DEFAULT_INITIAL_ORGANIZATION_CONFIDENCE
    # 上传默认执行主分类并将工作副本首次发布到分类目录；
    # 两个开关和 Shadow 模式仍保留为运维紧急回退边界。
    auto_primary_classification_enabled: bool = True
    auto_initial_placement_enabled: bool = True
    auto_classification_shadow_mode: bool = False
    auto_classification_policy_version: str = DEFAULT_AUTO_CLASSIFICATION_POLICY_VERSION
    auto_classification_calibration_version: str = DEFAULT_AUTO_CLASSIFICATION_CALIBRATION_VERSION
    auto_classification_target_precision: float = DEFAULT_AUTO_CLASSIFICATION_TARGET_PRECISION
    auto_classification_full_taxonomy_enabled: bool = True
    auto_classification_global_fallback_policy: str = (
        DEFAULT_AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY
    )
    auto_classification_fallback_threshold: float = DEFAULT_AUTO_CLASSIFICATION_FALLBACK_THRESHOLD
    auto_classification_fallback_margin: float = DEFAULT_AUTO_CLASSIFICATION_FALLBACK_MARGIN
    upload_max_file_size_mb: int = DEFAULT_UPLOAD_MAX_FILE_SIZE_MB
    upload_chunk_size_bytes: int = DEFAULT_UPLOAD_CHUNK_SIZE_BYTES
    upload_allowed_extensions: tuple[str, ...] = DEFAULT_UPLOAD_ALLOWED_EXTENSIONS
    retrieval_mode: str = "lexical"
    chinese_tokenizer: str = "jieba"
    # 阶段四的 CPU 两阶段检索是默认主路径；关闭仅用于紧急回退旧兼容实现。
    two_stage_retrieval_enabled: bool = True
    retrieval_document_candidate_limit: int = 30
    retrieval_document_detail_limit: int = 12
    retrieval_chunk_limit_per_document: int = 3
    retrieval_chunk_global_limit: int = 24
    retrieval_query_max_chars: int = 500
    retrieval_preview_max_chars: int = 240
    retrieval_statement_timeout_ms: int = 2000
    # 受管原始目录先建立只读检索索引；分析完成后再由独立队列全量同步工作副本。
    managed_file_initialization_mode: str = DEFAULT_MANAGED_FILE_INITIALIZATION_MODE
    managed_source_analysis_enabled: bool = True
    managed_source_analysis_background_priority: int = DEFAULT_MANAGED_SOURCE_ANALYSIS_BACKGROUND_PRIORITY
    managed_source_analysis_on_demand_priority: int = DEFAULT_MANAGED_SOURCE_ANALYSIS_ON_DEMAND_PRIORITY
    managed_source_analysis_batch_size: int = DEFAULT_MANAGED_SOURCE_ANALYSIS_BATCH_SIZE
    managed_source_libreoffice_concurrency: int = DEFAULT_MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY
    managed_source_search_enabled: bool = True
    materialize_all_managed_files: bool = True
    materialize_relevant_files_after_response: bool = True
    materialize_working_copy_priority: int = DEFAULT_MATERIALIZE_WORKING_COPY_PRIORITY
    materialize_working_copy_background_priority: int = (
        DEFAULT_MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY
    )
    materialize_relevant_files_batch_size: int = DEFAULT_MATERIALIZE_RELEVANT_FILES_BATCH_SIZE
    retrieval_filename_trgm_min_chars: int = 4
    retrieval_filename_trgm_candidate_limit: int = 20
    retrieval_filename_trgm_similarity_threshold: float = 0.25
    document_chunk_max_chars: int = DEFAULT_DOCUMENT_CHUNK_MAX_CHARS
    document_chunk_overlap_chars: int = DEFAULT_DOCUMENT_CHUNK_OVERLAP_CHARS
    document_index_max_chars: int = DEFAULT_DOCUMENT_INDEX_MAX_CHARS
    document_index_max_chunks: int = DEFAULT_DOCUMENT_INDEX_MAX_CHUNKS
    embedding_enabled: bool = False
    embedding_provider: str = "disabled"
    log_dir: str = DEFAULT_LOG_DIR
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS
    log_level: str = "INFO"
    ocr_enabled: bool = True
    ocr_provider: str = DEFAULT_OCR_PROVIDER
    ocr_external_content_authorized: bool = False
    tencent_cloud_ocr_secret_id: str = ""
    tencent_cloud_ocr_secret_key: str = ""
    tencent_cloud_ocr_region: str = DEFAULT_TENCENT_CLOUD_OCR_REGION
    tencent_cloud_ocr_endpoint: str = DEFAULT_TENCENT_CLOUD_OCR_ENDPOINT
    tencent_cloud_ocr_action: str = DEFAULT_TENCENT_CLOUD_OCR_ACTION
    tencent_cloud_ocr_timeout_seconds: int = DEFAULT_TENCENT_CLOUD_OCR_TIMEOUT_SECONDS
    tencent_cloud_ocr_max_retries: int = DEFAULT_TENCENT_CLOUD_OCR_MAX_RETRIES
    tencent_cloud_ocr_max_qps: int = DEFAULT_TENCENT_CLOUD_OCR_MAX_QPS
    tencent_cloud_ocr_max_image_bytes: int = DEFAULT_TENCENT_CLOUD_OCR_MAX_IMAGE_BYTES
    tencent_cloud_table_ocr_max_qps: int = DEFAULT_TENCENT_CLOUD_TABLE_OCR_MAX_QPS
    ocr_local_fallback_enabled: bool = False
    ocr_paddle_model_source: str = DEFAULT_OCR_PADDLE_MODEL_SOURCE
    ocr_llm_enabled: bool = False
    ocr_llm_fallback_quality_threshold: float = DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD
    pp_structure_enabled: bool = False
    pp_structure_device: str = "cpu"
    pp_structure_pipeline_config: str = "PP-StructureV3"
    pp_structure_model_source: str = "BOS"
    pp_structure_text_detection_model: str = (
        DEFAULT_PP_STRUCTURE_TEXT_DETECTION_MODEL
    )
    pp_structure_text_recognition_model: str = (
        DEFAULT_PP_STRUCTURE_TEXT_RECOGNITION_MODEL
    )
    pp_structure_use_doc_preprocessor: bool = True
    pp_structure_use_table_recognition: bool = False
    pp_structure_use_formula_recognition: bool = False
    pp_structure_use_chart_recognition: bool = False
    pp_structure_use_seal_recognition: bool = False
    pp_structure_use_region_detection: bool = True
    pp_structure_max_image_pixels: int = DEFAULT_PP_STRUCTURE_MAX_IMAGE_PIXELS
    pp_structure_max_pdf_pages: int = DEFAULT_PP_STRUCTURE_MAX_PDF_PAGES
    structured_extraction_enabled: bool = False
    structured_extraction_layout_provider: str = "pp_structure_v3"
    structured_extraction_llm_provider: str = "disabled"
    structured_extraction_llm_base_url: str = ""
    structured_extraction_llm_api_key: str = ""
    structured_extraction_llm_model: str = ""
    structured_extraction_llm_timeout_seconds: int = 180
    structured_extraction_task_timeout_seconds: int = 300
    structured_extraction_max_fields: int = DEFAULT_STRUCTURED_EXTRACTION_MAX_FIELDS
    structured_extraction_max_retry_fields: int = DEFAULT_STRUCTURED_EXTRACTION_MAX_RETRY_FIELDS
    structured_extraction_max_records: int = DEFAULT_STRUCTURED_EXTRACTION_MAX_RECORDS
    structured_extraction_prompt_version: str = "structured-extraction-v1"
    structured_extraction_high_confidence: float = 0.85
    structured_extraction_retry_confidence: float = 0.65
    structured_extraction_external_images_authorized: bool = False
    structured_extraction_vision_provider: str = "disabled"
    structured_extraction_vision_crop_upscale: float = 2.0
    paddleocr_vl_pipeline_version: str = "v1.6"
    paddleocr_vl_model_name: str = "PaddleOCR-VL-1.6-0.9B"
    paddleocr_vl_backend: str = "native"
    paddleocr_vl_device: str = "cpu"
    paddleocr_vl_max_new_tokens: int = 4096
    docling_enabled: bool = True
    docling_formats: tuple[str, ...] = DEFAULT_DOCLING_FORMATS
    docling_ocr_enabled: bool = False
    legacy_office_conversion_enabled: bool = True
    legacy_office_converter: str = "libreoffice"
    libreoffice_executable: str = ""
    legacy_office_conversion_timeout_seconds: int = DEFAULT_LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS
    legacy_office_max_file_size_mb: int = DEFAULT_LEGACY_OFFICE_MAX_FILE_SIZE_MB
    legacy_office_derivative_dir: str = DEFAULT_LEGACY_OFFICE_DERIVATIVE_DIR
    file_rename_executor: str = DEFAULT_FILE_RENAME_EXECUTOR
    file_rename_parse_mode: str = DEFAULT_FILE_RENAME_PARSE_MODE
    file_rename_max_batch_size: int = DEFAULT_FILE_RENAME_MAX_BATCH_SIZE
    file_rename_execution_timeout_seconds: int = DEFAULT_FILE_RENAME_EXECUTION_TIMEOUT_SECONDS
    file_rename_llm_validation_enabled: bool = False
    file_rename_llm_validation_mode: str = "risk_based"
    file_rename_llm_validation_threshold: float = DEFAULT_FILE_RENAME_LLM_VALIDATION_THRESHOLD
    file_rename_llm_validation_timeout_seconds: int = DEFAULT_FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS
    file_rename_llm_validation_max_items_per_batch: int = DEFAULT_FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH
    file_rename_llm_validation_prompt_version: str = DEFAULT_FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION
    f2_binary_path: str = "f2"
    f2_expected_version: str = DEFAULT_F2_EXPECTED_VERSION
    f2_fallback_to_native: bool = False
    f2_stdout_max_bytes: int = DEFAULT_F2_STDOUT_MAX_BYTES
    # 图谱增强默认运行在 Shadow：执行投影和候选对比，但在完成评估前不改变用户可见分类结果。
    graph_classification_enabled: bool = True
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_query_timeout_seconds: int = DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS
    neo4j_sync_enabled: bool = True
    graph_classification_max_hops: int = DEFAULT_GRAPH_CLASSIFICATION_MAX_HOPS
    graph_classification_top_k: int = DEFAULT_GRAPH_CLASSIFICATION_TOP_K
    graph_classification_mode: str = DEFAULT_GRAPH_CLASSIFICATION_MODE
    graph_embedding_enabled: bool = True
    graph_embedding_provider: str = "local"
    graph_embedding_model_path: str = ""
    graph_embedding_model_name: str = ""
    graph_embedding_version: str = "document-semantic-v1"
    graph_embedding_dimension: int = DEFAULT_GRAPH_EMBEDDING_DIMENSION
    graph_vector_index_name: str = "document_version_embedding_v1"
    graph_vector_top_k: int = DEFAULT_GRAPH_VECTOR_TOP_K
    graph_vector_min_score: float = 0.0
    graph_projection_worker_enabled: bool = True
    graph_projection_batch_size: int = DEFAULT_GRAPH_PROJECTION_BATCH_SIZE
    graph_feedback_collection_enabled: bool = True
    graph_classification_rollout_percent: int = DEFAULT_GRAPH_CLASSIFICATION_ROLLOUT_PERCENT
    graph_feedback_eval_min_samples: int = DEFAULT_GRAPH_FEEDBACK_EVAL_MIN_SAMPLES
    managed_path_classification_profile_dir: str = "./rules/managed-root-classification"
    managed_path_default_mode: str = "NONE"
    managed_path_vector_pilot_limit: int = 1000
    managed_file_classification_sync_limit: int = DEFAULT_MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT
    managed_file_classification_batch_size: int = 20
    managed_root_archive_write_path: str = ""
    managed_root_archive_enabled: bool = True
    working_copy_storage_root: str = "./storage/working-copies"
    trash_storage_root: str = "./storage/trash"
    managed_root_watch_enabled: bool = True
    managed_root_reconcile_interval_seconds: int = DEFAULT_MANAGED_ROOT_RECONCILE_INTERVAL_SECONDS
    managed_root_reconcile_on_startup: bool = True
    # 增量扫描每批完成后立即提交导入任务，避免大型目录全量扫描阻塞工作副本创建。
    managed_root_scan_batch_size: int = DEFAULT_MANAGED_ROOT_SCAN_BATCH_SIZE
    managed_root_scan_batch_max_seconds: int = DEFAULT_MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS
    upload_archive_enabled: bool = True
    upload_archive_retry_interval_seconds: int = DEFAULT_UPLOAD_ARCHIVE_RETRY_INTERVAL_SECONDS
    upload_duplicate_check_enabled: bool = True
    upload_duplicate_similarity_threshold: float = DEFAULT_UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD
    upload_duplicate_max_candidates: int = DEFAULT_UPLOAD_DUPLICATE_MAX_CANDIDATES
    upload_duplicate_confirmation_ttl_hours: int = DEFAULT_UPLOAD_DUPLICATE_CONFIRMATION_TTL_HOURS
    filesystem_async_jobs_enabled: bool = True
    filesystem_job_lease_seconds: int = DEFAULT_FILESYSTEM_JOB_LEASE_SECONDS
    archive_worker_concurrency: int = 2
    import_worker_concurrency: int = 2
    working_copy_import_batch_size: int = DEFAULT_WORKING_COPY_IMPORT_BATCH_SIZE
    working_copy_operation_batch_size: int = DEFAULT_WORKING_COPY_OPERATION_BATCH_SIZE
    trash_retention_days: int = DEFAULT_TRASH_RETENTION_DAYS
    trash_auto_purge_enabled: bool = False


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


def _normalize_background_summary_provider(value: str) -> str:
    """规范化后台双摘要 Provider，未知值安全回退本地抽取式实现。

    ``openai_compatible`` 是旧配置文档使用过的名称，继续映射为 ``llm``，避免升级后
    意外关闭已经获得部署授权的模型；其他未知值不能触发文件正文外发。
    """

    normalized = str(value or "").strip().lower()
    if normalized in {"llm", "openai_compatible"}:
        return "llm"
    return DEFAULT_DOCUMENT_SUMMARY_PROVIDER


def _normalize_chat_summary_provider(value: str) -> str:
    """规范化用户显式总结 Provider；当前仅支持 LLM 或关闭两种受控模式。"""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"llm", "disabled"} else DEFAULT_CHAT_DOCUMENT_SUMMARY_PROVIDER


def _normalize_evidence_answer_provider(value: str) -> str:
    """规范化阶段五回答 Provider，未知值不得隐式调用外部模型。"""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"llm", "disabled"} else "disabled"


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """读取有界整数配置，非法值必须在启动阶段明确失败，不能静默改写部署意图。"""

    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} 必须在 {minimum} 到 {maximum} 之间，当前值为 {value}")
    return value


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
        adaptive_planner_mode=_choice(
            os.getenv("ADAPTIVE_PLANNER_MODE", DEFAULT_ADAPTIVE_PLANNER_MODE),
            allowed={"legacy", "shadow", "enabled"},
            default=DEFAULT_ADAPTIVE_PLANNER_MODE,
        ),
        adaptive_planner_rollout_percent=_bounded_int_env(
            "ADAPTIVE_PLANNER_ROLLOUT_PERCENT",
            DEFAULT_ADAPTIVE_PLANNER_ROLLOUT_PERCENT,
            minimum=0,
            maximum=100,
        ),
        adaptive_planner_shadow_sample_percent=_bounded_int_env(
            "ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT",
            DEFAULT_ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT,
            minimum=0,
            maximum=100,
        ),
        adaptive_planner_schema_version=os.getenv(
            "ADAPTIVE_PLANNER_SCHEMA_VERSION",
            DEFAULT_ADAPTIVE_PLANNER_SCHEMA_VERSION,
        ).strip()
        or DEFAULT_ADAPTIVE_PLANNER_SCHEMA_VERSION,
        llm_classification_mode=os.getenv("LLM_CLASSIFICATION_MODE", "rule_only").lower(),
        llm_classification_allow_free_paths=os.getenv("LLM_CLASSIFICATION_ALLOW_FREE_PATHS", "false").lower() == "true",
        document_summary_enabled=os.getenv("DOCUMENT_SUMMARY_ENABLED", "true").lower() == "true",
        document_summary_provider=_normalize_background_summary_provider(
            os.getenv("DOCUMENT_SUMMARY_PROVIDER", DEFAULT_DOCUMENT_SUMMARY_PROVIDER)
        ),
        document_summary_prompt_version=os.getenv(
            "DOCUMENT_SUMMARY_PROMPT_VERSION", "document-summary-v1"
        ).strip() or "document-summary-v1",
        document_summary_schema_version=os.getenv(
            "DOCUMENT_SUMMARY_SCHEMA_VERSION", "document-summary-schema-v1"
        ).strip() or "document-summary-schema-v1",
        llm_classification_summary_enabled=os.getenv(
            "LLM_CLASSIFICATION_SUMMARY_ENABLED", "true"
        ).lower() == "true",
        classification_summary_provider=_normalize_background_summary_provider(
            os.getenv("CLASSIFICATION_SUMMARY_PROVIDER", DEFAULT_CLASSIFICATION_SUMMARY_PROVIDER)
        ),
        llm_classification_summary_prompt_version=os.getenv(
            "LLM_CLASSIFICATION_SUMMARY_PROMPT_VERSION", "classification-topic-summary-v1"
        ).strip() or "classification-topic-summary-v1",
        classification_summary_schema_version=os.getenv(
            "CLASSIFICATION_SUMMARY_SCHEMA_VERSION", "classification-topic-summary-schema-v1"
        ).strip() or "classification-topic-summary-schema-v1",
        chat_document_summary_provider=_normalize_chat_summary_provider(
            os.getenv("CHAT_DOCUMENT_SUMMARY_PROVIDER", DEFAULT_CHAT_DOCUMENT_SUMMARY_PROVIDER)
        ),
        agent_receipt_summary_provider=_normalize_chat_summary_provider(
            os.getenv(
                "AGENT_RECEIPT_SUMMARY_PROVIDER",
                DEFAULT_AGENT_RECEIPT_SUMMARY_PROVIDER,
            )
        ),
        evidence_answer_enabled=os.getenv("EVIDENCE_ANSWER_ENABLED", "true").lower() == "true",
        evidence_answer_provider=_normalize_evidence_answer_provider(
            os.getenv("EVIDENCE_ANSWER_PROVIDER", DEFAULT_EVIDENCE_ANSWER_PROVIDER)
        ),
        evidence_answer_prompt_version=os.getenv(
            "EVIDENCE_ANSWER_PROMPT_VERSION",
            DEFAULT_EVIDENCE_ANSWER_PROMPT_VERSION,
        ).strip()
        or DEFAULT_EVIDENCE_ANSWER_PROMPT_VERSION,
        evidence_answer_schema_version=os.getenv(
            "EVIDENCE_ANSWER_SCHEMA_VERSION",
            DEFAULT_EVIDENCE_ANSWER_SCHEMA_VERSION,
        ).strip()
        or DEFAULT_EVIDENCE_ANSWER_SCHEMA_VERSION,
        evidence_answer_max_documents=_bounded_int_env(
            "EVIDENCE_ANSWER_MAX_DOCUMENTS",
            DEFAULT_EVIDENCE_ANSWER_MAX_DOCUMENTS,
            minimum=1,
            maximum=50,
        ),
        evidence_answer_max_items=_bounded_int_env(
            "EVIDENCE_ANSWER_MAX_ITEMS",
            DEFAULT_EVIDENCE_ANSWER_MAX_ITEMS,
            minimum=1,
            maximum=500,
        ),
        evidence_answer_max_input_chars=_bounded_int_env(
            "EVIDENCE_ANSWER_MAX_INPUT_CHARS",
            DEFAULT_EVIDENCE_ANSWER_MAX_INPUT_CHARS,
            minimum=10_000,
            maximum=1_000_000,
        ),
        evidence_answer_max_calls=_bounded_int_env(
            "EVIDENCE_ANSWER_MAX_CALLS",
            DEFAULT_EVIDENCE_ANSWER_MAX_CALLS,
            minimum=1,
            maximum=10,
        ),
        evidence_answer_repair_calls=_bounded_int_env(
            "EVIDENCE_ANSWER_REPAIR_CALLS",
            DEFAULT_EVIDENCE_ANSWER_REPAIR_CALLS,
            minimum=0,
            maximum=3,
        ),
        evidence_answer_cache_enabled=os.getenv(
            "EVIDENCE_ANSWER_CACHE_ENABLED", "true"
        ).lower()
        == "true",
        initial_working_copy_organization_enabled=os.getenv(
            "INITIAL_WORKING_COPY_ORGANIZATION_ENABLED", "true"
        ).lower() == "true",
        initial_organization_confidence=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "INITIAL_ORGANIZATION_CONFIDENCE",
                        str(DEFAULT_INITIAL_ORGANIZATION_CONFIDENCE),
                    )
                ),
            ),
        ),
        auto_primary_classification_enabled=os.getenv(
            "AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true"
        ).lower() == "true",
        auto_initial_placement_enabled=os.getenv(
            "AUTO_INITIAL_PLACEMENT_ENABLED", "true"
        ).lower() == "true",
        auto_classification_shadow_mode=os.getenv(
            "AUTO_CLASSIFICATION_SHADOW_MODE", "false"
        ).lower() == "true",
        auto_classification_policy_version=os.getenv(
            "AUTO_CLASSIFICATION_POLICY_VERSION",
            DEFAULT_AUTO_CLASSIFICATION_POLICY_VERSION,
        ).strip() or DEFAULT_AUTO_CLASSIFICATION_POLICY_VERSION,
        auto_classification_calibration_version=os.getenv(
            "AUTO_CLASSIFICATION_CALIBRATION_VERSION",
            DEFAULT_AUTO_CLASSIFICATION_CALIBRATION_VERSION,
        ).strip() or DEFAULT_AUTO_CLASSIFICATION_CALIBRATION_VERSION,
        auto_classification_target_precision=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "AUTO_CLASSIFICATION_TARGET_PRECISION",
                        str(DEFAULT_AUTO_CLASSIFICATION_TARGET_PRECISION),
                    )
                ),
            ),
        ),
        auto_classification_full_taxonomy_enabled=os.getenv(
            "AUTO_CLASSIFICATION_FULL_TAXONOMY_ENABLED", "true"
        ).lower() == "true",
        auto_classification_global_fallback_policy=os.getenv(
            "AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY",
            DEFAULT_AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY,
        ).strip() or DEFAULT_AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY,
        auto_classification_fallback_threshold=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "AUTO_CLASSIFICATION_FALLBACK_THRESHOLD",
                        str(DEFAULT_AUTO_CLASSIFICATION_FALLBACK_THRESHOLD),
                    )
                ),
            ),
        ),
        auto_classification_fallback_margin=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "AUTO_CLASSIFICATION_FALLBACK_MARGIN",
                        str(DEFAULT_AUTO_CLASSIFICATION_FALLBACK_MARGIN),
                    )
                ),
            ),
        ),
        upload_max_file_size_mb=max(
            1,
            int(os.getenv("UPLOAD_MAX_FILE_SIZE_MB", str(DEFAULT_UPLOAD_MAX_FILE_SIZE_MB))),
        ),
        upload_chunk_size_bytes=max(
            64 * 1024,
            min(
                8 * 1024 * 1024,
                int(os.getenv("UPLOAD_CHUNK_SIZE_BYTES", str(DEFAULT_UPLOAD_CHUNK_SIZE_BYTES))),
            ),
        ),
        upload_allowed_extensions=tuple(
            sorted(
                {
                    f".{item.strip().lower().lstrip('.')}"
                    for item in os.getenv(
                        "UPLOAD_ALLOWED_EXTENSIONS",
                        ",".join(DEFAULT_UPLOAD_ALLOWED_EXTENSIONS),
                    ).split(",")
                    if item.strip()
                }
            )
        ),
        retrieval_mode=_choice(
            os.getenv("RETRIEVAL_MODE", "lexical"),
            allowed={"lexical", "hybrid"},
            default="lexical",
        ),
        chinese_tokenizer=_choice(
            os.getenv("CHINESE_TOKENIZER", "jieba"),
            allowed={"jieba"},
            default="jieba",
        ),
        two_stage_retrieval_enabled=os.getenv("TWO_STAGE_RETRIEVAL_ENABLED", "true").lower() == "true",
        retrieval_document_candidate_limit=max(
            1, min(50, int(os.getenv("RETRIEVAL_DOCUMENT_CANDIDATE_LIMIT", "30")))
        ),
        retrieval_document_detail_limit=max(
            1, min(20, int(os.getenv("RETRIEVAL_DOCUMENT_DETAIL_LIMIT", "12")))
        ),
        retrieval_chunk_limit_per_document=max(
            1, min(3, int(os.getenv("RETRIEVAL_CHUNK_LIMIT_PER_DOCUMENT", "3")))
        ),
        retrieval_chunk_global_limit=max(
            1, min(24, int(os.getenv("RETRIEVAL_CHUNK_GLOBAL_LIMIT", "24")))
        ),
        retrieval_query_max_chars=max(
            10, min(500, int(os.getenv("RETRIEVAL_QUERY_MAX_CHARS", "500")))
        ),
        retrieval_preview_max_chars=max(
            10, min(1000, int(os.getenv("RETRIEVAL_PREVIEW_MAX_CHARS", "240")))
        ),
        retrieval_statement_timeout_ms=max(
            100, min(30000, int(os.getenv("RETRIEVAL_STATEMENT_TIMEOUT_MS", "2000")))
        ),
        managed_file_initialization_mode=_choice(
            os.getenv(
                "MANAGED_FILE_INITIALIZATION_MODE",
                DEFAULT_MANAGED_FILE_INITIALIZATION_MODE,
            ),
            allowed={"source_index_first", "eager_working_copy"},
            default=DEFAULT_MANAGED_FILE_INITIALIZATION_MODE,
        ),
        managed_source_analysis_enabled=os.getenv(
            "MANAGED_SOURCE_ANALYSIS_ENABLED", "true"
        ).lower() == "true",
        managed_source_analysis_background_priority=_bounded_int_env(
            "MANAGED_SOURCE_ANALYSIS_BACKGROUND_PRIORITY",
            DEFAULT_MANAGED_SOURCE_ANALYSIS_BACKGROUND_PRIORITY,
            minimum=1,
            maximum=1000,
        ),
        managed_source_analysis_on_demand_priority=_bounded_int_env(
            "MANAGED_SOURCE_ANALYSIS_ON_DEMAND_PRIORITY",
            DEFAULT_MANAGED_SOURCE_ANALYSIS_ON_DEMAND_PRIORITY,
            minimum=1,
            maximum=1000,
        ),
        managed_source_analysis_batch_size=_bounded_int_env(
            "MANAGED_SOURCE_ANALYSIS_BATCH_SIZE",
            DEFAULT_MANAGED_SOURCE_ANALYSIS_BATCH_SIZE,
            minimum=1,
            maximum=200,
        ),
        managed_source_libreoffice_concurrency=_bounded_int_env(
            "MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY",
            DEFAULT_MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY,
            minimum=1,
            maximum=8,
        ),
        managed_source_search_enabled=os.getenv(
            "MANAGED_SOURCE_SEARCH_ENABLED", "true"
        ).lower() == "true",
        materialize_all_managed_files=os.getenv(
            "MATERIALIZE_ALL_MANAGED_FILES", "true"
        ).lower() == "true",
        materialize_relevant_files_after_response=os.getenv(
            "MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE", "true"
        ).lower() == "true",
        materialize_working_copy_priority=_bounded_int_env(
            "MATERIALIZE_WORKING_COPY_PRIORITY",
            DEFAULT_MATERIALIZE_WORKING_COPY_PRIORITY,
            minimum=1,
            maximum=1000,
        ),
        materialize_working_copy_background_priority=_bounded_int_env(
            "MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY",
            DEFAULT_MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY,
            minimum=1,
            maximum=1000,
        ),
        materialize_relevant_files_batch_size=_bounded_int_env(
            "MATERIALIZE_RELEVANT_FILES_BATCH_SIZE",
            DEFAULT_MATERIALIZE_RELEVANT_FILES_BATCH_SIZE,
            minimum=1,
            maximum=500,
        ),
        retrieval_filename_trgm_min_chars=max(
            4, min(20, int(os.getenv("RETRIEVAL_FILENAME_TRGM_MIN_CHARS", "4")))
        ),
        retrieval_filename_trgm_candidate_limit=max(
            1, min(20, int(os.getenv("RETRIEVAL_FILENAME_TRGM_CANDIDATE_LIMIT", "20")))
        ),
        retrieval_filename_trgm_similarity_threshold=max(
            0.15, min(1.0, float(os.getenv("RETRIEVAL_FILENAME_TRGM_SIMILARITY_THRESHOLD", "0.25")))
        ),
        document_chunk_max_chars=max(
            200,
            min(8000, int(os.getenv("DOCUMENT_CHUNK_MAX_CHARS", str(DEFAULT_DOCUMENT_CHUNK_MAX_CHARS)))),
        ),
        document_chunk_overlap_chars=max(
            0,
            min(1000, int(os.getenv("DOCUMENT_CHUNK_OVERLAP_CHARS", str(DEFAULT_DOCUMENT_CHUNK_OVERLAP_CHARS)))),
        ),
        document_index_max_chars=max(
            1_000_000,
            min(500_000_000, int(os.getenv("DOCUMENT_INDEX_MAX_CHARS", str(DEFAULT_DOCUMENT_INDEX_MAX_CHARS)))),
        ),
        document_index_max_chunks=max(
            1_000,
            min(500_000, int(os.getenv("DOCUMENT_INDEX_MAX_CHUNKS", str(DEFAULT_DOCUMENT_INDEX_MAX_CHUNKS)))),
        ),
        embedding_enabled=os.getenv("EMBEDDING_ENABLED", "false").lower() == "true",
        embedding_provider=_choice(
            os.getenv("EMBEDDING_PROVIDER", "disabled"),
            allowed={"disabled", "openai_compatible", "local_service"},
            default="disabled",
        ),
        log_dir=os.getenv("LOG_DIR", DEFAULT_LOG_DIR),
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", str(DEFAULT_LOG_RETENTION_DAYS))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        ocr_enabled=os.getenv("OCR_ENABLED", "true").lower() == "true",
        ocr_provider=_choice(
            os.getenv("OCR_PROVIDER", DEFAULT_OCR_PROVIDER),
            allowed={"paddleocr_cpu", "tencent_cloud"},
            default=DEFAULT_OCR_PROVIDER,
        ),
        ocr_external_content_authorized=os.getenv(
            "OCR_EXTERNAL_CONTENT_AUTHORIZED", "false"
        ).lower() == "true",
        tencent_cloud_ocr_secret_id=os.getenv("TENCENT_CLOUD_OCR_SECRET_ID", "").strip(),
        tencent_cloud_ocr_secret_key=os.getenv("TENCENT_CLOUD_OCR_SECRET_KEY", ""),
        tencent_cloud_ocr_region=os.getenv(
            "TENCENT_CLOUD_OCR_REGION", DEFAULT_TENCENT_CLOUD_OCR_REGION
        ).strip() or DEFAULT_TENCENT_CLOUD_OCR_REGION,
        tencent_cloud_ocr_endpoint=os.getenv(
            "TENCENT_CLOUD_OCR_ENDPOINT", DEFAULT_TENCENT_CLOUD_OCR_ENDPOINT
        ).strip() or DEFAULT_TENCENT_CLOUD_OCR_ENDPOINT,
        tencent_cloud_ocr_action=_choice(
            os.getenv("TENCENT_CLOUD_OCR_ACTION", DEFAULT_TENCENT_CLOUD_OCR_ACTION),
            allowed={"GeneralAccurateOCR"},
            default=DEFAULT_TENCENT_CLOUD_OCR_ACTION,
            normalize=lambda item: str(item).strip(),
        ),
        tencent_cloud_ocr_timeout_seconds=_bounded_int_env(
            "TENCENT_CLOUD_OCR_TIMEOUT_SECONDS",
            DEFAULT_TENCENT_CLOUD_OCR_TIMEOUT_SECONDS,
            minimum=1,
            maximum=300,
        ),
        tencent_cloud_ocr_max_retries=_bounded_int_env(
            "TENCENT_CLOUD_OCR_MAX_RETRIES",
            DEFAULT_TENCENT_CLOUD_OCR_MAX_RETRIES,
            minimum=0,
            maximum=5,
        ),
        tencent_cloud_ocr_max_qps=_bounded_int_env(
            "TENCENT_CLOUD_OCR_MAX_QPS",
            DEFAULT_TENCENT_CLOUD_OCR_MAX_QPS,
            minimum=1,
            maximum=20,
        ),
        tencent_cloud_ocr_max_image_bytes=_bounded_int_env(
            "TENCENT_CLOUD_OCR_MAX_IMAGE_BYTES",
            DEFAULT_TENCENT_CLOUD_OCR_MAX_IMAGE_BYTES,
            minimum=1_024,
            maximum=10 * 1024 * 1024,
        ),
        tencent_cloud_table_ocr_max_qps=_bounded_int_env(
            "TENCENT_CLOUD_TABLE_OCR_MAX_QPS",
            DEFAULT_TENCENT_CLOUD_TABLE_OCR_MAX_QPS,
            minimum=1,
            maximum=20,
        ),
        ocr_local_fallback_enabled=os.getenv(
            "OCR_LOCAL_FALLBACK_ENABLED", "false"
        ).lower() == "true",
        ocr_paddle_model_source=os.getenv("OCR_PADDLE_MODEL_SOURCE", DEFAULT_OCR_PADDLE_MODEL_SOURCE),
        ocr_llm_enabled=os.getenv("OCR_LLM_ENABLED", "false").lower() == "true",
        ocr_llm_fallback_quality_threshold=float(
            os.getenv(
                "OCR_LLM_FALLBACK_QUALITY_THRESHOLD",
                str(DEFAULT_OCR_LLM_FALLBACK_QUALITY_THRESHOLD),
            )
        ),
        pp_structure_enabled=os.getenv("PP_STRUCTURE_ENABLED", "false").lower() == "true",
        pp_structure_device=os.getenv("PP_STRUCTURE_DEVICE", "cpu").strip() or "cpu",
        pp_structure_pipeline_config=os.getenv(
            "PP_STRUCTURE_PIPELINE_CONFIG", "PP-StructureV3"
        ).strip()
        or "PP-StructureV3",
        pp_structure_model_source=os.getenv("PP_STRUCTURE_MODEL_SOURCE", "BOS").strip() or "BOS",
        pp_structure_text_detection_model=os.getenv(
            "PP_STRUCTURE_TEXT_DETECTION_MODEL",
            DEFAULT_PP_STRUCTURE_TEXT_DETECTION_MODEL,
        ).strip()
        or DEFAULT_PP_STRUCTURE_TEXT_DETECTION_MODEL,
        pp_structure_text_recognition_model=os.getenv(
            "PP_STRUCTURE_TEXT_RECOGNITION_MODEL",
            DEFAULT_PP_STRUCTURE_TEXT_RECOGNITION_MODEL,
        ).strip()
        or DEFAULT_PP_STRUCTURE_TEXT_RECOGNITION_MODEL,
        pp_structure_use_doc_preprocessor=os.getenv(
            "PP_STRUCTURE_USE_DOC_PREPROCESSOR", "true"
        ).lower()
        == "true",
        # CPU Worker 默认只加载字段抽取必需的版面与 OCR 模型；重型专项模型必须显式开启。
        pp_structure_use_table_recognition=os.getenv(
            "PP_STRUCTURE_USE_TABLE_RECOGNITION", "false"
        ).lower()
        == "true",
        pp_structure_use_formula_recognition=os.getenv(
            "PP_STRUCTURE_USE_FORMULA_RECOGNITION", "false"
        ).lower()
        == "true",
        pp_structure_use_chart_recognition=os.getenv(
            "PP_STRUCTURE_USE_CHART_RECOGNITION", "false"
        ).lower()
        == "true",
        pp_structure_use_seal_recognition=os.getenv(
            "PP_STRUCTURE_USE_SEAL_RECOGNITION", "false"
        ).lower()
        == "true",
        pp_structure_use_region_detection=os.getenv(
            "PP_STRUCTURE_USE_REGION_DETECTION", "true"
        ).lower()
        == "true",
        pp_structure_max_image_pixels=_bounded_int_env(
            "PP_STRUCTURE_MAX_IMAGE_PIXELS",
            DEFAULT_PP_STRUCTURE_MAX_IMAGE_PIXELS,
            minimum=1_000_000,
            maximum=200_000_000,
        ),
        pp_structure_max_pdf_pages=_bounded_int_env(
            "PP_STRUCTURE_MAX_PDF_PAGES",
            DEFAULT_PP_STRUCTURE_MAX_PDF_PAGES,
            minimum=1,
            maximum=500,
        ),
        structured_extraction_enabled=os.getenv(
            "STRUCTURED_EXTRACTION_ENABLED", "false"
        ).lower()
        == "true",
        structured_extraction_layout_provider=_choice(
            os.getenv("STRUCTURED_EXTRACTION_LAYOUT_PROVIDER", "pp_structure_v3"),
            allowed={"pp_structure_v3", "tencent_cloud_table", "disabled"},
            default="pp_structure_v3",
        ),
        structured_extraction_llm_provider=_choice(
            os.getenv("STRUCTURED_EXTRACTION_LLM_PROVIDER", "disabled"),
            allowed={"disabled", "openai_compatible"},
            default="disabled",
        ),
        structured_extraction_llm_base_url=os.getenv(
            "STRUCTURED_EXTRACTION_LLM_BASE_URL", ""
        ).strip(),
        structured_extraction_llm_api_key=os.getenv(
            "STRUCTURED_EXTRACTION_LLM_API_KEY", ""
        ),
        structured_extraction_llm_model=os.getenv(
            "STRUCTURED_EXTRACTION_LLM_MODEL", ""
        ).strip(),
        structured_extraction_llm_timeout_seconds=_bounded_int_env(
            "STRUCTURED_EXTRACTION_LLM_TIMEOUT_SECONDS", 180, minimum=10, maximum=600
        ),
        structured_extraction_task_timeout_seconds=_bounded_int_env(
            "STRUCTURED_EXTRACTION_TASK_TIMEOUT_SECONDS", 300, minimum=30, maximum=900
        ),
        structured_extraction_max_fields=_bounded_int_env(
            "STRUCTURED_EXTRACTION_MAX_FIELDS",
            DEFAULT_STRUCTURED_EXTRACTION_MAX_FIELDS,
            minimum=1,
            maximum=40,
        ),
        structured_extraction_max_retry_fields=_bounded_int_env(
            "STRUCTURED_EXTRACTION_MAX_RETRY_FIELDS",
            DEFAULT_STRUCTURED_EXTRACTION_MAX_RETRY_FIELDS,
            minimum=1,
            maximum=20,
        ),
        structured_extraction_max_records=_bounded_int_env(
            "STRUCTURED_EXTRACTION_MAX_RECORDS",
            DEFAULT_STRUCTURED_EXTRACTION_MAX_RECORDS,
            minimum=1,
            maximum=10_000,
        ),
        structured_extraction_prompt_version=os.getenv(
            "STRUCTURED_EXTRACTION_PROMPT_VERSION", "structured-extraction-v1"
        ).strip()
        or "structured-extraction-v1",
        structured_extraction_high_confidence=max(
            0.0,
            min(1.0, float(os.getenv("STRUCTURED_EXTRACTION_HIGH_CONFIDENCE", "0.85"))),
        ),
        structured_extraction_retry_confidence=max(
            0.0,
            min(1.0, float(os.getenv("STRUCTURED_EXTRACTION_RETRY_CONFIDENCE", "0.65"))),
        ),
        structured_extraction_external_images_authorized=os.getenv(
            "STRUCTURED_EXTRACTION_EXTERNAL_IMAGES_AUTHORIZED", "false"
        ).lower()
        == "true",
        structured_extraction_vision_provider=_choice(
            os.getenv("STRUCTURED_EXTRACTION_VISION_PROVIDER", "disabled"),
            allowed={"disabled", "paddleocr_vl"},
            default="disabled",
        ),
        structured_extraction_vision_crop_upscale=max(
            1.0,
            min(4.0, float(os.getenv("STRUCTURED_EXTRACTION_VISION_CROP_UPSCALE", "2.0"))),
        ),
        paddleocr_vl_pipeline_version=_choice(
            os.getenv("PADDLEOCR_VL_PIPELINE_VERSION", "v1.6"),
            allowed={"v1.6"},
            default="v1.6",
            normalize=lambda item: str(item).strip().lower(),
        ),
        paddleocr_vl_model_name=os.getenv(
            "PADDLEOCR_VL_MODEL_NAME", "PaddleOCR-VL-1.6-0.9B"
        ).strip()
        or "PaddleOCR-VL-1.6-0.9B",
        paddleocr_vl_backend=_choice(
            os.getenv("PADDLEOCR_VL_BACKEND", "native"),
            allowed={"native"},
            default="native",
        ),
        paddleocr_vl_device=os.getenv("PADDLEOCR_VL_DEVICE", "cpu").strip() or "cpu",
        paddleocr_vl_max_new_tokens=_bounded_int_env(
            "PADDLEOCR_VL_MAX_NEW_TOKENS", 4096, minimum=256, maximum=16384
        ),
        docling_enabled=os.getenv("DOCLING_ENABLED", "true").lower() == "true",
        docling_formats=tuple(
            item.strip().lower().lstrip(".")
            for item in os.getenv("DOCLING_FORMATS", ",".join(DEFAULT_DOCLING_FORMATS)).split(",")
            if item.strip()
        ),
        docling_ocr_enabled=os.getenv("DOCLING_OCR_ENABLED", "false").lower() == "true",
        legacy_office_conversion_enabled=os.getenv(
            "LEGACY_OFFICE_CONVERSION_ENABLED",
            "true",
        ).lower() == "true",
        legacy_office_converter=_choice(
            os.getenv("LEGACY_OFFICE_CONVERTER", "libreoffice"),
            allowed={"libreoffice"},
            default="libreoffice",
        ),
        libreoffice_executable=os.getenv("LIBREOFFICE_EXECUTABLE", "").strip(),
        legacy_office_conversion_timeout_seconds=max(
            1,
            int(
                os.getenv(
                    "LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS",
                    str(DEFAULT_LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS),
                )
            ),
        ),
        legacy_office_max_file_size_mb=max(
            1,
            int(os.getenv("LEGACY_OFFICE_MAX_FILE_SIZE_MB", str(DEFAULT_LEGACY_OFFICE_MAX_FILE_SIZE_MB))),
        ),
        legacy_office_derivative_dir=os.getenv(
            "LEGACY_OFFICE_DERIVATIVE_DIR",
            DEFAULT_LEGACY_OFFICE_DERIVATIVE_DIR,
        ).strip(),
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
        file_rename_llm_validation_enabled=os.getenv(
            "FILE_RENAME_LLM_VALIDATION_ENABLED", "false"
        ).lower() == "true",
        file_rename_llm_validation_mode=_choice(
            os.getenv("FILE_RENAME_LLM_VALIDATION_MODE", "risk_based"),
            allowed={"off", "risk_based", "all"},
            default="risk_based",
        ),
        file_rename_llm_validation_threshold=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "FILE_RENAME_LLM_VALIDATION_THRESHOLD",
                        str(DEFAULT_FILE_RENAME_LLM_VALIDATION_THRESHOLD),
                    )
                ),
            ),
        ),
        file_rename_llm_validation_timeout_seconds=max(
            1,
            int(
                os.getenv(
                    "FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS",
                    str(DEFAULT_FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS),
                )
            ),
        ),
        file_rename_llm_validation_max_items_per_batch=max(
            0,
            int(
                os.getenv(
                    "FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH",
                    str(DEFAULT_FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH),
                )
            ),
        ),
        file_rename_llm_validation_prompt_version=os.getenv(
            "FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION",
            DEFAULT_FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION,
        ).strip() or DEFAULT_FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION,
        f2_binary_path=os.getenv("F2_BINARY_PATH", "f2"),
        f2_expected_version=os.getenv("F2_EXPECTED_VERSION", DEFAULT_F2_EXPECTED_VERSION),
        f2_fallback_to_native=os.getenv("F2_FALLBACK_TO_NATIVE", "false").lower() == "true",
        f2_stdout_max_bytes=int(os.getenv("F2_STDOUT_MAX_BYTES", str(DEFAULT_F2_STDOUT_MAX_BYTES))),
        graph_classification_enabled=os.getenv("GRAPH_CLASSIFICATION_ENABLED", "true").lower() == "true",
        neo4j_uri=os.getenv("NEO4J_URI", "").strip(),
        neo4j_username=os.getenv("NEO4J_USERNAME", "").strip(),
        neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
        neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j").strip() or "neo4j",
        neo4j_query_timeout_seconds=max(
            1,
            int(os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", str(DEFAULT_NEO4J_QUERY_TIMEOUT_SECONDS))),
        ),
        neo4j_sync_enabled=os.getenv("NEO4J_SYNC_ENABLED", "true").lower() == "true",
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
        graph_embedding_enabled=os.getenv("GRAPH_EMBEDDING_ENABLED", "true").lower() == "true",
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
        graph_projection_worker_enabled=os.getenv("GRAPH_PROJECTION_WORKER_ENABLED", "true").lower() == "true",
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
        managed_root_archive_write_path=os.getenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", "").strip(),
        managed_root_archive_enabled=os.getenv("MANAGED_ROOT_ARCHIVE_ENABLED", "true").lower() == "true",
        working_copy_storage_root=os.getenv("WORKING_COPY_STORAGE_ROOT", "./storage/working-copies").strip(),
        trash_storage_root=os.getenv("TRASH_STORAGE_ROOT", "./storage/trash").strip(),
        managed_root_watch_enabled=os.getenv("MANAGED_ROOT_WATCH_ENABLED", "true").lower() == "true",
        managed_root_reconcile_interval_seconds=max(
            30,
            int(
                os.getenv(
                    "MANAGED_ROOT_RECONCILE_INTERVAL_SECONDS",
                    str(DEFAULT_MANAGED_ROOT_RECONCILE_INTERVAL_SECONDS),
                )
            ),
        ),
        managed_root_reconcile_on_startup=os.getenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "true").lower() == "true",
        managed_root_scan_batch_size=max(
            1,
            min(
                1000,
                int(os.getenv("MANAGED_ROOT_SCAN_BATCH_SIZE", str(DEFAULT_MANAGED_ROOT_SCAN_BATCH_SIZE))),
            ),
        ),
        managed_root_scan_batch_max_seconds=max(
            1,
            min(
                60,
                int(
                    os.getenv(
                        "MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS",
                        str(DEFAULT_MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS),
                    )
                ),
            ),
        ),
        upload_archive_enabled=os.getenv("UPLOAD_ARCHIVE_ENABLED", "true").lower() == "true",
        upload_archive_retry_interval_seconds=max(
            30,
            int(os.getenv("UPLOAD_ARCHIVE_RETRY_INTERVAL_SECONDS", str(DEFAULT_UPLOAD_ARCHIVE_RETRY_INTERVAL_SECONDS))),
        ),
        upload_duplicate_check_enabled=os.getenv("UPLOAD_DUPLICATE_CHECK_ENABLED", "true").lower() == "true",
        upload_duplicate_similarity_threshold=max(
            0.0,
            min(
                1.0,
                float(
                    os.getenv(
                        "UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD",
                        str(DEFAULT_UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD),
                    )
                ),
            ),
        ),
        upload_duplicate_max_candidates=max(
            1,
            min(50, int(os.getenv("UPLOAD_DUPLICATE_MAX_CANDIDATES", str(DEFAULT_UPLOAD_DUPLICATE_MAX_CANDIDATES)))),
        ),
        upload_duplicate_confirmation_ttl_hours=max(
            1,
            int(
                os.getenv(
                    "UPLOAD_DUPLICATE_CONFIRMATION_TTL_HOURS",
                    str(DEFAULT_UPLOAD_DUPLICATE_CONFIRMATION_TTL_HOURS),
                )
            ),
        ),
        filesystem_async_jobs_enabled=os.getenv("FILESYSTEM_ASYNC_JOBS_ENABLED", "true").lower() == "true",
        filesystem_job_lease_seconds=max(
            30,
            int(os.getenv("FILESYSTEM_JOB_LEASE_SECONDS", str(DEFAULT_FILESYSTEM_JOB_LEASE_SECONDS))),
        ),
        archive_worker_concurrency=max(1, int(os.getenv("ARCHIVE_WORKER_CONCURRENCY", "2"))),
        import_worker_concurrency=max(1, int(os.getenv("IMPORT_WORKER_CONCURRENCY", "2"))),
        working_copy_import_batch_size=max(
            1,
            int(os.getenv("WORKING_COPY_IMPORT_BATCH_SIZE", str(DEFAULT_WORKING_COPY_IMPORT_BATCH_SIZE))),
        ),
        working_copy_operation_batch_size=max(
            1,
            int(os.getenv("WORKING_COPY_OPERATION_BATCH_SIZE", str(DEFAULT_WORKING_COPY_OPERATION_BATCH_SIZE))),
        ),
        trash_retention_days=max(1, int(os.getenv("TRASH_RETENTION_DAYS", str(DEFAULT_TRASH_RETENTION_DAYS)))),
        # MVP 明确禁止自动永久删除；即使误配 true 也保持 false，避免回收站绕过 OperationPlan。
        trash_auto_purge_enabled=False,
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
