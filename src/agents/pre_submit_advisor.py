"""
Pre-submit Advisor Agent（提交前建议）
根据涉及事项与归口部门，给出简洁、实用的**提交前自查清单** + **需同步比对的上位法清单** + urgency。

坚持"建议必须与事项相关"：上位法清单由图谱 (Matter)-[:REQUIRES_EVIDENCE]->(Evidence)
提供，LLM 只做取舍与补充，不凭空罗列无关法规。
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
from src.agents.state import AgentState

logger = setup_logger(__name__, "agents.log")


class PreSubmitAdvice(BaseModel):
    """Pre-submit advice result"""
    advice: list[str] = Field(description="3-5 条简洁的提交前自查清单（每条一行，含依据核对/程序要件/表述规范，不下合法/违法结论）")
    urgency: str = Field(default="中", description="紧急程度：高 / 中 / 低（涉及红线或处罚/许可设定的为高）")


SYSTEM_PROMPT = """你是资深的企业合规文件审核专家。根据【涉及事项】【归口部门】【应同步比对的上位法】，
给出 3-5 条简洁、实用的**提交前自查清单**。

要求：
- 清单聚焦"提交/发布前必须自查的要点"，酌情包含：
  · 是否逐条比对了上位法（本清单给出的上位法必须与该事项合理相关，**绝不罗列无关法规**）；
  · 是否误设了行政处罚/行政许可/行政强制（企业制度无权自行设定行政处罚）；
  · 程序要件（依据是否明确、是否履行公平竞争审查/合法性审核、是否保留告知与异议渠道）；
  · 表述规范（避免"必须经我方审批"等变相审批前置表述）。
- 不给出"合法/违法"的结论，只提示"应比对的依据"与"需注意的风险"。
- urgency：涉及红线或处罚/许可设定的判为"高"，可能减损权益的判为"中"，一般表述问题判为"低"。
- 每条一行、简洁，不要寒暄。"""


class PreSubmitAdvisorAgent:
    """Pre-submit Advisor Agent (LLM-generated, matter-relevant)."""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._chain = None
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("user", "【涉及事项】{matters}\n【归口部门】{authorities}\n【应同步比对的上位法】{evidences}")
        ])

    def _ensure_chain(self):
        if self._chain is None:
            # 显式指定 function_calling：langchain-openai 1.x 默认 method="json_schema"，
            # DashScope 兼容接口不支持该 response_format（报 400 要求 messages 含 "json"）
            self._chain = get_llm(temperature=0.2).with_structured_output(
                PreSubmitAdvice, method="function_calling"
            )
        return self._chain

    @staticmethod
    def _tier_graph(matters: list[str]) -> list[str]:
        """图谱上位法候选：查 (Matter)-[:REQUIRES_EVIDENCE]->(Evidence)。绝不抛异常。"""
        names: list[str] = []
        try:
            from src.knowledge_graph.graph_queries import GraphQueries
            q = GraphQueries()
            for m in matters:
                names.extend(q.get_matter_evidences(m))
            q.close()
        except Exception as e:
            logger.warning(f"[PreSubmitAdvisor] graph tier error: {e}")
        return list(dict.fromkeys(names))

    def __call__(self, state: AgentState) -> dict:
        matters = state.get("matters", [])
        authorities = state.get("authorities", [])
        logger.info(f"[PreSubmitAdvisor] Matters: {matters}, Authorities: {len(authorities)}")

        evidences = self._tier_graph(matters)
        authority_names = [a.get("name", "") for a in authorities if isinstance(a, dict) and a.get("name")]

        advice: list[str] = []
        urgency = ""
        try:
            msgs = self.prompt.format_messages(
                matters="、".join(matters) if matters else "（未明确）",
                authorities="、".join(authority_names) if authority_names else "（未明确）",
                evidences="、".join(evidences) if evidences else "（图谱未匹配，请人工核对）",
            )
            res = self._ensure_chain().invoke(msgs)
            advice = res.advice or []
            urgency = res.urgency or ""
        except Exception as e:
            logger.warning(f"[PreSubmitAdvisor] LLM advice failed: {e}")
            advice = []

        # 确定性兜底（LLM 失败时仍给出安全的通用自查清单）
        if not advice:
            advice = [
                "逐条核对该文件是否与上位法（法律、行政法规、部门规章）存在冲突",
                "确认是否误设行政处罚、行政许可、行政强制（企业制度无权自行设定行政处罚）",
                "核对程序要件：依据是否明确、是否需履行合法性审核/公平竞争审查",
                "检查表述是否构成变相审批前置（如'必须经我方审批'）",
            ]
            if authority_names:
                advice.append(f"建议同时咨询归口部门：{'、'.join(authority_names[:3])}")

        # 上位法清单与 urgency 作为结构化补充写入 advice（无独立 state 字段）
        if evidences:
            advice.append("应同步比对上位法：" + "、".join(evidences))
        if urgency:
            advice.insert(0, f"【紧急程度】{urgency}")

        logger.info(f"[PreSubmitAdvisor] Generated {len(advice)} advice items")
        return {
            "advice": advice,
            "messages": [f"[PreSubmitAdvisor] Generated {len(advice)} advice items (evidences={evidences})"]
        }


# Singleton instance
pre_submit_advisor = PreSubmitAdvisorAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("PRE-SUBMIT ADVISOR AGENT TEST")
    print("=" * 60)

    state = {"matters": ["招标投标"], "authorities": [{"name": "发展和改革部门"}]}
    result = pre_submit_advisor(state)
    assert result["advice"], "自查清单不得为空"
    print(f"  advice items: {len(result['advice'])}")
    for a in result["advice"][:3]:
        print(f"    - {a}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Pre-submit advisor tests completed!")
    print("=" * 60)
