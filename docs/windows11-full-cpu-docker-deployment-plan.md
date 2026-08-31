# Windows 11 全功能 CPU Docker 部署方案

> 制定日期：2026-08-26
> 目标主机：Windows 11、6 核 CPU、32GB 内存
> 受管目录：`E:/workdata`
> 模型策略：中国大陆联网构建、模型随镜像固化、运行期离线复用
> LLM：外部 OpenAI-compatible 接口
> 图片能力：启用 PaddleOCR、PP-StructureV3 全部子能力和 PaddleOCR-VL

## 1. 目标与约束

本方案把宿主机职责限制为 Docker Desktop、WSL2、磁盘挂载和网络入口。Python、Node.js、
LibreOffice、PostgreSQL、Neo4j、OCR/Docling/Embedding 依赖和模型全部进入 Linux 容器镜像，避免在
Windows 宿主机维护多套运行环境。

6 核、32GB 属于“可以完整启用、但必须严格限并发”的配置。部署基线如下：

- Docker Desktop 使用 WSL2 Linux containers，建议分配 5 个 CPU、24GB 内存和不少于 150GB SSD。
- 结构化抽取只启动一个 worker，同一进程并发固定为 1。
- SOURCE_ANALYSIS 与 ANALYSIS 共用一个文档分析 worker；MATERIALIZE、IMPORT 与文件生命周期队列共用
  一个 I/O worker；GRAPH 保持独立。
- PaddleOCR-VL 只在本地 OCR/PP-StructureV3 仍存在字段缺失或低置信度时运行，不作为每张图片首选路径。
- PP-StructureV3 的表格、公式、图表、印章、区域检测和文档预处理全部安装并启用，但由专用慢队列执行。
- 外部 LLM 默认只接收任务文本、OCR 文本和字段映射请求；原始图片外发保持关闭，除非管理员另行授权。

## 2. 服务拓扑

```text
浏览器
  -> gateway (Caddy + React)
  -> api (FastAPI/LangGraph)
       -> PostgreSQL 16 + pgvector
       -> Neo4j 5.26 Community
       -> 外部 OpenAI-compatible LLM

migrate（一次性 Alembic）
scheduler
watcher
reconcile-scan-worker
lifecycle-worker
source-analysis-worker
structured-extraction-worker
graph-worker
```

所有 Python 服务共享 `file-agent-api-full-cpu` 镜像，通过 `APP_RUNTIME` 和
`FILESYSTEM_WORKER_QUEUES` 隔离职责。五个常驻 worker 分别负责扫描、文件生命周期与物化、文档分析、
结构化抽取和图谱投影。数据库迁移只能由一次性 `migrate` 服务执行；API 和 worker
不得在并发启动时重复迁移。Neo4j 与 `graph-worker` 故障时不得阻断 API、扫描、源侧分析和工作副本同步；
图谱链路恢复后再继续消费可重建投影任务。

## 3. 宿主机软件清单

必须安装：

- Windows 11 最新稳定更新。
- BIOS/UEFI 虚拟化能力。
- WSL2。
- Docker Desktop，启用 WSL2 后端和 Linux containers。
- PowerShell 5.1 或 PowerShell 7。

可选安装：Git、7-Zip。宿主机不安装 Python、Node.js、LibreOffice、PostgreSQL、Neo4j 或模型 SDK。

## 4. 镜像内容

API/worker 镜像包含：

- Python 3.11。
- LibreOffice Writer/Calc。
- `fonts-noto-cjk`、`fontconfig`、`libgl1`、`libglib2.0-0`、`libgomp1`。
- Filesystem MCP Server 及 Node.js 运行时。
- 项目基础依赖、Neo4j/embedding 依赖、`paddlex[ocr]` 和 `paddleocr[doc-parser]`。
- PaddleOCR 中文 OCR、Docling、PP-StructureV3、PaddleOCR-VL 和 384 维文档 embedding 模型。

