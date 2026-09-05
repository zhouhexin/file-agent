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
        "structured-extraction-worker",
        "graph-worker",
        "gateway",
    ):
        assert f"  {service}:" in compose
    assert compose.count("APP_RUNTIME: migrate") == 1
    assert "condition: service_completed_successfully" in compose
    assert (
        "FILESYSTEM_WORKER_QUEUES: "
        "DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT"
    ) in compose
    assert "FILESYSTEM_WORKER_QUEUES: SOURCE_ANALYSIS,ANALYSIS" in compose
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

    dockerfile = _read("deploy/Dockerfile.api-base")
    code_dockerfile = _read("deploy/Dockerfile.api")
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
    assert "mirrors.aliyun.com/debian" in dockerfile
    assert "Acquire::Retries=3" in dockerfile
    assert "APT::Update::Error-Mode=any" in dockerfile
    # 国内构建入口必须使用已在 Docker 网络中验证过的源，并容忍短时网络抖动。
    assert "mirrors.aliyun.com/pypi/simple" in dockerfile
    assert "--retries 10 --timeout 120" in dockerfile
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
    assert "/var/cache/file-agent-paddlex-models" in dockerfile
    assert "PaddleOCR-VL-1.6-0.9B" in dockerfile
    assert "ln -s PaddleOCR-VL-1.6" in dockerfile
    # VL 初始化新增的布局模型必须通过整目录缓存挂载参与校验并同步回最终镜像。
    assert "ln -s /var/cache/file-agent-paddlex-models/official_models /opt/file-agent/models/paddlex/official_models" in dockerfile
    assert "cp -a /var/cache/file-agent-paddlex-models/official_models/. /opt/file-agent/models/paddlex/official_models/" in dockerfile
    assert dockerfile.count("preload_models.py") >= 7
    assert "snapshot_download" not in preloader
    assert "DOCLING_REPOSITORIES" in preloader
    assert "_contains_lfs_pointer" in preloader
    assert 'DOWNLOAD_CACHE_ROOT / "docling-tools"' in preloader
    assert 'LOCAL_CACHE_ROOT / "docling" / "RapidOcr"' in preloader
    assert "RAPIDOCR_REQUIRED_FILES" in preloader
    assert "_aggregate_model_digest" in preloader
    assert "REQUIRED_MODEL_COMPONENTS" in verifier
    assert "shutil.which(\"soffice\")" in verifier
    # 日常代码镜像必须从固定基础镜像派生，不能再次执行依赖安装和模型预加载。
    assert "ARG API_RUNTIME_BASE_IMAGE=" in code_dockerfile
    assert "FROM ${API_RUNTIME_BASE_IMAGE}" in code_dockerfile
    assert "COPY --link . /app" in code_dockerfile
    assert "COPY --link deploy/entrypoint.api.sh" in code_dockerfile
    assert "sed -i 's/\\r$//' /app/deploy/entrypoint.api.sh" in code_dockerfile
    assert "chmod 755 /app/deploy/entrypoint.api.sh" in code_dockerfile
    assert "pip install" not in code_dockerfile
    assert "preload_models.py" not in code_dockerfile
    assert "RUN rm -rf /app" in dockerfile


def test_code_image_context_excludes_generated_offline_archives() -> None:
    """代码层构建不得复制离线归档、上传文件或运行日志。"""

    dockerignore = _read(".dockerignore")
    assert "file-agent-offline-images/" in dockerignore
    assert "file-agent-offline-images-*/" in dockerignore
    assert "apps/api/storage/" in dockerignore
    assert "apps/api/logs/" in dockerignore
    assert "*.tar" in dockerignore
    assert "*.tar.sha256" in dockerignore


