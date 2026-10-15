"""
Measure Advisor Agent（措施建议）
针对"有事项/风险点的合规咨询"，给出**合法、可落地**的合规措施。

设计要点：图谱措施仅作参考，由 LLM 依据合规红线审慎取舍，并正面回答用户的实际问题
（如"怎么改才合规"）。强制提示"企业制度无权自行设定行政处罚"类红线，绝不教唆规避上位法。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.common.memory import format_history
from src.agents.state import AgentState

logger = setup_logger(__name__, "agents.log")

# LLM 失败时的确定性兜底建议（绝不输出空答案，且强制含红线提示）
SAFE_FALLBACK_ADVICE = (
    "该合规问题建议由法务结合具体文件与上位法评估后再定稿。"
    "企业制度无权自行设定行政处罚；涉及许可、处罚、强制设定的，必须有上位法依据并经法定程序，"
    "必要时请咨询主管部门或执业律师。"
)


class MeasureItem(BaseModel):
    """单条合规措施"""
    name: str = Field(description="措施名称，如：改为援引上位法既有条款 / 删除自设罚款表述")
    scenario: str = Field(default="", description="适用场景（该措施针对什么情形）")
    key_points: list[str] = Field(default_factory=list, description="落地要点 1-4 条")


class MeasureAdvice(BaseModel):
    """LLM 合规措施建议结果"""
    advice: str = Field(description="针对用户问题的合规指导（2-4 句）：风险定性、整改方向与必须遵守的边界")
    measures: list[MeasureItem] = Field(description="合规措施 0-3 条；拿不准或不适合自行处理时返回空列表")


SYSTEM_PROMPT = """你是谨慎的企业合规措施建议专家。请根据【涉及事项】【风险信号】【用户问题】给出**合法、可落地**的合规措施。
【图谱参考措施】（由"(事项)-[:ADDRESSED_BY]->(措施)"聚合而来，可能含与本次事项无关的措施，仅供你判断参考，切勿直接照搬）：{kg_measures}

合规红线（必须遵守）：
1. **企业制度无权自行设定行政处罚**，也不得变相设置审批前置、无依据增设行政许可/行政强制。凡涉及处罚、许可、强制设定的，一律改为"援引上位法既有条款"或"删除自行设定内容"，并走法定程序。
2. **绝不**建议任何规避、架空上位法或规避监管的做法；不教唆、不提供"打擦边球"方案。
3. 必须正面回答用户的实际问题（如"怎么改才合规"）：给出可落地的整改方向（修改表述、补充依据、履行程序、保留告知/异议渠道等）。
4. 涉及处罚/许可设定的，明确提示"须有上位法依据并经法定程序，建议咨询主管部门或执业律师"。
只输出结构化结果，不要寒暄。"""


class MeasureAdvisorAgent:
    """Measure Advisor Agent (LLM-based, compliance-safe)."""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._chain = None
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("user", "【涉及事项】{matters}\n【风险信号】{risk_signals}\n【用户问题】{query}\n【对话历史】{history}")
        ])

    def _ensure_chain(self):
        if self._chain is None:
            # 显式指定 function_calling：langchain-openai 1.x 默认 method="json_schema"，
            # DashScope 兼容接口不支持该 response_format（报 400 要求 messages 含 "json"）
            self._chain = get_llm(temperature=0.1).with_structured_output(
                MeasureAdvice, method="function_calling"
            )
        return self._chain

    @staticmethod
    def _kg_measures(matters: list[str]) -> list[dict]:
        """图谱候选措施（仅作参考，含噪声）"""
        items: list[dict] = []
        try:
            from src.knowledge_graph.graph_queries import GraphQueries
            q = GraphQueries()
            for m in matters:
                items.extend(q.get_matter_measures(m))
            q.close()
        except Exception as e:
            logger.warning(f"[MeasureAdvisor] kg measures failed: {e}")
        # 去重（按 name）
        seen, out = set(), []
        for it in items:
            n = it.get("name")
            if n and n not in seen:
                seen.add(n)
                out.append(it)
        return out[:10]

    def __call__(self, state: AgentState) -> dict:
        query = state.get("query", "")
        matters = state.get("matters", [])
        risk_signals = state.get("risk_signals", [])
        history = state.get("history", [])
        logger.info(f"[MeasureAdvisor] Query: {query[:50]}..., matters={matters}")

        kg_measures = self._kg_measures(matters)
        advice_text = ""
        measure_items: list[dict] = []
        try:
            msgs = self.prompt.format_messages(
                kg_measures="、".join(m.get("name", "") for m in kg_measures) if kg_measures else "（无）",
                matters="、".join(matters) if matters else "（未明确）",
                risk_signals="、".join(risk_signals) if risk_signals else "（无）",
                query=query,
                history=format_history(history),
            )
            res = self._ensure_chain().invoke(msgs)
            advice_text = res.advice or ""
            measure_items = [
                {"name": m.name, "scenario": m.scenario or "", "key_points": m.key_points or []}
                for m in (res.measures or [])
            ][:3]
            logger.info(f"[MeasureAdvisor] LLM measures={[m['name'] for m in measure_items]}")
        except Exception as e:
            logger.error(f"[MeasureAdvisor] LLM error: {e}")

        # 兜底：LLM 失败也给安全建议，绝不输出空答案
        if not advice_text:
            advice_text = SAFE_FALLBACK_ADVICE

        return {
            "measures": measure_items,
            "regulation_answer": advice_text,   # 措施说明文本，由 answer_fusion 渲染
            "messages": [f"[MeasureAdvisor] advice={len(advice_text)} chars, measures={[m['name'] for m in measure_items]}"]
        }


# Singleton instance
measure_advisor = MeasureAdvisorAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("MEASURE ADVISOR AGENT TEST")
    print("=" * 60)

    # 离线可跑：验证错误兜底路径（无 key 时 LLM 抛错 → 确定性合规建议）
    print("\n[Deterministic fallback check]")
    assert "企业制度无权自行设定行政处罚" in SAFE_FALLBACK_ADVICE, "兜底建议必须含红线提示"
    print("  SAFE_FALLBACK_ADVICE contains redline caution: OK")

    print("\n[Agent run]")
    state = {"query": "这份考核办法想加一条'违规的处5万元以上罚款'，怎么改才合规？",
             "matters": ["行政处罚"], "risk_signals": ["处5万元以上罚款"], "history": []}
    result = measure_advisor(state)
    assert result["regulation_answer"], "建议不得为空"
    print(f"  advice chars: {len(result['regulation_answer'])}")
    print(f"  measures: {[m['name'] for m in result['measures']]}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Measure advisor tests completed!")
    print("=" * 60)
