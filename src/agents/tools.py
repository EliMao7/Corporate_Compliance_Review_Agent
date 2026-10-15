"""
Agent Tools（LangChain `@tool` 外壳 · 进程内直调模式）

把 tool_layer/tools_impl.py 的纯函数实现包装成 LangChain 工具，供 ReAct 自主 Agent
在需要时调用。每个工具的 docstring 即模型判断"何时调用"的依据。

⚠️ 本文件**只做适配、不写业务逻辑** —— 实现一律在 tool_layer/tools_impl.py（R1 工具实现唯一源）。
   同一份能力有两种调用方式（进程内 @tool / 跨进程 MCP server），业务逻辑必须有唯一实现源。

注意：工具返回值一律为 str（create_react_agent 的 ToolNode 需要可序列化文本）。
工具内部对 Neo4j / 向量库的失败做兜底，返回"查询失败/未找到"文本，绝不抛异常打断 ReAct 循环。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.tools import tool as _lc_tool

from src.common.logger import setup_logger
from tool_layer import tools_impl

logger = setup_logger(__name__, "agents.log")

# 合规域工具名（mapping §7）：业务实现由 tool_layer/tools_impl.py 提供，本层仅生成外壳
tool_names = [
    "search_knowledge_graph",
    "semantic_search",
    "get_regulation_info",
    "natural_language_graph_query",
]

# 直接由实现函数生成 LangChain 工具：名称 / docstring / 参数 schema 全部沿用实现层，
# 不在此处重复书写（避免与 tool_layer/tools_impl.py 的 docstring 产生第二份副本）。
_by_name = {t.name: t for t in (_lc_tool(fn) for fn in tools_impl.all_tools().values())}

_missing = [n for n in tool_names if n not in _by_name]
if _missing:
    # 容错：工具实现层缺项时给出明确告警，而不是导入即崩
    logger.warning(f"[tools] tool_layer 未提供以下工具（请检查 tools_impl.py）: {_missing}")

tools = [_by_name[n] for n in tool_names if n in _by_name]

# 具名导出（保持 `from src.agents.tools import search_knowledge_graph` 的调用方式可用）
search_knowledge_graph = _by_name.get("search_knowledge_graph")
semantic_search = _by_name.get("semantic_search")
get_regulation_info = _by_name.get("get_regulation_info")
natural_language_graph_query = _by_name.get("natural_language_graph_query")

__all__ = [
    "tools",
    "tool_names",
    "search_knowledge_graph",
    "semantic_search",
    "get_regulation_info",
    "natural_language_graph_query",
]


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("AGENT TOOLS ADAPTER TEST")
    print("=" * 60)

    print(f"\n[registry] loaded {len(_by_name)} tools from tool_layer: {sorted(_by_name)}")
    print(f"[expected] {tool_names}")
    if _missing:
        print(f"[warn] tool_layer 未提供: {_missing}（容错，不阻断）")
    else:
        print("[ok] 全部合规工具已就绪")

    for n in tool_names:
        t = _by_name.get(n)
        print(f"  - {n}: {'OK' if t is not None else 'MISSING'} (docstring={'yes' if t is not None and t.description else 'no'})")

    print("\n" + "=" * 60)
    print("[SUCCESS] Agent tools adapter tests completed!")
    print("=" * 60)
