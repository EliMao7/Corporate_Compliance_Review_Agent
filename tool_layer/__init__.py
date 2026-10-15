"""
工具层（Tool Layer）—— 本项目「已测能力」的唯一实现源

本包的**模块级依赖只有标准库** —— 业务依赖（Neo4j / FAISS / LLM）
全部延迟到函数真正被调用时才 import。因此：

  · src/agents/tools.py  → 进程内 LangChain `@tool` 外壳（完整环境下使用）
  · mcp_server/server.py → 独立进程 MCP server（最小环境下也能启动）

  如果把它放在 src/agents/ 下，`from src.agents import ...` 会先执行
  src/agents/__init__.py → state → knowledge_graph → common → langchain_openai，
  一个只想暴露四个工具的进程就被迫依赖整套 Agent 栈才能启动 ——
  这正是「工具层应独立于消费方」这个目标要消除的耦合。

  实测记录：把工具层放在 src/agents/ 下时，MCP server 直接
  ModuleNotFoundError: langchain_openai（本地只装了 mcp SDK）。下沉到本包后，
  模块导入只需标准库 + mcp，server 可在最小环境启动；工具被真正调用时
  才需要对应后端的依赖，缺失也只在单次调用内被兜底成"查询失败"文本。
"""
