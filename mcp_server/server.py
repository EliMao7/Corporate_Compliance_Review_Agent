"""
MCP Server —— 企业合规审查系统「工具层」的跨进程暴露

设计动机：
  改造前，知识图谱查询 / 向量检索 / 法规信息查询 / Text2Cypher 这四项能力只以 LangChain
  `@tool` 的形式存在。它们能且只能被「本进程内的这个 Agent」调用 —— 这是三重绑定：
     业务逻辑 ↔ 调用框架（LangChain） ↔ 进程（FastAPI 服务）。
  一旦有第二个消费方出现（另一个 Agent、另一套系统、另一种语言的客户端），
  要么复制一份实现，要么把这套 Python 服务整个搬过去。

  把工具层抽成独立进程的 MCP server 后，耦合解开：
      - 消费方只要会说 MCP（JSON-RPC over stdio / SSE / streamable-http），就能用这些能力；
      - 工具进化的边界从「函数签名」变成「协议契约」，可以独立部署、独立扩缩；
      - 工具实现本身零改动 —— 复用 tool_layer/tools_impl.py（唯一实现源）。

  这不是「为了用 MCP 而用 MCP」：只有当工具层确实存在**跨进程 / 跨消费方复用**的需求时，
  引入独立进程的协议层才划算。否则进程内直调（`tool_transport=inproc`）更快更简单。
  两种模式在同一套代码里并存，按配置切换，见 src/agents/mcp_tools.py 与 config/settings.py。

启动：
    python -m mcp_server.server          # 在项目根目录执行
    # 或由 MCP 客户端以子进程方式拉起（stdio 传输，配置见 config/settings.py 的 mcp_server_*）

工具列表由 tool_layer/tools_impl.py::all_tools() 统一登记 —— 新增工具只需在那里登记一次，
LangChain 外壳与 MCP 外壳同时生效，不会漏。
"""

import sys
from pathlib import Path

# 允许以 `python mcp_server/server.py` 直接启动（此时 sys.path[0] 是 mcp_server/ 而非项目根）
_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# MCP Python SDK 2.0 把 FastMCP 更名为 MCPServer 并调整了包路径。
# 本项目用到的三个 API（构造 / add_tool / run）两代签名一致，故做兼容导入：
# 1.x 与 2.x 都能跑，不被 SDK 大版本升级卡住。
try:                                                # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _MCPToolServer
except ImportError:                                 # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _MCPToolServer

from tool_layer import tools_impl

# 服务名会出现在 MCP 客户端的 tools/list 响应里，作为工具来源标识
mcp = _MCPToolServer("ccr-compliance-tools")

# 从实现层注册表批量注册：工具名 / 描述（docstring）/ 参数 schema 全部来自 tool_layer，
# 本文件不重复声明任何一项，杜绝与 LangChain 外壳的 schema 漂移。
for _name, _fn in tools_impl.all_tools().items():
    mcp.add_tool(_fn, name=_name)


def main() -> None:
    """以 stdio 传输启动服务（MCP 客户端通过子进程 stdin/stdout 通信）。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