def test_runtime_entrypoint_and_management_scripts_fail_closed() -> None:
    """运行脚本必须校验模型、资源、目录、迁移和离线包哈希。"""

    entrypoint = _read("deploy/entrypoint.api.sh")
    deploy = _read("deploy/deploy.ps1")
    update = _read("deploy/update.ps1")
    exporter = _read("deploy/export-offline-images.ps1")
    importer = _read("deploy/import-offline-images.ps1")
    cache_preparer = _read("deploy/prepare-local-model-cache.ps1")
    layered_builder = _read("deploy/build-layered-images.ps1")
    reusable_base = _read("deploy/Dockerfile.api-base-reuse")
    reusable_web = _read("deploy/Dockerfile.web-reuse")
    assert entrypoint.count("alembic -c apps/api/alembic.ini upgrade head") == 1
    assert 'APP_RUNTIME="${APP_RUNTIME:-api}"' in entrypoint
    assert "verify_runtime.py --managed-root" in entrypoint
    assert "dockerCpuCount -lt 4" in deploy
    assert "dockerMemoryGb -lt 20" in deploy
    assert "Docker Compose 启动失败" in deploy
    assert "MANAGED_ROOT_HOST_PATH" in deploy
    assert "LLM_BASE_URL" in deploy and "LLM_API_KEY" in deploy and "LLM_CHAT_MODEL" in deploy
    assert "-UsePrebuiltImages" in _read("deploy/README.md")
    assert "service_completed_successfully" in _read("deploy/docker-compose.production.yml")
    assert "rm -f migrate" in update
    assert 'if ($health -eq "healthy")' in update
    assert "Docker Compose 更新启动失败" in update
    assert "build-layered-images.ps1" in exporter
    assert "MODEL_PRELOAD=true" in layered_builder
    assert 'docker image ls --quiet --filter "reference=$Image"' in layered_builder
    assert "if ($listExitCode -ne 0)" in layered_builder
    assert "if ($RebuildBase -or -not $baseExists)" in layered_builder
    assert "Dockerfile.api-base" in layered_builder
    assert "Dockerfile.api-base-reuse" in layered_builder
    assert "SEED_API_IMAGE=$SeedApiImage" in layered_builder
    assert "Assert-SeedImageHasReusableRuntime $SeedApiImage" in layered_builder
    for manifest_component in (
        "pp_structure_v3",
        "paddleocr_vl",
        "document_embedding",
    ):
        assert manifest_component in layered_builder
    assert "Assert-ImageHasNoRuntimeData $BaseImage" in layered_builder
    assert "FROM scratch" in reusable_base
    assert "COPY --from=sanitized-seed / /" in reusable_base
    assert "/app" in reusable_base and "/data" in reusable_base
    assert "API_RUNTIME_BASE_IMAGE=$BaseImage" in layered_builder
    assert "FILE_AGENT_WEB_SEED_IMAGE" in layered_builder
    assert "npm run build" in layered_builder
    assert 'web-dist=$dockerWebDistContext' in layered_builder
    assert "Dockerfile.web-reuse" in layered_builder
    assert "FROM ${SEED_WEB_IMAGE}" in reusable_web
    assert "COPY --from=web-dist / /srv" in reusable_web
    assert "docker save" in exporter
    assert "Get-FileHash" in exporter
    assert "docker load" in importer
    assert '"file-agent-api-runtime-base:$BaseImageTag"' in exporter
    assert '"file-agent-api-runtime-base:$BaseImageTag"' in importer
    assert "SHA-256 verification failed" in importer
    assert "local-model-cache=" in layered_builder
    assert "AllowedPaddleModels" in cache_preparer
    assert "RapidOcrPath" in cache_preparer
    assert 'docling/RapidOcr' in cache_preparer


def test_deployment_plan_records_the_actual_server_baseline() -> None:
    """正式方案不能丢失用户指定的硬件和业务边界。"""

    plan = _read("docs/windows11-full-cpu-docker-deployment-plan.md")
    assert "Windows 11、6 核 CPU、32GB 内存" in plan
    assert "E:/workdata" in plan
    assert "外部 OpenAI-compatible" in plan
    assert "PP-StructureV3 全部子能力" in plan
    assert "PaddleOCR-VL" in plan
