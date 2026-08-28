# File Agent Windows 11 全功能 CPU 部署包

本部署包面向以下正式基线：

- Windows 11，6 核 CPU，32GB 内存。
- Docker Desktop 使用 WSL2 Linux containers，建议分配 5 CPU、24GB 内存、8GB Swap。
- 受管目录固定从宿主机 `E:/workdata` 只读挂载到容器 `/managed/workdata`。
- LLM 使用外部 OpenAI-compatible 接口。
- 本地启用 PaddleOCR、PP-StructureV3 全部子能力、PaddleOCR-VL、Docling、LibreOffice 和 Neo4j。
- 模型在镜像构建阶段下载并固化，运行期不临时下载。

完整设计与资源说明见
[Windows 11 全功能 CPU Docker 部署方案](../docs/windows11-full-cpu-docker-deployment-plan.md)。

## 宿主机需要安装的软件

必须安装：

1. Windows 11 最新稳定更新。
2. WSL2，并在 BIOS/UEFI 中启用虚拟化。
3. Docker Desktop，启用 WSL2 后端与 Linux containers。
4. PowerShell 5.1 或 PowerShell 7。

宿主机不需要安装 Python、Node.js、LibreOffice、PostgreSQL、Neo4j、PaddleOCR 或模型 SDK；它们都在镜像内。
`Git` 仅在直接拉取代码更新时需要。

## 启动的服务

```text
gateway (Caddy + React)
api (FastAPI/LangGraph)
postgres (PostgreSQL 16 + pgvector)
neo4j (Neo4j 5.26 Community)
migrate (一次性 Alembic)
scheduler + watcher
reconcile-scan-worker
lifecycle-worker
source-analysis-worker
structured-extraction-worker
graph-worker
```

只对宿主机发布 80/443。PostgreSQL、Neo4j 和 API 端口只存在于内部 Docker 网络。

## 首次联网部署

1. 把项目放到服务器，例如 `C:\file-agent`。
2. 确认 `E:\workdata` 已存在，并在 Docker Desktop 中允许访问 E 盘。
3. 在项目根目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\deploy.ps1 -SiteAddress :80 -OpenFirewall
```

脚本第一次运行会生成 `deploy/.env`。打开该文件，至少填写：

```dotenv
LLM_BASE_URL=https://你的-openai-compatible-服务/v1
LLM_API_KEY=真实密钥
LLM_CHAT_MODEL=真实模型名
```

`STRUCTURED_EXTRACTION_LLM_BASE_URL/API_KEY/MODEL` 留空时会复用上述通用 LLM 配置；
`STRUCTURED_EXTRACTION_EXTERNAL_IMAGES_AUTHORIZED=false` 保证原始图片不发送给外部接口，图片辨识由本地
PaddleOCR/PP-StructureV3/PaddleOCR-VL 完成。填写后重新运行同一条命令。

首次完整构建需要下载系统包、Python 包和多个 CPU 模型，可能需要数小时并占用大量磁盘。构建成功后，
容器通过 `model-manifest.json` 校验所有模型，缺少任一必要模型就拒绝启动，避免生产任务临时联网下载。
默认构建源为清华 Debian/PyPI、npmmirror、hf-mirror.com 和百度 BOS；这些地址均可在 `deploy/.env`
覆盖。若 Docker Hub 基础镜像拉取失败，还需在 Docker Desktop 的 Docker Engine 配置中填写单位批准的
registry mirror。镜像源属于供应链边界，正式上线前应由管理员确认。

Docling 使用固定 commit 的 Git LFS 下载，不使用容易被国内镜像 HEAD 元数据阻断的
`snapshot_download()`。每类模型独立成层，失败重建会复用已经完成的模型层。最终清单包含全部模型内容
SHA-256，任何 Git LFS 指针或依赖版本漂移都会让构建失败。

域名 HTTPS 部署示例：

```powershell
.\deploy\deploy.ps1 -SiteAddress file-agent.example.com -OpenFirewall
```

还必须在外部完成 DNS A 记录、路由器 80/443 端口转发。`:80` 仅适合受信任局域网测试。

## 受管目录与全量工作副本同步

生产模板已经设置：

```dotenv
MANAGED_ROOT_HOST_PATH=E:/workdata
MANAGED_ROOT_WORKDATA=/managed/workdata
MANAGED_ROOT_WORKDATA_CLASSIFICATION_MODE=NONE
MANAGED_ROOT_VOLUME_MODE=ro
MANAGED_FILE_INITIALIZATION_MODE=source_index_first
MANAGED_SOURCE_ANALYSIS_ENABLED=true
MANAGED_SOURCE_SEARCH_ENABLED=true
MATERIALIZE_ALL_MANAGED_FILES=true
MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY=100
MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE=true
MATERIALIZE_WORKING_COPY_PRIORITY=20
```

工作副本和持久化 Office 派生件保存在项目 `data/uploads/` 对应的容器目录中；原始 `E:/workdata`
保持只读。要执行受管文件改名等写操作，必须同时改为 `rw`、显式开启对应
`MANAGED_ROOT_<KEY>_ALLOW_RENAME=true`，并仍然经过 OperationPlan 确认。

## 制作与使用完整离线镜像包

在能访问中国大陆互联网的同架构 Windows 机器上执行：

```powershell
.\deploy\export-offline-images.ps1 -OutputDirectory .\file-agent-offline-images
```

如果本机已经有 PaddleX 或 multilingual MiniLM 缓存，可先按白名单准备命名构建上下文：

```powershell
.\deploy\prepare-local-model-cache.ps1 -OutputDirectory .\data\build-model-cache
.\deploy\export-offline-images.ps1 `
  -OutputDirectory .\file-agent-offline-images `
  -LocalModelCacheContext .\data\build-model-cache
