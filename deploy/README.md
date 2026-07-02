# File Agent Windows 一键部署包

把整个 `deploy/` 文件夹复制到 File Agent 项目根目录。

## 会启动什么

```text
浏览器
  → Caddy（仅对外开放 80 / 443）
  → FastAPI（容器内 8000）
  → PostgreSQL + pgvector（容器内）
```

持久化数据在项目根目录：

```text
data/uploads
data/logs
data/backups
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

## 安全约束

- `deploy/.env` 含数据库密码和 JWT 密钥，不能提交到 Git。
- 不要暴露 PostgreSQL 5432 或 FastAPI 8000；本方案只暴露 80/443。
- 浏览器公开注册已开启，Caddy 会将 `POST /api/auth/register` 转发给后端。
- `create-user.ps1` 仍可供管理员手动预创建账号使用。
- 公开注册意味着任何访问者都能创建账号；上线后应尽快补充注册频率限制、邮箱验证或邀请码机制。
