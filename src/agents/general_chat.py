"""
General Chat Agent（通用咨询）
处理非合规问题：礼貌拒答 + 能力引导。**直达 END，不经安全检查**（有意取舍）。

⚠️ 边界：本分支的回答模板**不含任何法律判断**（只做能力引导），因此可以不走安全检查。
若未来让通用分支也能讲法规，**必须接回安全检查**（见 TDD §6.7）。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.prompts import ChatPromptTemplate

from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.agents.state import AgentState

logger = setup_logger(__name__, "agents.log")

# 能力引导模板（确定性兜底复用，不含任何法律判断）
CAPABILITY_TEMPLATE = """您好！我是企业合规审查助手，专注于帮您做**法规依据检索与风险提示**。我可以：

1. **合规排查**：描述一份文件或事项，我帮您梳理应比对的上位法、归口部门与风险点；
2. **措施建议**：针对已知风险点，给出可落地的合规整改方向；
3. **法规知识**：查询某部法规/某个法条的内容与适用范围；
4. **高风险红线识别**：无依据增设许可/处罚、超越权限等情形提示。

（说明：本系统只提供依据与风险提示，不提供法律意见，也不对文件是否合法作出结论。）
请问有什么可以帮您的吗？"""

SYSTEM_PROMPT = """你是企业合规审查助手的通用咨询分支。当用户问与法规审查无关的问题时，
礼貌地回应并引导用户使用合规功能。

你可以回答的问题类型：
- 合规排查：描述文件/事项，梳理应比对法规与归口部门
- 措施建议：怎么改才合规
- 法规知识：某法条的内容与适用范围
- 高风险红线：无依据增设许可/处罚等提示

【严格约束】
- 只做能力引导，**不得对任何文件作出"合法/违法"的判断**，不得提供具体法律意见。
- 若用户的问题其实涉及合规审查，应引导其按上述方式重新描述，而不是给出结论。

示例回答：
用户：你好
回答：您好！我是企业合规审查助手，可以帮您做合规排查、措施建议、法规知识问答与高风险红线识别。请问您想了解哪方面？

请回答用户的问题。"""


class GeneralChatAgent:
    """General Chat Agent"""

    def __init__(self):
        # 懒加载：无 API key 时不在此构建 LLM，保证 graph 可离线编译
        self._llm = None
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("user", "{query}")
        ])

    def _ensure_llm(self):
        if self._llm is None:
            self._llm = get_llm(temperature=0.5)
        return self._llm

    def __call__(self, state: AgentState) -> dict:
        """Process state and return update"""
        query = state.get("query", "")
        logger.info(f"[GeneralChat] Query: {query[:50]}...")

        try:
            messages = self.prompt.format_messages(query=query)
            answer = self._ensure_llm().invoke(messages).content
        except Exception as e:
            logger.error(f"[GeneralChat] Error: {e}")
            answer = CAPABILITY_TEMPLATE

        return {
            "final_answer": answer,
            "messages": ["[GeneralChat] Responded to general query"]
        }


# Singleton instance
general_chat = GeneralChatAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("GENERAL CHAT AGENT TEST")
    print("=" * 60)

    # 结构性断言：能力模板只做能力引导，不含任何"合法/违法"结论式判断
    for banned in ("该文件是合法的", "该文件是违法的", "符合法律规定，可以直接发布"):
        assert banned not in CAPABILITY_TEMPLATE, f"通用分支模板不得含结论式判断: {banned}"
    assert "不提供法律意见" in CAPABILITY_TEMPLATE, "通用分支须声明不提供法律意见"

    result = general_chat({"query": "你好"})
    assert result["final_answer"], "回答不得为空"
    print("  answer chars:", len(result["final_answer"]))

    print("\n" + "=" * 60)
    print("[SUCCESS] General chat tests completed!")
    print("=" * 60)
