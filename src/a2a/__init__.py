# src/a2a/__init__.py
# A2A（Agent2Agent）对外协作层
#
# 与项目里已有的 tool_layer / mcp_server 是两条**正交**的链路：
#   MCP / 工具层 —— 本系统的 Agent 出去「用工具」（图谱查询、向量检索、Text2Cypher）
#   A2A          —— 外部系统来「用本系统」：把企业合规审查整体当成一个可被发现、可被委托的远端 Agent
#
# 对外暴露三样东西：
#   Agent Card（能力说明书）→ JSON-RPC 端点（接任务）→ 任务状态与产物（回结果）
#
# 最小用法（在 src/api/main.py 里）：
#   from src.a2a import install_a2a
#   install_a2a(app)

from src.a2a.agent_card import (
    AGENT_CARD_PATH,
    PROTOCOL_BINDING,
    PROTOCOL_VERSION,
    RPC_PATH,
    build_agent_card,
)
from src.a2a.app import build_standalone_app, install_a2a
from src.a2a.executor import ComplianceReviewExecutor

__all__ = [
    "AGENT_CARD_PATH",
    "PROTOCOL_BINDING",
    "PROTOCOL_VERSION",
    "RPC_PATH",
    "ComplianceReviewExecutor",
    "build_agent_card",
    "build_standalone_app",
    "install_a2a",
]
