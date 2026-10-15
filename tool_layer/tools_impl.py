"""
Tool Implementation Layer（工具业务实现层 · 纯函数，零框架依赖）

设计动机：
  「已测能力」（知识图谱查询 / 向量语义检索 / 法规条款信息 / Text2Cypher）的
  业务实现下沉到本模块，作为**唯一实现源**；上层只保留两种「外壳」：
    - src/agents/tools.py    → LangChain `@tool` 适配（进程内直调，本地/评测路径）
    - mcp_server/server.py   → MCP 适配（独立进程，stdio 传输，供跨进程/跨系统复用）

  两个外壳行为完全一致（同名同参同返回）。改逻辑只改这里，避免两份实现漂移（TDD §10）。

依赖纪律（重要，TDD §10.3）：
  **模块级只依赖标准库**。Neo4j / FAISS / LLM 相关 import 一律写在函数体内 ——
  这样独立进程的 MCP server 在只装了 mcp SDK 的最小环境里也能启动，
  后端不可用时由每个函数自己的 try/except 兜底成"查询失败"文本，不影响进程存活。

契约（不得破坏）：
  1. 返回值一律为 str —— `create_react_agent` 的 ToolNode 需要可序列化文本。
  2. 内部对 Neo4j / 向量库 / LLM 的失败做兜底，返回「查询失败/未找到」文本，
     **绝不抛异常** —— 异常会打断 ReAct 循环，让整轮问答失败。
  3. 不在本层做日志以外的任何 I/O 副作用。
"""

import logging
import sys
from pathlib import Path

# 允许以直接脚本方式运行 / 让 project root 可被 import
_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


# ============================================================
# 日志（延迟获取）：模块级只依赖标准库，故不在此处 import src.common
# ============================================================

_logger = None


def _get_logger():
    """延迟获取项目统一 logger（写入 logs/tools.log）；最小环境下退化为标准库 logging。

    刻意不在模块级 import src.common —— 其包级 __init__ 会连带导入 LangChain 等
    重依赖，而本模块要求「模块级只依赖标准库」（MCP server 需在只有 mcp SDK 的
    最小环境启动）。日志获取失败也绝不影响工具返回。
    """
    global _logger
    if _logger is None:
        try:
            from src.common.logger import setup_logger
            _logger = setup_logger(__name__, "tools.log")
        except Exception:  # pragma: no cover - 最小依赖环境下走这里
            _logger = logging.getLogger(__name__)
    return _logger


def _graph():
    """创建图谱查询封装（函数内 import：模块级不引入重依赖）。"""
    from src.knowledge_graph.graph_queries import GraphQueries
    return GraphQueries()


def search_knowledge_graph(entity: str) -> str:
    """根据"审查事项"或"风险点"查询知识图谱，返回其 归口部门/风险点/合规措施 的摘要文本。
    当用户询问某事项该由谁管、有哪些风险点、怎么改才合规，或某风险点可能牵出
    哪些事项时使用。若图谱中查无该实体，返回"未找到"说明，不要据此编造。"""
    try:
        q = _graph()
        info = q.get_matter_info(entity)
        if info:
            risks = q.get_matter_risks(entity)
            auths = q.get_matter_authorities(entity)
            measures = q.get_matter_measures(entity)
            measures_txt = "、".join(m.get("name", "") for m in measures if m.get("name"))
            parts = [f"审查事项：{info.get('name', '')}"]
            if info.get("description"):
                parts.append(f"描述：{info['description']}")
            if risks:
                parts.append(f"风险点：{'、'.join(risks)}")
            if auths:
                parts.append(f"归口部门：{'、'.join(auths)}")
            if measures_txt:
                parts.append(f"合规措施：{measures_txt}")
            q.close()
            return "；".join(parts)
        matters = q.get_risk_matters(entity)
        q.close()
        if matters:
            return f"风险点「{entity}」可能牵出的审查事项：{'、'.join(matters)}"
        return f"未在知识图谱中找到实体「{entity}」"
    except Exception as e:
        _get_logger().warning(f"[tool search_knowledge_graph] {e}")
        return f"查询知识图谱失败：{e}"


