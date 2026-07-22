# File Agent Windows 一键部署包

把整个 `deploy/` 文件夹复制到 File Agent 项目根目录。

## 会启动什么

```text
浏览器
  → Caddy（仅对外开放 80 / 443）
  → FastAPI（容器内 8000）
  → filesystem-worker（容器内，消费 managed root 扫描任务）
  → PostgreSQL + pgvector（容器内）
```

持久化数据在项目根目录：

```text
data/uploads
data/logs
data/backups
data/managed
```

## 新 Windows 电脑的最短部署流程

1. 安装并启动 Docker Desktop，启用 WSL 2 后端。
2. 将项目克隆或复制到电脑上，例如 `C:\file-agent`。
3. 将本部署包中的 `deploy/` 文件夹放进 `C:\file-agent\deploy\`。
4. 在项目根目录 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\deploy.ps1 -SiteAddress file-agent.example.com -OpenFirewall
```

5. 打开站点地址，用户可在登录页选择“申请注册”自行创建账号。

## 服务器受管目录配置

如果要启用“服务器受管文件”扫描，需要先准备宿主机目录，例如：

```text
data/managed/student-affairs
```

当前部署模板内置了一个示例挂载：

```text
宿主机 ../data/managed/student-affairs
→ 容器内 /managed/student-affairs
→ root_key = student_affairs
```

对应环境变量在 `deploy/.env` 中填写：

```text
MANAGED_ROOT_STUDENT_AFFAIRS=/managed/student-affairs
```

该 root 会在用户通过对话或 API 查询时自动生效：系统会按 env 配置自动同步
`managed_roots`，扫描并更新 `managed_files` 索引，不需要管理员再通过 API 启用。
后续如果要增加更多受管目录，需要同时修改：

1. `deploy/docker-compose.production.yml` 中的只读 volume mount。
2. `deploy/.env` 中的 `MANAGED_ROOT_<ROOT_KEY>` 环境变量。

受管目录默认分类模式为 `NONE`：

```text
MANAGED_ROOT_STUDENT_AFFAIRS=/managed/student-affairs
```

普通受管目录，只扫描、列出、搜索文件，不认为父目录代表分类。

如果该目录本身已经按父目录分好类，可以在 env 中额外声明：

```text
MANAGED_ROOT_STUDENT_AFFAIRS_CLASSIFICATION_MODE=PATH_AS_CATEGORY
```

已分类文件库，文件父目录会作为分类路径。例如：

```text
奖学金/国家励志奖学金/a.pdf
```

对应分类路径是：

```text
奖学金/国家励志奖学金
```

受管目录默认只读。要启用“生成计划 -> 用户确认 -> 受控重命名”，必须同时显式配置：

```text
MANAGED_ROOT_WORKDATA_ALLOW_RENAME=true
MANAGED_ROOT_VOLUME_MODE=rw
```

未设置 `ALLOW_RENAME=true` 时，即使容器挂载可写，后端也会拒绝生成可执行重命名项。
启用前应备份宿主机目录；上传原件不受该开关影响，仍禁止直接改名。

重命名执行器默认使用 Python 内置 Native 实现：

```text
FILE_RENAME_EXECUTOR=native
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
```

F2 是可选批量执行器，不负责读取正文或生成文件名。离线部署包需要预先携带固定版本
F2 v2.2.2，并设置：

```text
FILE_RENAME_EXECUTOR=f2
F2_BINARY_PATH=/opt/file-agent/bin/f2
F2_EXPECTED_VERSION=2.2.2
F2_FALLBACK_TO_NATIVE=false
F2_STDOUT_MAX_BYTES=1048576
```

部署前必须在目标服务器执行 `f2 --version` 并按离线包清单校验 SHA-256。配置为
`f2` 时，二进制缺失或版本不匹配会拒绝执行，不会在用户不知情时改用 Native。需要回退时应
显式把 `FILE_RENAME_EXECUTOR` 改为 `native` 后重启 API。运行时不得联网下载 F2。

后续新上传文件归档时，只能把 `PATH_AS_CATEGORY` 目录作为目标分类库；`NONE` 目录不会被当成分类体系。

## Filesystem MCP 只读实时查询

受管目录索引适合常规查询。对于需要实时读取服务器目录状态的场景，可以启用
Filesystem MCP 只读桥接。第一阶段只允许列目录、搜索文件和读取文件元信息，不读取正文，
不执行创建、改名、移动、删除等写操作。

部署模板已经在 API 镜像中安装 `@modelcontextprotocol/server-filesystem`，并预留以下环境变量：

```text
MCP_FILESYSTEM_ENABLED=false
MCP_FILESYSTEM_ROOT=/managed/workdata
MCP_FILESYSTEM_COMMAND=/usr/local/bin/mcp-server-filesystem
MCP_FILESYSTEM_TIMEOUT_SECONDS=45
MCP_FILESYSTEM_MAX_OUTPUT_CHARS=50000
```

启用时需要：

1. 在 `deploy/docker-compose.production.yml` 中把目标宿主机目录只读挂载到
   `MCP_FILESYSTEM_ROOT` 对应的容器路径。
2. 在 `deploy/.env` 中设置 `MCP_FILESYSTEM_ENABLED=true`。
3. 重新构建并启动 API 容器。

