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
默认构建源为阿里云 Debian/PyPI、npmmirror、hf-mirror.com 和百度 BOS；这些地址均可在 `deploy/.env`
覆盖。APT 更新会重试三次，并在任一索引下载失败时立即终止；pip 下载会重试十次并使用 120 秒超时，
避免短时网络抖动被误报为软件包不存在。若 Docker Hub 基础镜像拉取失败，还需在 Docker Desktop 的
Docker Engine 配置中填写单位批准的 registry mirror。镜像源属于供应链边界，正式上线前应由管理员确认。

Docling 使用固定 commit 的 Git LFS 下载，不使用容易被国内镜像 HEAD 元数据阻断的
`snapshot_download()`。模型下载写入持久 BuildKit 缓存，最终只在一个镜像层固化一次，避免 PaddleX
模型跨层复制使镜像虚增；失败重建会复用已完成的下载缓存。最终清单包含全部模型内容 SHA-256，任何
Git LFS 指针或依赖版本漂移都会让构建失败。

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

## 分层构建与快速代码更新

API 镜像分为 `file-agent-api-runtime-base` 基础镜像和 `file-agent-api-full-cpu` 代码镜像。基础镜像保存
系统包、Python 依赖和全部本地模型；代码镜像通过 BuildKit `COPY --link` 只生成独立源码层，避免重新
导出大型父镜像内容。执行同一条命令时，如果本机已经存在指定
基础镜像标签，脚本会跳过所有重量级步骤：

```powershell
.\deploy\build-layered-images.ps1 `
  -ImageTag "20260904-code1" `
  -BaseImageTag "20260904-v1" `
  -LocalModelCacheContext ".\data\build-model-cache"
```

`SeedApiImage` 可以指向包含完整依赖和模型清单的已验证旧镜像。转换使用多阶段构建：先删除旧 `/app`、
`/data`、临时缓存和可能的凭据文件，再通过 `FROM scratch` 复制清理后的合并文件系统；因此旧业务数据
不会像普通 whiteout 那样残留在父层和离线归档中。转换后脚本还会检查基础镜像的 `/app`、`/data` 不含
文件。若本地同时存在同标签旧 Web 镜像，脚本会用本机已有 `node_modules` 编译最新前端，再复用旧镜像
中的 Caddy 运行时生成新 Web 镜像，不查询或下载 Node/Caddy 基础镜像；也可用 `-SeedWebImage` 显式指定。
日常代码更新只修改 `ImageTag`，保持 `BaseImageTag` 不变。只有
`requirements*.txt`、系统包、模型版本或
`preload_models.py` 发生变化时，才递增 `BaseImageTag`；也可以显式传入 `-RebuildBase` 强制重建。
不要对普通代码更新使用 `-RebuildBase`，也不要执行 Docker builder/system prune。

`deploy.ps1` 和 `update.ps1` 已调用同一分层构建脚本。现有 `deploy/.env` 如果没有该字段，可以补充：

```dotenv
FILE_AGENT_BASE_IMAGE_TAG=20260904-v1
# 可填写已通过运行时及模型清单校验的完整 API 镜像；转换过程会平铺清理业务数据。
FILE_AGENT_BASE_SEED_IMAGE=
# 可填写已有 Web 镜像；留空时按照 API 种子镜像的标签自动推导。
FILE_AGENT_WEB_SEED_IMAGE=
```

## 制作与使用完整离线镜像包

Windows 新服务器从镜像导出、密钥初始化到验收、更新和备份的端到端步骤见
[`docs/windows-offline-docker-deployment-guide.md`](../docs/windows-offline-docker-deployment-guide.md)。

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

PP-StructureV3 和 PaddleOCR-VL 的下载目录使用持久 BuildKit 缓存；如果构建在后续模型或镜像导出阶段
失败，再次执行同一导出命令会复用已经下载的 PaddleX 模型。PaddleX 3.7.2 生成的
`PP-Chart2Table_safetensors` 与 `PaddleOCR-VL-1.6` 实际目录会在镜像内转换为运行时兼容布局，
不需要手工改名或重复下载。

脚本会先复用或构建基础镜像，再生成最新代码镜像，并与 PostgreSQL/pgvector、Neo4j 一起生成 Docker
归档、SHA-256 文件和清单。归档同时保存基础镜像标签，目标服务器导入一次后也能在本地快速构建后续
代码更新。将整个输出目录复制到目标服务器，然后执行：

```powershell
.\deploy\import-offline-images.ps1 `
  -ArchivePath C:\packages\file-agent-full-cpu-20260826.tar `
  -ImageTag "20260826" `
  -BaseImageTag "20260904-v1"
.\deploy\deploy.ps1 -SiteAddress :80 -OpenFirewall -UsePrebuiltImages
```

