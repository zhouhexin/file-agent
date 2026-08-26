"""Windows 11 全功能 CPU 部署包的静态契约测试。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEPLOY_ROOT = PROJECT_ROOT / "deploy"


def _read(relative_path: str) -> str:
    """以 UTF-8 读取部署资产。"""

    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _env_values() -> dict[str, str]:
    """解析不含 shell 展开的生产环境模板。"""

    values: dict[str, str] = {}
    for raw_line in _read("deploy/.env.production.example").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_compose_starts_unique_migration_and_all_required_workers() -> None:
    """生产拓扑必须覆盖全量同步、结构化抽取和图投影，且迁移只执行一次。"""

    compose = _read("deploy/docker-compose.production.yml")
    for service in (
        "postgres",
        "neo4j",
        "migrate",
        "api",
        "scheduler",
        "watcher",
        "reconcile-scan-worker",
        "lifecycle-worker",
        "source-analysis-worker",
        "materialize-worker",
        "analysis-worker",
        "structured-extraction-worker",
        "graph-worker",
        "gateway",
    ):
        assert f"  {service}:" in compose
    assert compose.count("APP_RUNTIME: migrate") == 1
    assert "condition: service_completed_successfully" in compose
    assert compose.count("FILESYSTEM_WORKER_ID: materialize-worker") == 1
    assert "FILESYSTEM_WORKER_QUEUES: STRUCTURED_EXTRACTION" in compose
    assert 'STRUCTURED_EXTRACTION_WORKER_CONCURRENCY: "1"' in compose
    assert "FILESYSTEM_WORKER_QUEUES: GRAPH" in compose
    assert '"${MANAGED_ROOT_HOST_PATH:-E:/workdata}:/managed/workdata:' in compose
    assert "5432:5432" not in compose
    assert "7687:7687" not in compose
    assert "8000:8000" not in compose
    api_block = compose.split("  api:", 1)[1].split("  scheduler:", 1)[0]
    assert "neo4j:" not in api_block


def test_production_env_enables_full_local_image_stack_and_managed_sync() -> None:
    """模板必须启用全部本地图片能力和 source-index-first 全量物化。"""

    env = _env_values()
    expected_true = {
        "MODEL_PRELOAD",
        "REQUIRE_PRELOADED_MODELS",
        "OCR_ENABLED",
        "PP_STRUCTURE_ENABLED",
        "PP_STRUCTURE_USE_DOC_PREPROCESSOR",
        "PP_STRUCTURE_USE_TABLE_RECOGNITION",
        "PP_STRUCTURE_USE_FORMULA_RECOGNITION",
        "PP_STRUCTURE_USE_CHART_RECOGNITION",
        "PP_STRUCTURE_USE_SEAL_RECOGNITION",
        "PP_STRUCTURE_USE_REGION_DETECTION",
        "STRUCTURED_EXTRACTION_ENABLED",
        "DOCLING_ENABLED",
        "DOCLING_OCR_ENABLED",
        "GRAPH_CLASSIFICATION_ENABLED",
        "NEO4J_SYNC_ENABLED",
        "GRAPH_EMBEDDING_ENABLED",
        "FILESYSTEM_ASYNC_JOBS_ENABLED",
        "MANAGED_ROOT_RECONCILE_ON_STARTUP",
        "MANAGED_SOURCE_ANALYSIS_ENABLED",
        "MANAGED_SOURCE_SEARCH_ENABLED",
        "MATERIALIZE_ALL_MANAGED_FILES",
        "MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE",
    }
    assert {key for key in expected_true if env.get(key) != "true"} == set()
    assert env["STRUCTURED_EXTRACTION_EXTERNAL_IMAGES_AUTHORIZED"] == "false"
    assert env["STRUCTURED_EXTRACTION_VISION_PROVIDER"] == "paddleocr_vl"
    assert env["PADDLEOCR_VL_DEVICE"] == "cpu"
    assert env["MANAGED_ROOT_HOST_PATH"] == "E:/workdata"
    assert env["MANAGED_ROOT_WORKDATA"] == "/managed/workdata"
    assert env["MANAGED_ROOT_VOLUME_MODE"] == "ro"
    assert env["MANAGED_ROOT_WORKDATA_CLASSIFICATION_MODE"] == "NONE"
    assert env["MANAGED_FILE_INITIALIZATION_MODE"] == "source_index_first"
    assert env["MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY"] == "100"
    assert env["MATERIALIZE_WORKING_COPY_PRIORITY"] == "20"


def test_api_image_installs_programs_and_preloads_every_required_model() -> None:
    """镜像必须包含旧 Office 转换和全图片能力，运行期保持离线。"""

    dockerfile = _read("deploy/Dockerfile.api")
    requirements = _read("deploy/requirements.full-cpu.txt")
    preloader = _read("deploy/scripts/preload_models.py")
    verifier = _read("deploy/scripts/verify_runtime.py")
    for package in ("libreoffice-writer", "libreoffice-calc", "fonts-noto-cjk"):
        assert package in dockerfile
    assert "paddlex[ocr]" in requirements
    assert "paddleocr[doc-parser]" in requirements
    assert "docling==2.120.3" in requirements
    assert "paddlex[ocr]==3.7.2" in requirements
    assert "paddleocr[doc-parser]==3.7.0" in requirements
    assert "mirrors.tuna.tsinghua.edu.cn/debian" in dockerfile
    assert "registry.npmmirror.com" in dockerfile
    assert "HF_ENDPOINT=https://hf-mirror.com" in dockerfile
    for component in (
        "docling",
        "paddleocr",
        "pp-structure",
        "paddleocr-vl",
        "embedding",
        "finalize",
    ):
        assert f"preload_models.py {component}" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert '"pp_structure_v3"' in preloader
    assert '"paddleocr_vl"' in preloader
    assert '"cpu_cores": 6' in preloader
    assert '"memory_gb": 32' in preloader
    assert "git-lfs" in dockerfile
    assert "from=local-model-cache" in dockerfile
    assert dockerfile.count("preload_models.py") >= 7
    assert "snapshot_download" not in preloader
    assert "DOCLING_REPOSITORIES" in preloader
    assert "_contains_lfs_pointer" in preloader
    assert "_aggregate_model_digest" in preloader
    assert "REQUIRED_MODEL_COMPONENTS" in verifier
    assert "shutil.which(\"soffice\")" in verifier


def test_runtime_entrypoint_and_management_scripts_fail_closed() -> None:
    """运行脚本必须校验模型、资源、目录、迁移和离线包哈希。"""

    entrypoint = _read("deploy/entrypoint.api.sh")
    deploy = _read("deploy/deploy.ps1")
    update = _read("deploy/update.ps1")
    exporter = _read("deploy/export-offline-images.ps1")
    importer = _read("deploy/import-offline-images.ps1")
    cache_preparer = _read("deploy/prepare-local-model-cache.ps1")
    assert entrypoint.count("alembic -c apps/api/alembic.ini upgrade head") == 1
    assert 'APP_RUNTIME="${APP_RUNTIME:-api}"' in entrypoint
    assert "verify_runtime.py --managed-root" in entrypoint
    assert "dockerCpuCount -lt 4" in deploy
    assert "dockerMemoryGb -lt 20" in deploy
    assert "MANAGED_ROOT_HOST_PATH" in deploy
    assert "LLM_BASE_URL" in deploy and "LLM_API_KEY" in deploy and "LLM_CHAT_MODEL" in deploy
    assert "-UsePrebuiltImages" in _read("deploy/README.md")
    assert "service_completed_successfully" in _read("deploy/docker-compose.production.yml")
    assert "rm -f migrate" in update
    assert 'if ($health -eq "healthy")' in update
    assert "MODEL_PRELOAD=true" in exporter
    assert "docker save" in exporter
    assert "Get-FileHash" in exporter
    assert "docker load" in importer
    assert "SHA-256 verification failed" in importer
    assert "local-model-cache=" in exporter
    assert "AllowedPaddleModels" in cache_preparer


def test_deployment_plan_records_the_actual_server_baseline() -> None:
    """正式方案不能丢失用户指定的硬件和业务边界。"""

    plan = _read("docs/windows11-full-cpu-docker-deployment-plan.md")
    assert "Windows 11、6 核 CPU、32GB 内存" in plan
    assert "E:/workdata" in plan
    assert "外部 OpenAI-compatible" in plan
    assert "PP-StructureV3 全部子能力" in plan
    assert "PaddleOCR-VL" in plan
