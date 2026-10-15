"""
Authority Locator Agent（归口定位）
两级定位 + 图谱兜底，确定事项的归口部门：
  Tier1 图谱候选：查 `(Matter)-[:GOVERNED_BY]->(Authority)` 得到候选归口部门（可溯源，含噪声，**不直接采用**）。
  Tier2 LLM 择优：结合 事项 + 图谱候选 + 对话历史 做最终取舍，输出 [{name, role(主责/协管), reason, basis}]；
        图谱缺失时据合规常识补正并说明依据。
  兜底：LLM 返回空时回退 Tier1 图谱候选；两者皆空则置 needs_clarification 追问。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from pydantic import BaseModel, Field

from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.common.memory import format_history
from src.agents.state import AgentState

logger = setup_logger(__name__, "agents.log")


class AuthorityItem(BaseModel):
    """单个归口部门"""
    name: str = Field(description="归口部门/主管机关名称，如：市场监督管理部门、发展改革部门")
    role: str = Field(default="主责", description="角色：主责 / 协管")
    reason: str = Field(default="", description="为何由该部门归口（简短）")
    basis: str = Field(default="", description="依据（法规名称/条款或事项性质），可选")


class AuthorityResult(BaseModel):
    """LLM 归口定位结果"""
    authorities: list[AuthorityItem] = Field(description="归口部门 1-3 个，按主责优先排序；信息确实不足时返回空列表")
    follow_up: str = Field(default="", description="若信息不足，需要向用户追问的具体内容")


AUTHORITY_PROMPT = """你是企业合规领域的归口部门定位专家。请根据【涉及事项】【本轮输入】【对话历史】给出最合适的 1-3 个归口部门。
【图谱参考候选部门】（由"(事项)-[:GOVERNED_BY]->(部门)"聚合而来，可能含噪声，仅供取舍参考）：{kg_candidates}

规则：
- 依据事项的行政领域判断归口部门，例如：市场准入/合同/不正当竞争→市场监督管理部门；招标投标/政府采购→发展改革或财政部门；安全生产→应急管理部门；环境保护→生态环境部门；劳动用工→人力资源和社会保障部门；数据合规/个人信息→网信部门；知识产权→知识产权管理部门。
- 每个部门标注 role（主责 / 协管），并简述 reason 与 basis。
- 可参考图谱候选，但**必须过滤掉与本次事项明显不符的**；图谱缺了正确部门时可据合规常识补上，并在 basis 里说明判断依据（不确定时留空）。
- 只有当信息确实太少、无法判断时，authorities 才返回空列表，并在 follow_up 写明需要追问的内容（文件性质、涉及事项等）。
- 只输出结构化结果，不要寒暄。

示例：
- 涉及"招标投标/政府采购" → authorities:[{{"name":"发展和改革部门","role":"主责","reason":"负责招标投标活动监管","basis":"《招标投标法》"}},{{"name":"财政部门","role":"协管","reason":"政府采购监管","basis":"《政府采购法》"}}]
- 涉及"劳动用工" → authorities:[{{"name":"人力资源和社会保障部门","role":"主责","reason":"劳动关系与用工监管","basis":"《劳动法》"}}]
- 信息不足（"这个制度归谁管"）→ authorities:[]，follow_up:"请补充该文件的性质与涉及的具体事项"。"""


class AuthorityLocatorAgent:
    """Authority Locator Agent with 2-tier fallback"""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._chain = None

    def _ensure_chain(self):
        if self._chain is None:
            # 显式指定 function_calling：langchain-openai 1.x 默认 method="json_schema"，
            # DashScope 兼容接口不支持该 response_format（报 400 要求 messages 含 "json"）
            self._chain = get_llm(temperature=0.1).with_structured_output(
                AuthorityResult, method="function_calling"
            )
        return self._chain

    # ---------- Tier 1: 图谱候选 ----------
    @staticmethod
    def _tier_graph(matters: list[str]) -> list[str]:
        """图谱归口候选：查 (Matter)-[:GOVERNED_BY]->(Authority)。绝不抛异常。"""
        names: list[str] = []
        try:
            from src.knowledge_graph.graph_queries import GraphQueries
            q = GraphQueries()
            for m in matters:
                names.extend(q.get_matter_authorities(m))
            q.close()
        except Exception as e:
            logger.warning(f"[AuthorityLocator] graph tier error: {e}")
        return list(dict.fromkeys(names))

    # ---------- Tier 2: LLM 择优（结合事项 + 图谱候选） ----------
    def _tier_llm(self, matters: list[str], query: str, history: list, kg_candidates: list[str]) -> AuthorityResult:
        from langchain_core.prompts import ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages([
            ("system", AUTHORITY_PROMPT),
            ("user", "【涉及事项】{matters}\n【本轮输入】{query}\n【对话历史】{history}")
        ])
        msgs = prompt.format_messages(
            kg_candidates="、".join(kg_candidates) if kg_candidates else "（无）",
            matters="、".join(matters) if matters else "（未明确）",
            query=query,
            history=format_history(history),
        )
        return self._ensure_chain().invoke(msgs)

    def __call__(self, state: AgentState) -> dict:
        matters = state.get("matters", [])
        query = state.get("query", "")
        history = state.get("history", [])
        logger.info(f"[AuthorityLocator] Matters: {matters}")

        # Tier 1：图谱候选（可溯源 + 给 LLM 当参考；含噪声，不直接采用）
        kg_authorities = self._tier_graph(matters) if matters else []

        # Tier 2：LLM 择优做最终取舍——过滤噪声、图谱缺失时补正
        authorities: list[dict] = []
        follow_up = ""
        try:
            res = self._tier_llm(matters, query, history, kg_candidates=kg_authorities)
            authorities = [
                {"name": a.name, "role": a.role or "主责", "reason": a.reason or "", "basis": a.basis or ""}
                for a in (res.authorities or [])
            ][:5]
            follow_up = res.follow_up or ""
            logger.info(f"[AuthorityLocator] LLM -> {[a['name'] for a in authorities]}")
        except Exception as e:
            logger.error(f"[AuthorityLocator] LLM error: {e}")

        # 融合：LLM 优先；LLM 为空时回退图谱候选
        if authorities:
            source = "llm+graph" if kg_authorities else "llm"
        elif kg_authorities:
            authorities = [
                {"name": n, "role": "主责", "reason": "图谱事项-归口匹配", "basis": ""}
                for n in kg_authorities[:5]
            ]
            source = "graph"
        else:
            source = "none"

        logger.info(f"[AuthorityLocator] Recommended: {[a['name'] for a in authorities]} (source={source})")

        update = {
            "authorities": authorities,
            "messages": [f"[AuthorityLocator] Recommended: {[a['name'] for a in authorities]} (source={source})"]
        }

        if authorities:
            # 已能定位归口 → 取消上游"无事项→追问"的默认追问
            update["needs_clarification"] = False
        elif follow_up:
            # 确实信息不足 → 追问
            update["needs_clarification"] = True
            update["clarification"] = follow_up

        return update


# Singleton instance
authority_locator = AuthorityLocatorAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("AUTHORITY LOCATOR AGENT TEST (2-tier)")
    print("=" * 60)

    for matters in [["招标投标", "政府采购"], ["劳动用工"]]:
        print(f"\nMatters: {matters}")
        result = authority_locator({"matters": matters, "query": "归口谁管", "history": []})
        print(f"  -> {[a['name'] for a in result['authorities']]}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Authority locator tests completed!")
    print("=" * 60)