构建阶段默认使用清华 Debian/PyPI 镜像、npmmirror、`hf-mirror.com`、ModelScope 和
`PADDLE_PDX_MODEL_SOURCE=BOS`，所有下载源均为可覆盖构建参数。生产管理员应按本单位供应链策略验证或
替换镜像源；Docker Hub 基础镜像若无法直接拉取，还需在 Docker Desktop Engine 中配置单位批准的
registry mirror。PaddleX 缓存固定到
`/opt/file-agent/models/paddlex`。Docling 模型固定到 `/opt/file-agent/models/docling`，Hugging Face
缓存固定到 `/opt/file-agent/models/huggingface`，embedding 模型保存到
`/opt/file-agent/models/document-embedding`。

Docling 不再使用 `huggingface_hub.snapshot_download()`，因为部分国内镜像不能稳定提供其要求的 HEAD
元数据。构建脚本改用 `hf-mirror.com` 的 Git/Git LFS 通道，按固定 commit SHA 下载五个仓库，并扫描
所有文件，发现 Git LFS 指针、空仓库或不可读模型即终止构建：

```text
docling-project/docling-layout-heron@8f39ad3c...
docling-project/docling-layout-heron-onnx@40bde044...
docling-project/docling-models@fc0f2d45...       # TableFormer v2.3.0
docling-project/DocumentFigureClassifier-v2.5@f859dfbf...
docling-project/CodeFormulaV2@ecedbe11...
```

RapidOCR 固定由 Docling 2.120.3 从 ModelScope 下载。生产镜像同时固定 Docling、Hugging Face Hub、
PaddlePaddle、PaddleOCR、PaddleX、sentence-transformers、Neo4j driver 和 Neo4j GraphRAG 版本。

模型预下载完成后生成 `model-manifest.json`，其中保存固定依赖版本、逐组件来源、文件数量、总字节数和
全模型内容 SHA-256。所有运行服务先执行轻量依赖与模型清单检查；清单缺失时
拒绝启动重量级 worker，避免生产任务触发不可控的临时下载。

Dockerfile 把 Docling、PaddleOCR、PP-StructureV3、PaddleOCR-VL、Embedding 分成独立构建层；已成功
组件可被 BuildKit 缓存复用。Docling 的 Git/LFS 下载目录使用 BuildKit cache mount，网络中断后保留
已到达的 LFS 对象并有限重试。

PP-StructureV3 与 PaddleOCR-VL 共用 `/var/cache/file-agent-paddlex-models` BuildKit cache mount。
PaddleX 下载成功的模型先保留在该持久缓存中，通过完整性校验后再复制进最终镜像层；后续阶段失败时，
重建可直接复用已下载模型。PaddleX 3.7.2 的图表模型实际目录为 `PP-Chart2Table_safetensors`，
PaddleOCR-VL 实际目录为 `PaddleOCR-VL-1.6`；镜像同时提供兼容目录
`PaddleOCR-VL-1.6-0.9B`，避免运行时配置名与真实缓存目录不一致。

可选本地缓存只能通过命名 BuildKit context 输入。`prepare-local-model-cache.ps1` 仅复制生产模型白名单
内的 PaddleX 目录、指定 sentence-transformers Hugging Face 缓存或显式 Document Embedding 目录；
其他本机文件不会进入构建上下文。

## 5. 数据与挂载

```text
项目根/data/uploads   -> /data/uploads
项目根/data/logs      -> /data/logs
项目根/data/backups   -> Windows 备份目录
E:/workdata           -> /managed/workdata:ro
```

生产环境必须配置：

```dotenv
MANAGED_ROOT_HOST_PATH=E:/workdata
MANAGED_ROOT_WORKDATA=/managed/workdata
MANAGED_ROOT_WORKDATA_CLASSIFICATION_MODE=NONE
MANAGED_ROOT_WORKDATA_ALLOW_RENAME=false
MANAGED_ROOT_VOLUME_MODE=ro
```

数据库、Caddy 和 Neo4j 使用 Docker named volume。PostgreSQL 是业务事实源；Neo4j 是可重建投影。

## 6. 资源与并发

推荐 Docker Desktop 资源：

```text
CPU: 5
Memory: 24GB
Swap: 8GB
Disk image limit: >= 150GB
```

运行限制：