可用下面命令确认容器内依赖是否可用：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml exec api sh -lc "mcp-server-filesystem --help >/dev/null && python -c 'import langchain_mcp_adapters; print(\"adapter ok\")'"
```

Filesystem MCP 当前仍只用于实时只读查询。受管文件重命名由后端受控 Tool 执行，
只有显式启用 `MANAGED_ROOT_<KEY>_ALLOW_RENAME=true` 且用户确认 OperationPlan 后才允许写入。

## 旧版 XLS 依赖

旧版 `.xls` 不再使用 `xlrd` 直读。API 必须安装 LibreOffice/`soffice`，在隔离临时目录中把 `.xls`
转换为临时 `.xlsx`，通过格式校验后再由 `openpyxl` 读取。容器和离线镜像因此必须预装 LibreOffice；
转换失败会返回结构化错误，不会覆盖原件，也不会把临时 `.xlsx` 登记成上传原件。

## 公网访问必须额外完成的网络步骤

部署脚本不能替你注册域名、设置 DNS 或修改路由器。公网 HTTPS 需要：

1. 为 `file-agent.example.com` 添加 A 记录，指向这台电脑公网 IP；
2. 在路由器将公网 TCP 80、443 转发到这台 Windows 电脑；
3. Windows 防火墙放行 TCP 80、443（`-OpenFirewall` 需要管理员 PowerShell）；
4. 等待 Caddy 自动申请 HTTPS 证书。

## 仅局域网测试

```powershell
.\deploy\deploy.ps1 -SiteAddress :80 -OpenFirewall
```

访问地址是 `http://<这台电脑的局域网 IP>/`。这是 HTTP，不能用于真实用户上传敏感文件。

## 日常命令

更新代码并重建：

```powershell
.\deploy\update.ps1
```

使用离线 zip 包更新并重建：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip
```

仅重建当前目录中的代码，不执行 `git pull`：

```powershell
.\deploy\update.ps1 -SkipGitPull
```

## 离线 zip 更新流程

适用于无法联网拉取 Git 仓库、但可以通过 U 盘或内网传输更新包的场景。

1. 在可联网环境准备项目更新 zip 包，压缩后的包内必须包含项目根目录内容，至少要有：

```text
apps/
deploy/
requirements.txt
```

2. 将 zip 包复制到部署机器，例如：

```text
C:\packages\file-agent-update.zip
```

3. 在项目根目录执行：

```powershell
.\deploy\update.ps1 -PackageZip C:\packages\file-agent-update.zip
```

4. 脚本会自动：
   - 解压 zip 到临时目录；
   - 校验包内是否存在 `apps/` 和 `deploy/`；
   - 用离线包覆盖项目代码；
   - 保留现有 `deploy/.env`；
   - 保留 `data/` 下的上传文件、日志、备份和受管目录数据；
   - 重新执行 `docker compose up -d --build`。

5. 更新完成后，用下面命令确认容器状态：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps
```

离线更新限制：

- zip 包必须来自受信任来源；
- 该脚本不会自动清理项目目录里“离线包已经删除、但旧目录仍残留”的顶层无关文件；
- 如果更新包修改了 `deploy/docker-compose.production.yml` 中的 volume mount，仍需人工确认宿主机目录和权限。
- Docling 属于新增的重量级 Python 依赖。完全断网环境首次构建前，需要在联网机器预先构建包含 Docling 依赖和模型缓存的 API 镜像并随更新包导入；仅携带源码 zip 无法替代 pip 包和模型文件下载。已有完整 Docker 构建缓存时可以继续使用 `update.ps1` 重建。
- `.xls` 完整解析依赖 LibreOffice，不再需要 xlrd wheel。完全断网部署必须预先构建并导入包含 LibreOffice 的 API 镜像；只更新源码无法为现有镜像补装系统包。

本版本默认配置为：

```text
DOCLING_ENABLED=true
DOCLING_FORMATS=pdf,docx
DOCLING_OCR_ENABLED=false
```

旧版 `.doc` 的结构化读取还需要 LibreOffice。容器部署应在 API 镜像中安装 LibreOffice；非 Docker
部署可使用系统安装：Windows 指向 `soffice.com`，macOS 指向 LibreOffice.app 内的 `soffice`，Linux
指向 `/usr/bin/soffice`。生产配置：

```text
LEGACY_OFFICE_CONVERSION_ENABLED=true
LEGACY_OFFICE_CONVERTER=libreoffice
LIBREOFFICE_EXECUTABLE=
LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS=90
LEGACY_OFFICE_MAX_FILE_SIZE_MB=100
LEGACY_OFFICE_DERIVATIVE_DIR=derivatives/office
```

离线更新包必须同时包含带 LibreOffice 的 API 镜像或对应平台的 LibreOffice 离线安装包。仅更新源码不会
自动安装系统程序。更新后执行迁移 `20260720_0001`，创建 `document_artifacts`。

更新后必须执行数据库迁移，创建 `document_elements` 并增加解析器版本字段：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml exec api `
  python -m alembic -c apps/api/alembic.ini upgrade head
```

停止应用（不删除数据）：

```powershell
.\deploy\stop.ps1
```

备份数据库：

```powershell
.\deploy\backup.ps1
```

备份数据库和上传文件：

```powershell
.\deploy\backup.ps1 -IncludeUploads
```

查看日志：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f
```

仅查看文件系统 worker 日志：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f filesystem-worker
```

## 安全约束

- `deploy/.env` 含数据库密码和 JWT 密钥，不能提交到 Git。
- 不要暴露 PostgreSQL 5432 或 FastAPI 8000；本方案只暴露 80/443。
- `managed root` 必须通过 Docker 只读挂载进入容器，不能把宿主机任意路径直接暴露给 API。
- 浏览器公开注册已开启，Caddy 会将 `POST /api/auth/register` 转发给后端。
- `create-user.ps1` 仍可供管理员手动预创建账号使用。
- 公开注册意味着任何访问者都能创建账号；上线后应尽快补充注册频率限制、邮箱验证或邀请码机制。
