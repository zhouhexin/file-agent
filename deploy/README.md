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

当前阶段不要把受管目录改成读写挂载。写操作必须等 OperationPlan 确认闭环和写入型
MCP Tool 白名单完成后再开启。

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