- `PADDLE_PDX_CPU_NUM_THREADS=4`，避免单模型占满所有宿主机核心。
- `OMP_NUM_THREADS=4`、`MKL_NUM_THREADS=4`。
- `STRUCTURED_EXTRACTION_WORKER_CONCURRENCY=1`。
- `MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY=1`。
- Neo4j heap 512MB～2GB、page cache 512MB。
- PostgreSQL 和 Neo4j 不向宿主机公开端口；外部只开放 80/443。

PaddleOCR-VL 在 CPU 上可能单页耗时几十秒到数分钟。该耗时属于硬件约束，不应通过增加并发规避；
增加并发会导致 32GB 内存主机发生换页或 OOM。

## 7. 外部 LLM 边界

配置 `LLM_ENABLED=true` 和 OpenAI-compatible 地址、模型、密钥。结构化字段映射复用独立
`STRUCTURED_EXTRACTION_LLM_*` 配置。默认保持：

```dotenv
OCR_LLM_ENABLED=false
STRUCTURED_EXTRACTION_EXTERNAL_IMAGES_AUTHORIZED=false
```

这表示原始图片不发送给外部服务；本地 OCR/VLM 生成的文本可供外部 LLM 做受控字段映射。若未来允许
图片外发，必须由管理员显式修改授权配置并完成数据合规评审。

## 8. 部署与更新流程

首次联网部署：

1. `deploy.ps1` 检查 Docker、CPU、Docker 可用内存、外部 LLM 必填项和 `E:/workdata`；管理员另行确认
   Docker 磁盘镜像上限与 80/443 端口可用。
2. 生成 PostgreSQL、Neo4j、JWT 密钥和部署环境文件。
3. 构建完整 CPU 镜像并预下载模型。
4. 启动 PostgreSQL、Neo4j。
5. `migrate` 单独升级到唯一 Alembic head。
6. 启动 API、调度器、watcher、分队列 worker 和网关。
7. 检查 API、数据库、图数据库、LibreOffice、模型清单和挂载目录。

离线迁移到另一台机器：

1. 在联网机器执行 `export-offline-images.ps1`。
2. 复制镜像 tar、SHA-256 文件、源码/Compose 和未提交密钥的环境模板。
3. 目标机器执行 `import-offline-images.ps1` 校验并加载镜像。
4. 使用 `deploy.ps1 -UsePrebuiltImages` 启动，不重新下载依赖或模型。

如需复用本机已有模型，先执行：

```powershell
.\deploy\prepare-local-model-cache.ps1 -OutputDirectory .\data\build-model-cache
.\deploy\export-offline-images.ps1 `
  -LocalModelCacheContext .\data\build-model-cache
```

普通代码更新继续复用模型镜像层；只有依赖、模型版本或预下载脚本变化时才重新下载重量级模型。

## 9. 验收标准

- Compose 配置包含唯一迁移服务和全部运行服务。
- `E:/workdata` 在容器内唯一解析为 `/managed/workdata`，默认只读。
- Alembic head 为 `20260826_0001` 或部署时仓库的更新唯一 head。
- LibreOffice 能生成 `CONVERTED_DOCX` 和 `CONVERTED_XLSX`。
- PaddleOCR、Docling、PP-StructureV3 全子能力、PaddleOCR-VL、sentence-transformers 均能离线初始化。
- 结构化 worker 并发为 1，6 核/32GB 主机不会因启动多个重量级模型 worker 发生 OOM。
- PostgreSQL、Neo4j、API 和 worker 重启后数据、任务和派生件保持可恢复。
- 外部仅开放 80/443；数据库、Bolt 和 API 内部端口不暴露。
- 完成图片、扫描 PDF、DOC/DOCX、XLS/XLSX、受管目录同步、检索、图谱和外部 LLM smoke test。

## 10. 回滚

- 更新前执行 PostgreSQL 与 `data/uploads` 备份。
- 旧镜像保留一个稳定 tag；失败时把 `FILE_AGENT_IMAGE_TAG` 切回旧 tag。
- Alembic 结构回滚必须单独评审；默认保留新增派生件和版本事实，不自动删除业务数据。
- Neo4j 可从 PostgreSQL Outbox 和现有文件事实重新投影，不以图数据库替代业务备份。
