"""
MCP 客户端适配层 —— 把独立进程 MCP server 的工具接进本项目的 LangGraph Agent

为什么需要这一层：
  `langchain-mcp-adapters` 提供的是**异步**接口（MCP 协议本身基于 asyncio），
  而本项目 Agent 的构造与调用是**同步**的（`create_react_agent(...).invoke(...)`，
  节点函数也是同步的）。本模块负责这层 sync/async 桥接，让上层完全无感。

  另一处约束：`regulation_knowledge.py` 在**模块导入时**就实例化单例，
  而导入期不能保证处于事件循环中。因此这里用「懒加载 + 首次调用时建连」，
  把建连成本从导入期挪到首次真实使用，同时用 lru_cache 保证只建一次。

配置（config/settings.py）：
    tool_transport      = "mcp" 时本模块才被使用（默认 "inproc" 走进程内直调）
    mcp_server_command  = 启动 MCP server 的可执行文件，如 "python"
    mcp_server_args     = 逗号分隔的参数，如 "-m,mcp_server.server"
    mcp_server_cwd      = MCP server 工作目录，留空 = 项目根

⚠️ 性能取舍（已知，不要回避）：
  stdio 传输下，`langchain-mcp-adapters` 的工具**每次 invoke 都会新建一次会话
  （= 重新拉起子进程）**。对本项目意味着每次工具调用都要重连 Neo4j / 重载 FAISS 索引，
  比进程内直调慢得多。因此：
    - 评测与生产问答走 `inproc`（保持基线可比）；
    - `mcp` 模式用于验证「工具层可被跨进程复用」的架构目标，以及给外部消费方接入。
  若要让 mcp 模式跑生产问答，需要改为长驻会话（复用同一个 ClientSession）。
"""

import asyncio
import concurrent.futures
import os
import sys
from functools import lru_cache
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "agents.log")

# MCP 客户端里的服务名（mcp_server/server.py 注册的 server 名）
MCP_SERVER_NAME = "ccr-compliance-tools"


def _server_config() -> dict:
    """构造 MCP 客户端需要的 stdio 连接配置。"""
    args = [a for a in (settings.mcp_server_args or "").split(",") if a.strip()]
    env = dict(os.environ)
    # 子进程必须能找到 `mcp_server` 与 `src` 两个顶层包：
    # 无论子进程 cwd 是什么，PYTHONPATH 都指向项目根，保证 `python -m mcp_server.server` 可用。
    env["PYTHONPATH"] = str(_project_root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    cfg = {
        # 留空 → 用当前解释器：确保子进程与主进程在同一套依赖里
        "command": settings.mcp_server_command or sys.executable,
        "args": args,
        "env": env,
        "transport": "stdio",
    }
    if settings.mcp_server_cwd:
        cfg["cwd"] = settings.mcp_server_cwd
    return cfg


def _run_sync(coro):
    """在同步上下文中执行协程。

    - 当前线程没有事件循环（正常路径：LangGraph 同步节点）→ 直接 asyncio.run
    - 当前线程**已在**事件循环中（例如被 async 路由调用）→ 换一个线程跑，
      避免 `asyncio.run() cannot be called from a running event loop`。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@lru_cache(maxsize=1)
def load_mcp_tools() -> tuple:
    """连接 MCP server 并加载全部工具（转换为 LangChain 工具对象）。

    返回 tuple（可缓存/可哈希）；结果缓存，避免每次问答都重新建连。
    建连失败时抛异常 —— 由调用方决定是否回退到进程内直调（见 regulation_knowledge.py）。
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    logger.info(f"[MCP] connecting tool server: {settings.mcp_server_command or sys.executable} "
                f"{settings.mcp_server_args} (cwd={settings.mcp_server_cwd or _project_root})")
    client = MultiServerMCPClient({MCP_SERVER_NAME: _server_config()})
    tools = tuple(_run_sync(client.get_tools()))
    logger.info(f"[MCP] loaded {len(tools)} tools: {[t.name for t in tools]}")
    return tools


def tools_by_name() -> dict:
    """返回 {工具名: LangChain 工具}，便于按名筛选（如 Text2Cypher 的条件启用）。"""
    return {t.name: t for t in load_mcp_tools()}


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("MCP TOOLS ADAPTER TEST")
    print("=" * 60)

    # 离线可跑：仅验证配置装配（不真正建连，避免拉起子进程）
    cfg = _server_config()
    print(f"  server name: {MCP_SERVER_NAME}")
    print(f"  transport  : {cfg.get('transport')}")
    print(f"  args       : {cfg.get('args')}")
    assert cfg.get("transport") == "stdio"
    assert MCP_SERVER_NAME and "-" in MCP_SERVER_NAME

    print("\n" + "=" * 60)
    print("[SUCCESS] MCP tools adapter tests completed!")
    print("=" * 60)
