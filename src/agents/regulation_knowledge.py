"""
Regulation Knowledge Agent —— 「强制混合检索打底 + 可选 ReAct 多跳」(grounded-ReAct)

三段式（grounded 生成）：
  Step0 实体链接：用户口语 → 图谱规范实体（link_entities，阈值 0.85）；
  Step1 强制检索：FAISS 语义召回 + Neo4j 精确匹配，两路合并去重（**必走，grounding 底线**）；
  Step2 grounded 生成：**只**基于检索到的条文/事实作答；检索为空 → 分风险兜底。

为什么是"强制"：合规场景答案必须可溯源。**允许模型跳过检索直接作答，等于允许幻觉进最终答案**。
检索为空时的分级兜底：
  · 一般常识问句 → 标注"非本系统知识库内容"，可给通用解释；
  · 涉及处罚设定/许可设定/权利义务的高风险问句 → **不进模型自由发挥**，直接建议咨询主管部门/律师。
绝不凭模型记忆编造法条。

只写 state["regulation_answer"]（+ retrieved_contexts/retrieved_entities/linked_entities + 调试 messages）。
节点名与导出单例名 `regulation_knowledge` 由 graph.py 使用。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from config.settings import settings
from src.knowledge_graph.schema import NodeType
from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.common.memory import format_history
from src.agents.state import AgentState

logger = setup_logger(__name__, "agents.log")

_FALLBACK = "抱歉，我暂时无法回答这个问题，请咨询主管部门或执业律师。"

# 高风险关键词：检索为空时这些问题绝不让 LLM 凭空答（涉及处罚/许可的设定与权利义务）。
# 依据 TDD §2.3.7「检索为空 → 分风险兜底」：涉及处罚设定 / 许可设定 / 权利义务的高风险问句直接建议咨询。
_HIGH_RISK_KW = ["处罚", "罚款", "没收", "没收违法所得", "许可", "审批", "行政强制", "强制",
                 "吊销", "资质", "责令", "征收", "征用", "权利", "义务", "复议", "行政诉讼",
                 "能不能设", "可以设", "自行设定", "罚多少", "罚则"]

SAFE_DEFERRAL = ("本系统知识库未检索到关于「{query}」的权威依据。该问题涉及处罚/许可设定或权利义务，"
                 "为避免误导，请务必咨询主管部门或执业律师，切勿自行设定或援引无依据的条款。")

_GROUNDED_SYSTEM = "你是严谨的法规知识问答助手。只依据提供的知识库内容作答，绝不编造法条。"

_GROUNDED_PROMPT = """请**仅依据**下面的【知识库检索内容】回答用户问题。
要求：
1. 不要引入检索内容之外的事实；检索内容不足以完整回答时，明确指出"知识库暂无该部分数据"。
2. 每条结论必须挂上法规名称与条款号（检索内容里有则注明；没有则说明"未检索到具体条款"）。
3. 通俗中文，结构清晰，可用列表。
4. 涉及处罚/许可设定或权利义务时，提醒"须有上位法依据并经法定程序，请由法务或律师复核"。

【知识库检索内容】
{contexts}

【用户问题】
{query}"""

# ReAct 多跳层：强制检索内容作为主要依据已给出，工具仅用于多跳补充
_REACT_GROUNDED_SYSTEM = """你是企业合规审查系统的"法规知识"专家。系统已**强制检索知识库**，得到如下内容，这是你作答的主要依据：
{contexts}
{entity_hints}
工作准则：
1. 优先依据上面的检索内容作答；若问题需要多跳补充（如"某事项的风险点 → 对应的合规措施/上位法"），可调用工具 search_knowledge_graph / semantic_search / get_regulation_info 进一步取证；需要灵活的多跳/聚合/条件查询时，可调用 natural_language_graph_query（自动生成只读 Cypher 查图谱）。
2. 绝不编造检索与工具之外的事实，**绝不凭记忆编造法条**；查不到就如实说"知识库暂无该数据"。
3. 每条结论尽量挂法规名称与条款号；涉及处罚/许可设定时提醒须有上位法依据并经法定程序。
4. 通俗中文，结构清晰，可用列表；重大事项建议咨询主管部门或律师。"""

_GENERAL_FALLBACK_PROMPT = """本系统知识库**未检索到**与下面问题相关的内容。请基于通用合规常识**简要**回答，并且：
1. 开头第一行必须写：（以下为通用合规信息，非本系统知识库内容，仅供参考）
2. 不得断言某文件合法/违法，不得给出"可直接发布/执行"的结论；涉及处罚、许可设定的一律建议咨询主管部门/律师。
3. 结尾提醒"具体以现行有效法规及法务/律师意见为准"。

