"""WorkBuddy 文件导入、检索、读取、证据回答和受控动作 MCP 适配器。

该包运行在用户本机，只把明确授权逻辑根中的文件流式发送给 File Agent；服务端永远不会接收或
解析客户端绝对路径。
"""

from .client import FileAgentIntegrationClient, LocalRootRegistry

__all__ = ["FileAgentIntegrationClient", "LocalRootRegistry"]
