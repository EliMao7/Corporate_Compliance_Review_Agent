"""
Matter Detector Agent（要素识别）
从用户描述（含多轮历史）中抽取：文件类型 doc_type、涉及事项 matters、适用主体、
高风险信号 risk_signals，并评估风险等级 severity / 是否命中红线 is_redline。
信息不足时不硬答，触发追问（needs_clarification）。
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
from src.agents.state import AgentState, SeverityLevel, IntentType
from src.agents.safety_checker import REDLINE_PATTERNS

logger = setup_logger(__name__, "agents.log")

# 信息不足时的标准追问话术
CLARIFY_PROMPT = (
    "为了准确为您定位需要比对的法规与归口部门，还需要您补充一些信息：\n"
    "① 这份**文件/事项的性质**（如管理办法、实施细则、合同、公告、通知、行政规范性文件）；"
    "② 它**涉及哪些事项**（如行政许可、行政处罚、招标投标、政府采购、安全生产、劳动用工、数据合规等）；"
    "③ 制定/发布**主体**是谁（企业、行业协会、还是行政机关）。\n"
    "您可以直接把上述情况描述给我，我来帮您梳理应比对的上位法与注意事项。"
)


class MatterResult(BaseModel):
    """Matter detection result（要素识别结构化输出）"""
    doc_type: str = Field(default="", description="文件类型：管理办法/实施细则/合同/公告/通知/行政规范性文件/其他；不确定留空")
    matters: list[str] = Field(description="涉及事项列表（标准术语，如：行政许可、行政处罚、招标投标、政府采购、安全生产、环境保护、劳动用工、数据合规、知识产权）")
    subject: str = Field(default="", description="适用/制定主体（如：企业、行业协会、行政机关），不确定留空")
    risk_signals: list[str] = Field(description="高风险信号原文短语（如：'处5万元以上罚款'、'必须经我方审批'、'吊销资质'）；无则空列表")
    severity: str = Field(description="风险等级: low/medium/high/redline")
    is_redline: bool = Field(description="是否命中高风险红线（无依据增设许可/处罚/强制、超越权限等）")


SYSTEM_PROMPT = """你是资深的企业合规文件审查专家（要素识别）。请从【对话历史】与【本轮用户输入】中抽取合规要素。

【核心原则：以本轮为主，历史仅用于"承接"——绝不跨话题累加事项】
1. 优先抽取【本轮用户输入】中的文件类型、涉及事项与风险信号。
2. 历史对话**仅在本轮是承接/追问**时参考：本轮本身没有新内容、只是"那该查哪些法""归口谁管"等指代上文的句子时，才继承上一轮的要素。
3. **一旦本轮提出新的文件或新的事项（换了话题），就只取本轮要素，绝不把历史里不相关的事项累加进来。**

【抽取要求】
- doc_type（文件类型）：管理办法 / 实施细则 / 合同 / 公告 / 通知 / 行政规范性文件 / 其他；不确定则留空。
- matters（涉及事项）：用标准术语，例如 行政许可 / 行政处罚 / 行政强制 / 招标投标 / 政府采购 / 安全生产 / 环境保护 / 劳动用工 / 数据合规 / 知识产权 / 公平竞争审查。
- subject（适用主体）：企业 / 行业协会 / 行政机关，不确定则留空。
- risk_signals（高风险信号）：抽取可能导致文件与上位法冲突的**原文短语**，例如"处5万元以上罚款""必须经我方审批""吊销资质""未中标者不得再参与"。
- severity（风险等级）：
  - redline：涉及**无依据增设行政许可 / 变相设置审批前置 / 无依据增设行政处罚或自行设定罚款数额 / 无依据增设行政强制 / 超越制定权限 / 违法减损权益 / 违反公平竞争审查**等红线。
  - high：可能明显减损相对人权益、加重义务，或与上位法存在实质冲突风险。
  - medium：表述不规范、依据不清、程序要件缺失。
  - low：无实质风险。
- is_redline：当且仅当命中上述 redline 情形时为 true。

【不硬答】当文件/事项信息严重不足（既无文件类型、又无任何涉及事项）时，matters 返回空列表，交由系统向用户追问，绝不凭空臆断。

【对比示例（务必区分 A 与 B）】
示例 A（本轮是承接 → 继承上一轮）：
历史：用户：我们准备发一份《供应商管理办法》/ 助手：…请补充文件性质…
本轮：是内部管理办法，主要是供应商准入
doc_type：管理办法；matters：["行政许可"]；severity：medium；is_redline：false

示例 B（本轮换话题 → 只取本轮）：
历史：用户：我们准备发一份《供应商管理办法》/ 助手：…推荐市场监督管理部门…
本轮：那《招标投标法》第 43 条怎么规定的？
doc_type：""；matters：[]；severity：low；is_redline：false