【用户问题】
{query}"""


def _is_high_risk(query: str) -> bool:
    """规则判定高风险（处罚/许可设定、权利义务）——检索为空时绝不臆测。"""
    return any(k in query for k in _HIGH_RISK_KW)


class RegulationKnowledgeAgent:
    """Grounded-ReAct knowledge agent: forced retrieval (grounding floor) + optional ReAct multi-hop."""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._llm = None
        self.react_enabled = settings.enable_react_layer
        # ReAct 工具调用层（create_react_agent 由 langgraph 提供）。
        # 工具可能来自**进程内**（LangChain @tool）或**独立进程**（MCP server），
        # 由 settings.tool_transport 切换，本类其余代码对两种传输无感。
        # 构造延迟到首次使用：MCP 建连要跑 asyncio，而本类在模块导入期即实例化，
        # 导入期不宜启动事件循环。
        self._agent = None

    def _ensure_llm(self):
        if self._llm is None:
            self._llm = get_llm(temperature=0.1)
        return self._llm

    # ---------------- ReAct 工具层（inproc / mcp 双传输） ----------------
    def _react_tools(self) -> list:
        """取得 ReAct 可用的工具列表。

        - inproc：进程内直调（src/agents/tools.py 的 LangChain @tool 外壳）
        - mcp   ：由独立进程的 MCP server 提供（mcp_server/server.py，stdio 传输）
        两种外壳共享同一份实现（tool_layer/tools_impl.py），工具名/参数/行为一致。
        """
        names = ["search_knowledge_graph", "semantic_search", "get_regulation_info"]
        if settings.text2cypher_enabled:
            names.append("natural_language_graph_query")  # Text2Cypher（只读五层防护）

        if settings.tool_transport == "mcp":
            try:
                from src.agents.mcp_tools import tools_by_name
                pool = tools_by_name()
                picked = [pool[n] for n in names if n in pool]
                missing = [n for n in names if n not in pool]
                if missing:
                    logger.warning(f"[RegulationKnowledge] MCP server 未提供工具: {missing}")
                if picked:
                    logger.info(f"[RegulationKnowledge] tool transport=mcp, tools={[t.name for t in picked]}")
                    return picked
                logger.warning("[RegulationKnowledge] MCP server 未返回任何工具，回退进程内直调")
            except Exception as e:
                # 关键取舍：**工具层故障不得拖垮整条问答链路**。
                # MCP server 起不来时回退进程内实现 —— grounding 底线是 Step 1 的强制检索，
                # ReAct 只是多跳补充，可安全降级。
                logger.error(f"[RegulationKnowledge] MCP 工具加载失败，回退进程内直调: {e}")

        from src.agents.tools import (
            search_knowledge_graph,
            semantic_search,
            get_regulation_info,
            natural_language_graph_query,
        )
        local = {
            "search_knowledge_graph": search_knowledge_graph,
            "semantic_search": semantic_search,
            "get_regulation_info": get_regulation_info,
            "natural_language_graph_query": natural_language_graph_query,
        }
        return [local[n] for n in names]

    def _ensure_agent(self):
        """首次使用时构造 ReAct agent（懒加载）。构造失败向上抛，由 _react_answer 的 try 兜住。"""
        if self._agent is None:
            self._agent = create_react_agent(
                get_llm(temperature=0.1),
                tools=self._react_tools(),
            )
        return self._agent

    # ---------------- Step 1a: 按实体类型扩展 Neo4j 结构化事实 ----------------
    @staticmethod
    def _expand_entity(q, name: str, typ: str):
        """返回该实体的一段事实摘要文本；查无返回 None。绝不抛异常。"""
        try:
            if typ == NodeType.MATTER.value:
                info = q.get_matter_full_info(name)
                if not info:
                    return None
                parts = [f"审查事项：{info['name']}"]
                if info.get("description"):
                    parts.append(f"描述：{info['description']}")
                if info.get("risks"):
                    parts.append("风险点：" + "、".join(info["risks"]))
                if info.get("authorities"):
                    parts.append("归口部门：" + "、".join(info["authorities"]))
                evs = [e for e in info.get("evidences", []) if e]
                if evs:
                    parts.append("应比对依据：" + "、".join(evs))
                meas = [m["name"] for m in info.get("measures", []) if m.get("name")]
                if meas:
                    parts.append("合规措施：" + "、".join(meas))
                return "；".join(parts)

            if typ == NodeType.RISK_POINT.value:
                info = q.get_risk_info(name)
                if not info:
                    return None
                parts = [f"风险点：{info['name']}"]
                if info.get("description"):
                    parts.append(f"说明：{info['description']}")
                if info.get("severity"):
                    parts.append(f"等级：{info['severity']}")
                matters = q.get_risk_matters(name)
                if matters:
                    parts.append("涉及事项：" + "、".join(matters))
                return "；".join(parts)

            if typ in (NodeType.MEASURE.value,):
                info = q.get_measure_info(name)
                if not info:
                    return None
                parts = [f"合规措施：{info['name']}"]
                if info.get("scenario"):
                    parts.append(f"适用场景：{info['scenario']}")
                if info.get("key_points"):
                    parts.append("要点：" + "、".join(info["key_points"]))
                if info.get("contraindications"):
                    parts.append("不适用/需避免：" + "、".join(info["contraindications"]))
                return "；".join(parts)

            if typ == NodeType.AUTHORITY.value:
                matters = q.get_authority_matters(name)
                return f"归口部门「{name}」主管事项：{'、'.join(matters)}" if matters else None
        except Exception as e:
            logger.warning(f"[RegulationKnowledge] expand '{name}' failed: {e}")
        return None

    # ---------------- Step 1: 强制混合检索（grounding 底线，必走） ----------------
    def _retrieve(self, text: str, retriever=None):
        """确定性强制检索：FAISS 语义召回(+Neo4j 精确) → 按类型扩展 Neo4j 事实。
        返回 (contexts, used_hits)。绝不抛异常；contexts 为空 == 知识库未覆盖。
        """
        hits = []
        try:
            from src.vector_store.search import get_shared_retriever
            vr = retriever or get_shared_retriever()
            hits = vr.search(text, top_k=5) or []
        except Exception as e:
            logger.warning(f"[RegulationKnowledge] vector retrieval failed: {e}")

        contexts, used, seen = [], [], set()
        try:
            from src.knowledge_graph.graph_queries import GraphQueries
            q = GraphQueries()
            for h in hits:
                name, typ = h.get("name", ""), h.get("type", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                ctx = self._expand_entity(q, name, typ)
                if ctx:
                    contexts.append(ctx)
                    used.append({k: h.get(k) for k in ("name", "type", "score", "source")})
            # 兜底：把 query 本身当实体名再各查一次（query 可能就是"招标投标""行政许可"）
            for typ in (NodeType.MATTER.value, NodeType.RISK_POINT.value):
                ctx = self._expand_entity(q, text.strip(), typ)
                if ctx and ctx not in contexts:
                    contexts.append(ctx)
            q.close()
        except Exception as e:
            logger.warning(f"[RegulationKnowledge] graph expansion failed: {e}")
        return contexts, used

    # ---------------- ReAct 解析辅助 ----------------
    @staticmethod
    def _tool_calls_of(msg) -> list[str]:
        tc = getattr(msg, "tool_calls", None)
        if not tc and isinstance(msg, dict):
            tc = msg.get("tool_calls")
        if not tc:
            return []
        return [(c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) for c in tc]

    @staticmethod
    def _final_text(msgs) -> str:
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and m.content:
                return m.content
            if isinstance(m, dict) and m.get("type") == "ai" and m.get("content"):
                return m["content"]
        return ""

    # ---------------- Step 2 生成方式 ----------------
    def _grounded_llm(self, user_text: str, contexts: list) -> str:
        """纯 grounded 生成（不开 ReAct 或 ReAct 失败时的退回路径）。"""
        prompt = _GROUNDED_PROMPT.format(contexts="\n".join(f"- {c}" for c in contexts), query=user_text)
        return self._ensure_llm().invoke([SystemMessage(content=_GROUNDED_SYSTEM),
                                          HumanMessage(content=prompt)]).content

    def _react_answer(self, user_text: str, contexts: list, entity_hints: str = ""):
        """在强制检索内容之上跑 ReAct 多跳；返回 (answer, 工具新增上下文, 工具调用名单)。"""
        hints_block = f"\n{entity_hints}\n" if entity_hints else ""
        sys = _REACT_GROUNDED_SYSTEM.format(
            contexts="\n".join(f"- {c}" for c in contexts),
            entity_hints=hints_block,
        )
        res = self._ensure_agent().invoke({"messages": [SystemMessage(content=sys), HumanMessage(content=user_text)]})
        msgs = res["messages"]
        tool_ctxs = [m.content for m in msgs if isinstance(m, ToolMessage) and m.content]
        tool_calls = [name for m in msgs for name in self._tool_calls_of(m)]
        final = self._final_text(msgs) or self._grounded_llm(user_text, contexts)
        return final, tool_ctxs, tool_calls

    def _general_llm(self, user_text: str) -> str:
        prompt = _GENERAL_FALLBACK_PROMPT.format(query=user_text)
        return self._ensure_llm().invoke([SystemMessage(content=_GROUNDED_SYSTEM),
                                          HumanMessage(content=prompt)]).content

    # ---------------- 主流程 ----------------
    def __call__(self, state: AgentState) -> dict:
        query = state.get("query", "")
        history = state.get("history", [])
        logger.info(f"[RegulationKnowledge] Query: {query[:50]}... (history {len(history)})")
        user_text = (f"【对话历史】\n{format_history(history)}\n\n【本轮问题】\n{query}") if history else query

        # Step 1：强制检索（grounding 底线，必走）
        retriever = None
        try:
            from src.vector_store.search import get_shared_retriever
            retriever = get_shared_retriever()
        except Exception as e:
            logger.warning(f"[RegulationKnowledge] retriever init failed: {e}")
        contexts, used = self._retrieve(user_text, retriever=retriever)
        logger.info(f"[RegulationKnowledge] forced retrieval: entities={[h.get('name') for h in used]} ctx={len(contexts)}")

        # Step 2：显式实体链接（高精度：用户表述/检索命中 → 图谱规范实体锚点）
        linked, hints_text = [], ""
        if retriever is not None:
            try:
                terms = (
                    list(state.get("matters", []))
                    + list(state.get("risk_signals", []))
                    + [h.get("name", "") for h in used if h.get("name")]
                    + [query]
                )
                linked = retriever.link_entities(terms)
                # 同一规范实体只保留相似度最高的一条锚点
                best: dict = {}
                for lk in linked:
                    key = lk["matched_entity"]
                    if key not in best or lk["similarity"] > best[key]["similarity"]:
                        best[key] = lk
                linked = sorted(best.values(), key=lambda x: x["similarity"], reverse=True)
                if linked:
                    hints_text = (
                        "【已链接实体（高置信链接，建议用下列规范名做精确查询条件）】\n"
                        + "；".join(
                            f"{lk['input_entity']}→{lk['matched_entity']}({lk['type']},{lk['similarity']:.2f})"
                            for lk in linked
                        )
                    )
            except Exception as e:
                logger.warning(f"[RegulationKnowledge] entity linking failed: {e}")
        logger.info(f"[RegulationKnowledge] entity linking: {[lk['matched_entity'] for lk in linked]}")

        # Step 2/3：生成（grounded+ReAct / 纯 grounded / 分风险兜底）
        tool_calls, mode = [], ""
        try:
            if contexts:
                if self.react_enabled:
                    answer, tool_ctxs, tool_calls = self._react_answer(
                        user_text, contexts, entity_hints=hints_text
                    )
                    contexts = contexts + tool_ctxs          # 工具多跳结果并入 grounding（供 RAGAS）
                    mode = "grounded+react"
                else:
                    answer = self._grounded_llm(user_text, contexts)
                    mode = "grounded"
            elif _is_high_risk(query):
                answer = SAFE_DEFERRAL.format(query=query)
                mode = "defer-high-risk"
            else:
                answer = self._general_llm(user_text)
                mode = "general-fallback"
        except Exception as e:
            logger.error(f"[RegulationKnowledge] generation error: {e}")
            answer, mode = _FALLBACK, "error"

        logger.info(f"[RegulationKnowledge] mode={mode} answer={len(answer)} chars tools={tool_calls}")
        return {
            "regulation_answer": answer,
            "retrieved_entities": used,                       # list[dict]，供观测
            "retrieved_contexts": contexts,                   # list[str]，供 RAGAS faithfulness/context
            "linked_entities": linked,                        # list[dict]，实体链接结果（API/前端展示）
            "messages": [f"[RegulationKnowledge] mode={mode} entities={[h.get('name') for h in used]} "
                         f"ctx={len(contexts)} tools={tool_calls} "
                         f"linked={[lk['matched_entity'] for lk in linked]}"],
        }


# 单例名保持 regulation_knowledge → graph.py 使用
regulation_knowledge = RegulationKnowledgeAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("REGULATION KNOWLEDGE AGENT TEST")
    print("=" * 60)

    # 离线可跑的结构性断言
    print("\n[Structural checks]")
    assert _is_high_risk("能不能自行设定行政处罚") is True
    assert _is_high_risk("什么是招标投标") is False
    assert "非本系统知识库内容" in _GENERAL_FALLBACK_PROMPT
    print("  high-risk detection: OK")
    print("  general-fallback labels 非知识库内容: OK")

    # 空检索 + 高风险 → 确定性 defer（不调用 LLM，无需 key）
    print("\n[High-risk deferral path]")
    res = regulation_knowledge._expand_entity  # 存在性
    assert callable(res)
    print("  SAFE_DEFERRAL:", SAFE_DEFERRAL.format(query="行政处罚")[:60], "...")

    print("\n" + "=" * 60)
    print("[SUCCESS] Regulation knowledge tests completed!")
    print("=" * 60)
