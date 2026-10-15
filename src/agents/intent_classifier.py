"""
Intent Classifier Agent
Classifies user query into intent types: review/measure/regulation/redline/general

意图类型的唯一主人是 src/agents/state.py 的 IntentType（R1），本模块只 import 使用，
不另定义一份意图字符串列表。
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
from src.agents.state import AgentState, IntentType

logger = setup_logger(__name__, "agents.log")


class IntentResult(BaseModel):
    """Intent classification result（结构化输出的字段名即 API 契约字段名）"""
    intent: str = Field(description="Intent type: review/measure/regulation/redline/general")
    intent_confidence: float = Field(description="Confidence score: 0-1")


SYSTEM_PROMPT = """你是资深的企业合规审查意图识别专家。请分析用户的问题，判断其意图类型。

意图类型定义（五类，唯一候选集）：
1. review（合规排查）：用户描述某份文件/某个事项，想知道**该查哪些法律法规、归口哪个部门管**、有没有冲突。
2. measure（措施建议）：用户想知道**怎么改才合规**——针对已知风险点给出合规措施与整改方向。
3. regulation（法规知识）：用户询问**某部法规/某个法条的内容与适用范围**（如"《招标投标法》第XX条怎么规定"）。
4. redline（高风险红线）：用户描述的情形**涉及无依据增设行政许可 / 擅自加设行政处罚或罚款 / 无依据增设行政强制 / 超越制定权限**等红线问题。
5. general（通用咨询）：非合规问题，如问候、闲聊、与法规审查无关的提问。

判断规则：
- 出现"帮我看/查一下这份文件有没有问题""该查哪些法""归口谁管""有没有冲突"→ review
- 出现"怎么改才合规""给些整改措施""如何规避风险"→ measure
- 出现"某法/某条怎么规定""适用范围是什么"→ regulation
- 出现"我们自己设个罚款/许可/审批""能不能直接规定处罚""吊销资质"→ redline
- 其他非合规问题 → general

Few-shot 示例：
用户：我们准备发一份《供应商管理办法》，帮我看下有没有跟法规冲突的地方
意图：review，置信度：0.93

用户：这份考核办法想加一条"违规的处5万元以上罚款"，怎么改才合规？
意图：measure，置信度：0.90

用户：《中华人民共和国招标投标法》第 43 条怎么规定的？
意图：regulation，置信度：0.94

用户：我们公司想在制度里直接规定，未完成业绩的可以吊销其资质，行不行？
意图：redline，置信度：0.97

用户：你好，今天天气怎么样？
意图：general，置信度：0.99

用户：这个制度是我们自己写的，直接加上"必须经我方审批"这一条
意图：redline，置信度：0.95

多轮示例：
历史：助手：为了定位归口部门，请补充这份文件的性质… / 用户：是行政规范性文件
→ 结合历史，本轮是在补充排查所需信息，意图仍为 review。

请结合【对话历史】与【本轮用户输入】，返回意图类型与置信度。
【准确率目标】在合规域测试集上意图分类准确率应 > 90%；把握不足时给出较低置信度而非硬猜。"""


class IntentClassifierAgent:
    """Intent Classifier Agent"""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译（模块导入期不依赖密钥）
        self._chain = None
        # 显式指定 function_calling：langchain-openai 1.x 默认 method="json_schema"，
        # DashScope 兼容接口不支持该 response_format（报 400 要求 messages 含 "json"）
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("user", "【对话历史】\n{history}\n\n【本轮用户输入】\n{query}")
        ])

    def _ensure_chain(self):
        if self._chain is None:
            self._chain = get_llm(temperature=0.1).with_structured_output(
                IntentResult, method="function_calling"
            )
        return self._chain

    def __call__(self, state: AgentState) -> dict:
        """Process state and return update"""
        query = state.get("query", "")
        history_text = format_history(state.get("history", []))
        logger.info(f"[IntentClassifier] Processing: {query[:50]}...")

        try:
            chain = self._ensure_chain()
            messages = self.prompt.format_messages(history=history_text, query=query)
            result = chain.invoke(messages)

            logger.info(f"[IntentClassifier] Result: {result.intent} ({result.intent_confidence:.2f})")

            return {
                "intent": result.intent,
                "intent_confidence": result.intent_confidence,
                "messages": [f"[IntentClassifier] Detected: {result.intent} ({result.intent_confidence:.2f})"]
            }

        except Exception as e:
            logger.error(f"[IntentClassifier] Error: {e}")
            return {
                "intent": IntentType.GENERAL,
                "intent_confidence": 0.5,
                "messages": [f"[IntentClassifier] Error, default to general: {e}"]
            }


# Singleton instance
intent_classifier = IntentClassifierAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("INTENT CLASSIFIER AGENT TEST")
    print("=" * 60)

    # 离线可跑：结构性断言（不依赖 LLM 密钥）
    print("\n[Structural checks]")
    assert len(IntentType.ALL) == 5, "意图数量应为 5"
    for name in IntentType.ALL:
        assert name in SYSTEM_PROMPT, f"提示词未覆盖意图: {name}"
        print(f"  - {name} 覆盖于提示词: OK")

    test_queries = [
        "我们准备发一份《供应商管理办法》，帮我看下有没有跟法规冲突的地方",
        "这份考核办法想加一条'违规的处5万元以上罚款'，怎么改才合规？",
        "《中华人民共和国招标投标法》第 43 条怎么规定的？",
        "我们公司想在制度里直接规定，未完成业绩的可以吊销其资质，行不行？",
        "你好",
    ]

    for query in test_queries:
        print(f"\nQuery: {query}")
        state = {"query": query}
        result = intent_classifier(state)
        print(f"  Intent: {result['intent']} (confidence: {result['intent_confidence']:.2f})")

    print("\n" + "=" * 60)
    print("[SUCCESS] Intent classifier tests completed!")
    print("=" * 60)