`-UsePrebuiltImages` 会禁止构建和拉取，确保实际使用刚导入的镜像。源码版本、`FILE_AGENT_IMAGE_TAG`
和离线包标签必须一致。

## 更新

只传输源码 ZIP、同时更新 API 和前端的完整操作步骤、参数和故障处理见
[`docs/windows-api-web-code-only-update-guide.md`](../docs/windows-api-web-code-only-update-guide.md)。

联网更新代码、重建镜像并重新执行 Alembic：

```powershell
.\deploy\update.ps1
```

使用源码 zip 更新：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip
```

仅修改后端 Python、规则或 taxonomy，且目标服务器已经存在当前
`file-agent-api-runtime-base`、`file-agent-web`、PostgreSQL 和 Neo4j 镜像时，不需要重新传输或导入
完整镜像 TAR。源码 ZIP 必须包含完整的 `apps/`、`deploy/`、`rules/` 和 `skills/`，但不得包含
`deploy/.env`、`data/`、模型、日志或 `node_modules`。在目标服务器项目根目录执行：

```powershell
.\deploy\update.ps1 `
  -PackageZip "E:\packages\file-agent-code-20260905-code5.zip" `
  -SkipInfrastructurePull `
  -SkipWeb
```

这条命令会保留现有 `deploy/.env` 和 `data/`，只同步源码，复用当前基础镜像重建 API 代码层，重新执行
数据库迁移并重启容器；`-SkipInfrastructurePull` 禁止访问镜像仓库，`-SkipWeb` 保留现有 Web 镜像。
不要添加 `-UsePrebuiltImages`，否则更新脚本会继续运行已有代码镜像，新源码不会进入容器。若前端代码也
有修改，则不能使用 `-SkipWeb`，应准备本地前端构建依赖及可复用 Web 镜像，或传输新的 Web 代码镜像。

后端和前端代码同时修改时，先在构建机用 `VITE_API_BASE_URL=/api` 执行 `npm run build`，并确保源码
ZIP 包含 `apps/web/dist/`。然后在目标服务器执行：

```powershell
.\deploy\update.ps1 `
  -PackageZip "E:\packages\file-agent-code-20260905-code5.zip" `
  -SkipInfrastructurePull `
  -UsePrebuiltWebDist
```

`-UsePrebuiltWebDist` 会复用目标服务器当前 `file-agent-web:<FILE_AGENT_IMAGE_TAG>` 中的 Caddy，只把
ZIP 中已经编译好的前端静态文件写入 Web 代码层，因此目标服务器不需要 npm/node_modules，也不会下载
Node 或 Caddy 镜像。

源码和完整镜像均已离线导入时：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip -UsePrebuiltImages
```

更新脚本保留 `deploy/.env` 与 `data/`，并强制重新创建一次性 `migrate` 容器。基础镜像已经导入且依赖、
模型没有变化时，可以在目标服务器从源码 zip 快速重建代码镜像；如果依赖或模型发生变化，则必须同时
提供新基础镜像。

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