```

准备脚本只复制部署清单允许的模型目录，不会把整个用户缓存或其他本机文件送进 Docker。没有本地缓存时
无需执行准备脚本，导出脚本使用 `deploy/empty-model-cache`。

脚本会构建包含全部模型的 API/worker 镜像，拉取 PostgreSQL/pgvector 和 Neo4j，生成一个 Docker
归档、SHA-256 文件和清单。将整个输出目录复制到目标服务器，然后执行：

```powershell
.\deploy\import-offline-images.ps1 `
  -ArchivePath C:\packages\file-agent-full-cpu-20260826.tar
.\deploy\deploy.ps1 -SiteAddress :80 -OpenFirewall -UsePrebuiltImages
```

`-UsePrebuiltImages` 会禁止构建和拉取，确保实际使用刚导入的镜像。源码版本、`FILE_AGENT_IMAGE_TAG`
和离线包标签必须一致。

## 更新

联网更新代码、重建镜像并重新执行 Alembic：

```powershell
.\deploy\update.ps1
```

使用源码 zip 更新：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip
```

源码和完整镜像均已离线导入时：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip -UsePrebuiltImages
```

更新脚本保留 `deploy/.env` 与 `data/`，并强制重新创建一次性 `migrate` 容器。仅有源码 zip、没有包含
新依赖和新模型的对应镜像时，不能完成断网更新。

## 运维命令

```powershell
# 查看状态
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps

# 查看全部日志
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f

# 查看结构化抽取慢队列
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f structured-extraction-worker

# 验证依赖、模型清单和受管目录挂载
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml exec -T api `
  python /app/deploy/scripts/verify_runtime.py --managed-root

# 停止服务但不删除数据
.\deploy\stop.ps1

# 备份数据库；加 -IncludeUploads 同时备份上传及派生数据
.\deploy\backup.ps1 -IncludeUploads
```

## 安全约束

- `deploy/.env` 包含数据库密码、Neo4j 密码、JWT 密钥和 LLM 密钥，不得提交 Git。
- `E:/workdata` 默认只读；原件不被 OCR、转换、分类或结构化抽取覆盖。
- 外部图片发送默认关闭。若业务明确授权开启，必须同时确认外部服务的数据合规边界。
- 不要发布 5432、7474、7687 或 8000 端口。
- Neo4j 是可重建图投影，PostgreSQL 与 `data/uploads` 才是必须优先备份的业务事实和文件数据。