示例 C（命中红线）：
本轮：这份考核办法想加一条"违规的处5万元以上罚款"
doc_type：管理办法；matters：["行政处罚"]；risk_signals：["处5万元以上罚款"]；severity：redline；is_redline：true

请分析下面的对话与本轮输入，按上述原则抽取要素。"""


class MatterDetectorAgent:
    """Matter Detector Agent —— 要素识别：从用户描述中抽取文件类型 / 涉及事项 / 高风险信号"""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._chain = None
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("user", "【对话历史】\n{history}\n\n【本轮用户输入】\n{query}")
        ])

    def _ensure_chain(self):
        if self._chain is None:
            self._chain = get_llm(temperature=0.1).with_structured_output(
                MatterResult, method="function_calling"
            )
        return self._chain

    @staticmethod
    def _hits_redline(query: str, risk_signals: list[str]) -> bool:
        """确定性兜底：文本命中 REDLINE_PATTERNS 即判定红线（不依赖模型行为）。"""
        text = query + " " + " ".join(risk_signals or [])
        return any(p in text for p in REDLINE_PATTERNS)

    def __call__(self, state: AgentState) -> dict:
        """Process state and return update"""
        query = state.get("query", "")
        history = state.get("history", [])
        history_text = format_history(history)
        intent = state.get("intent", "")
        logger.info(f"[MatterDetector] Processing: {query[:50]}... (history turns: {len(history)})")

        try:
            chain = self._ensure_chain()
            messages = self.prompt.format_messages(history=history_text, query=query)
            result = chain.invoke(messages)

            is_redline = bool(result.is_redline) or self._hits_redline(query, result.risk_signals)
            severity = SeverityLevel.REDLINE.value if is_redline else (result.severity or SeverityLevel.LOW.value)

            logger.info(
                f"[MatterDetector] doc_type={result.doc_type}, matters={result.matters}, "
                f"risk_signals={result.risk_signals}, severity={severity}, is_redline={is_redline}"
            )

            update = {
                "doc_type": result.doc_type or "",
                "matters": result.matters or [],
                "risk_signals": result.risk_signals or [],
                "severity": severity,
                "is_redline": is_redline,
                "messages": [f"[MatterDetector] doc_type={result.doc_type} matters={result.matters} "
                             f"risk_signals={result.risk_signals} severity={severity} is_redline={is_redline}"]
            }

            # 追问仅在「合规排查(review) + 无事项锚点」时触发：没事项就无法定位归口与依据，必须问。
            # 措施建议(measure)无事项是合法的（交给能用工具的法规知识 Agent 兜底），不追问。
            if intent == IntentType.REVIEW and not result.matters:
                update["needs_clarification"] = True
                update["clarification"] = CLARIFY_PROMPT
                logger.info("[MatterDetector] review without matters -> needs clarification")

            return update

        except Exception as e:
            logger.error(f"[MatterDetector] Error: {e}")
            # 确定性兜底：即便 LLM 失败，也用红线词表做一次纯规则扫描
            is_redline = self._hits_redline(query, [])
            return {
                "doc_type": "",
                "matters": [],
                "risk_signals": [],
                "severity": SeverityLevel.REDLINE.value if is_redline else SeverityLevel.MEDIUM.value,
                "is_redline": is_redline,
                "messages": [f"[MatterDetector] Error: {e}"]
            }


# Singleton instance
matter_detector = MatterDetectorAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("MATTER DETECTOR AGENT TEST")
    print("=" * 60)

    # 离线可跑的结构性/确定性断言
    print("\n[Deterministic checks]")
    assert matter_detector._hits_redline("无依据增设行政许可", []) is True
    assert matter_detector._hits_redline("普通的内部管理规定", []) is False
    print("  redline pattern scan: OK")

    test_cases = [
        {"query": "我们准备发一份《供应商管理办法》，有没有跟法规冲突的地方", "history": []},
        {"query": "这份考核办法想加一条'违规的处5万元以上罚款'，怎么改才合规？", "history": []},
        # 多轮：本轮是承接，无新要素
        {
            "query": "那该查哪些法？",
            "history": [
                {"role": "user", "content": "我们准备发一份《供应商管理办法》"},
                {"role": "assistant", "content": "了解到这是管理办法…"},
            ],
        },
    ]

    for tc in test_cases:
        print(f"\nQuery: {tc['query']}  | history turns: {len(tc['history'])}")
        state = {"query": tc["query"], "history": tc["history"], "intent": IntentType.REVIEW}
        result = matter_detector(state)
        print(f"  doc_type: {result['doc_type']}")
        print(f"  matters: {result['matters']}")
        print(f"  risk_signals: {result['risk_signals']}")
        print(f"  severity={result['severity']} is_redline={result['is_redline']}")
        print(f"  Needs clarification: {result.get('needs_clarification', False)}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Matter detector tests completed!")
    print("=" * 60)
