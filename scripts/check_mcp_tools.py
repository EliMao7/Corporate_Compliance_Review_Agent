"""
MCP 工具层连通性自检

用途：验证「工具层抽成独立进程 MCP server」这条链路真的贯通 ——
      拉起 mcp_server.server（stdio 子进程）→ tools/list → 逐个工具签发一次调用。

特点：**不依赖 Neo4j / FAISS / LLM**。工具被真正调用时若后端不可用，实现层会按契约
      兜底成"查询失败：..."文本而非抛异常（见 tool_layer/tools_impl.py 契约 2），
      所以本脚本能在没装数据库的机器上验证「协议链路」是否打通。

判据：
  ✅ PASS 工具数 = 4，且每个工具调用都有返回（返回"查询失败"也算 PASS —— 那证明
           协议链路通了、只是后端没起；返回为空或抛异常才是 FAIL）
  ❌ FAIL 连不上 server / 工具数为 0 / 调用抛异常

用法（在项目根目录）：
    python scripts/check_mcp_tools.py
    python scripts/check_mcp_tools.py --call       # 额外对每个工具发一次真实调用
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

EXPECTED_TOOLS = {
    "search_knowledge_graph",
    "semantic_search",
    "get_regulation_info",
    "natural_language_graph_query",
}

# 每个工具签一次的最小合法入参
SAMPLE_ARGS = {
    "search_knowledge_graph": {"entity": "行政许可设定"},
    "semantic_search": {"query": "供应商准入的合规要求"},
    "get_regulation_info": {"name": "优化营商环境条例"},
    "natural_language_graph_query": {"query": "行政许可设定对应哪些归口部门"},
}


async def _run(check_call: bool) -> int:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PROJECT_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    client = MultiServerMCPClient({
        "ccr-compliance-tools": {
            # 用当前解释器启动子进程，保证与主进程同一套依赖
            "command": sys.executable,
            "args": ["-m", "mcp_server.server"],
            "env": env,
            "cwd": str(_PROJECT_ROOT),
            "transport": "stdio",
        }
    })

    print("[1/3] 连接 MCP server 并拉取工具列表 ...")
    tools = await client.get_tools()
    names = {t.name for t in tools}
    print(f"      拿到 {len(tools)} 个工具: {sorted(names)}")

    print("[2/3] 校验工具集合与参数 schema ...")
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    ok = True
    if missing:
        print(f"      ❌ 缺失工具: {sorted(missing)}")
        ok = False
    if extra:
        print(f"      ⚠️ 多出未预期工具: {sorted(extra)}")
    for t in tools:
        schema = getattr(t, "args_schema", None)
        # langchain-mcp-adapters 直接把 MCP 的 inputSchema（JSON Schema）挂上来，
        # 因此这里是 dict 而非 pydantic 模型（本地 @tool 生成的则是模型）。
        if schema is None:
            props = []
        elif isinstance(schema, dict):
            props = list((schema.get("properties") or {}).keys())
        else:
            props = list((schema.model_json_schema().get("properties") or {}).keys())
        desc = (t.description or "").strip().splitlines()[0][:40]
        print(f"      · {t.name}({', '.join(props)})  「{desc}」")

    if check_call:
        print("[3/3] 逐工具签发一次真实调用（后端不可用应返回兜底文本，不得抛异常）...")
        for t in tools:
            args = SAMPLE_ARGS.get(t.name, {})
            try:
                res = await t.ainvoke(args)
                text = str(res).replace("\n", " ")[:120]
                # 兜底文本（含"失败"/"未找到"）同样算链路 OK
                print(f"      ✅ {t.name} → {text}")
            except Exception as e:
                print(f"      ❌ {t.name} 调用抛异常: {type(e).__name__}: {e}")
                ok = False
    else:
        print("[3/3] 跳过真实调用（加 --call 开启）")

    print()
    print("=" * 56)
    print("[SUCCESS] MCP 工具层链路正常" if ok else "[FAILED] MCP 工具层链路异常")
    print("=" * 56)
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP 工具层连通性自检")
    ap.add_argument("--call", action="store_true", help="对每个工具发一次真实调用（需后端，或接受兜底文本）")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.call)))


if __name__ == "__main__":
    main()
