# 公开注册已启用

本部署包不再在 Caddy 层拦截 `POST /api/auth/register`。

部署后，访问登录页即可使用“申请注册”创建账号。现有 `create-user.ps1`
保留为管理员预创建账号的可选工具。

请勿将 FastAPI 的 8000 端口直接暴露到公网；仅通过 Caddy 的 80/443 对外提供服务。