def semantic_search(query: str) -> str:
    """对知识库做语义（向量）检索，返回最相关实体列表（名称/类型/相似度）。
    当用户表述与图谱实体名不一致（如口语"罚款"对应规范实体"行政处罚·罚款"）、
    需要模糊语义匹配时使用。"""
    try:
        # 进程级共享检索器（启动时已预热）：避免每次工具调用重读索引/重载模型
        from src.vector_store.search import get_shared_retriever
        vr = get_shared_retriever()
        hits = vr.search(query, top_k=5)
        if not hits:
            return "语义检索无相关结果"
        lines = [f"{h.get('name', '')}（{h.get('type', '')}, {h.get('score', 0):.2f}）" for h in hits]
        return "语义检索结果：" + "；".join(lines)
    except Exception as e:
        _get_logger().warning(f"[tool semantic_search] {e}")
        return f"语义检索失败：{e}"


def get_regulation_info(name: str) -> str:
    """查询某法规/依据材料的 效力层级/文号/发布机关/施行日期/适用范围/相关条款。
    当用户询问某部法律法规的效力层级、发文字号、发布机关，或需核对某事项应比对
    的依据材料时使用。若图谱中查无该法规，返回"未找到"说明，不要据此编造。"""
    try:
        q = _graph()
        info = None
        # 优先使用图谱查询封装提供的专用方法（若已收编该接口）
        getter = getattr(q, "get_evidence_info", None)
        if callable(getter):
            info = getter(name)
        if not info:
            # 兜底：只读 Cypher 精确匹配 Evidence（run_readonly_cypher 为接口契约方法）
            try:
                rows = q.run_readonly_cypher(
                    "MATCH (e:Evidence {name: $name}) "
                    "RETURN e.name AS name, e.level AS level, e.doc_no AS doc_no, "
                    "e.issuing_body AS issuing_body, e.effective_date AS effective_date, "
                    "e.articles AS articles LIMIT 1",
                    {"name": name},
                )
                info = rows[0] if rows else None
            except Exception:
                info = None
        q.close()
        if not info:
            return f"未在知识图谱中找到法规「{name}」"

        parts = [f"法规：{info.get('name', name)}"]
        if info.get("level"):
            parts.append(f"效力层级：{info['level']}")
        if info.get("doc_no"):
            parts.append(f"文号：{info['doc_no']}")
        if info.get("issuing_body"):
            parts.append(f"发布机关：{info['issuing_body']}")
        if info.get("effective_date"):
            parts.append(f"施行日期：{info['effective_date']}")
        scope = info.get("description") or info.get("scope") or info.get("applicable_scope")
        parts.append(f"适用范围：{scope}" if scope else "适用范围：（未收录，建议核对原文）")
        articles = info.get("articles") or []
        if articles:
            a_txt = "；".join(str(a) for a in articles if a)
            parts.append(f"相关条款：{a_txt}")
        else:
            parts.append("相关条款：（未收录，建议核对原文）")
        return "；".join(parts)
    except Exception as e:
        _get_logger().warning(f"[tool get_regulation_info] {e}")
        return f"查询法规信息失败：{e}"


def natural_language_graph_query(query: str) -> str:
    """用自然语言灵活查询知识图谱（内部自动生成并执行只读 Cypher，即 Text2Cypher）。
    比 search_knowledge_graph 更适合复杂/灵活的查询：多跳关系、条件过滤、聚合列举等
    （如"某事项的归口部门和合规措施分别是什么""哪些事项涉及行政处罚设定"）。
    建议在 query 中带上已知的规范实体名（可先参考 semantic_search 的链接结果）。
    查询在只读事务中执行并有记录数上限；返回结构化图谱记录，无法生成有效查询时返回说明文本。"""
    try:
        from src.knowledge_graph.text2cypher import get_engine
        result = get_engine().run(query)
        return result.get("text") or "图谱查询无结果"
    except Exception as e:
        _get_logger().warning(f"[tool natural_language_graph_query] {e}")
        return f"图谱查询失败：{e}"


# ============================================================
# 工具注册表：上层外壳（LangChain @tool / MCP）从此处取函数，避免各自硬编码
# ============================================================

def all_tools() -> dict:
    """返回 {工具名: 实现函数}。新增工具只需在此登记一次，两个外壳同时生效。"""
    return {
        "search_knowledge_graph": search_knowledge_graph,
        "semantic_search": semantic_search,
        "get_regulation_info": get_regulation_info,
        "natural_language_graph_query": natural_language_graph_query,
    }
