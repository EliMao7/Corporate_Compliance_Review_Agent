"""
Safety Checker Agent（本节点**零 LLM**，纯规则）

合规系统的兜底闸门，必须**永远执行、结果可预期**，不可被模型行为绕过：
1. 强制注入免责声明（disclaimers 必须非空）；
2. is_redline 时把高风险红线提示**置顶** warnings；
3. 拦截结论性表述（CONCLUSION_PATTERNS）——系统不下"合法/违法"结论，替换为依据式表述。
"""

import re
import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.common.logger import setup_logger
from src.agents.state import AgentState, SeverityLevel

logger = setup_logger(__name__, "agents.log")


# ============================================================
# 安全规则常量（合规域）—— 与 TDD §6.1/§6.2/§6.3 逐字一致
# ============================================================

# 强制免责声明（DO NOT MODIFY）—— disclaimers 必须非空
DISCLAIMER_TEMPLATE = (
    "🔴 重要声明：本系统只提供法规依据检索与风险提示，不提供法律意见，"
    "也不对文件是否合法作出结论。以上内容请由法务或执业律师复核后使用。"
    "重大事项请咨询专业律师或主管部门。"
)

# 高风险红线：依据国办发〔2018〕37号等对行政规范性文件的要求
REDLINE_PATTERNS = [
    "无依据增设行政许可", "变相设置审批前置",
    "无依据增设行政处罚", "自行设定罚款数额",
    "无依据增设行政强制", "超越制定权限",
    "违法减损权益", "违反公平竞争审查",
]

# 红线置顶提示
REDLINE_WARNING = (
    "🚨 高风险红线提示：所描述的情形可能触及「无依据增设行政许可/行政处罚、自行设定罚款数额、"
    "超越制定权限」等红线。行政规范性文件设定许可、处罚、强制须有上位法依据并履行法定程序，"
    "**企业制度无权自行设定行政处罚**。请务必对照上位法核对，并建议咨询主管部门或执业律师。"
)

# 结论性表述拦截（系统不下"合法/违法"结论）
CONCLUSION_PATTERNS = [
    r"该(文件|制度|条款)是(合法|违法)的",
    r"符合法律规定，可以(直接|放心)(发布|执行)",
    r"不(违反|触犯)法律",
]

# 命中结论性表述后替换成的"依据式表述"
CONCLUSION_REPLACEMENT = "未检索到与该表述直接冲突的条款，但仍建议由法务复核后发布。"


def sanitize_conclusions(text: str) -> str:
    """把结论性表述替换为依据式表述（纯规则，deterministic）。

    供本节点就地清洗已生成的文本字段，也供 answer_fusion 对最终拼装结果做后置兜底。
    """
    if not text:
        return text
    cleaned = text
    for pat in CONCLUSION_PATTERNS:
        cleaned = re.sub(pat, CONCLUSION_REPLACEMENT, cleaned)
    return cleaned


class SafetyCheckerAgent:
    """
    Safety Checker Agent

    This is the MOST IMPORTANT agent in the system.
    It ensures all compliance outputs include mandatory disclaimers.
    The disclaimer is added programmatically by RULES, NOT by LLM.
    """

    def __call__(self, state: AgentState) -> dict:
        """Process state and return update"""
        intent = state.get("intent", "")
        severity = state.get("severity", "")
        is_redline = state.get("is_redline", False)
        risk_signals = state.get("risk_signals", [])
        measures = state.get("measures", [])
        regulation_answer = state.get("regulation_answer", "")

        logger.info("[SafetyChecker] Checking safety...")

        disclaimers = []
        warnings = []

        # 1. MANDATORY: Always add disclaimer (DO NOT SKIP!)
        disclaimers.append(DISCLAIMER_TEMPLATE)

        # 2. 高风险红线：把红线提示放在 warnings 首位（置顶）
        if is_redline or severity == SeverityLevel.REDLINE.value:
            warnings.insert(0, REDLINE_WARNING)
            logger.warning(f"[SafetyChecker] REDLINE detected! risk_signals={risk_signals}")

        # 3. 措施建议一律提醒：企业制度无权自行设定行政处罚（即便模型未提及）
        if measures or intent == "measure":
            disclaimers.append(
                "⚠️ 措施提醒：企业制度无权自行设定行政处罚；涉及许可、处罚、强制设定的，"
                "必须有上位法依据并经法定程序，请由法务复核。"
            )

        # 4. 较高风险提示
        if severity == SeverityLevel.HIGH.value:
            warnings.append("⚠️ 该文件/事项存在较高合规风险，建议在发布或执行前由法务做一次完整比对。")

        # 5. 拦截结论性表述：就地清洗已生成的法规知识文本
        cleaned = sanitize_conclusions(regulation_answer)
        if cleaned != regulation_answer:
            logger.info("[SafetyChecker] conclusion-style statement intercepted & rewritten")

        logger.info(f"[SafetyChecker] Added {len(disclaimers)} disclaimers, {len(warnings)} warnings")

        return {
            "disclaimers": disclaimers,
            "warnings": warnings,
            "regulation_answer": cleaned,
            "messages": [f"[SafetyChecker] Added {len(disclaimers)} disclaimers, {len(warnings)} warnings"]
        }


# Singleton instance
safety_checker = SafetyCheckerAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("SAFETY CHECKER AGENT TEST (zero-LLM, pure rules)")
    print("=" * 60)

    # Test 1: Normal query
    print("\n[Test 1] Normal query")
    state = {"query": "我们准备发一份《供应商管理办法》", "matters": ["行政许可"], "severity": "medium", "measures": []}
    result = safety_checker(state)
    print(f"  Disclaimers: {len(result['disclaimers'])}")
    print(f"  Warnings: {len(result['warnings'])}")
    assert result["disclaimers"], "disclaimers 必须非空"
    assert result["disclaimers"][0] == DISCLAIMER_TEMPLATE

    # Test 2: Redline query
    print("\n[Test 2] Redline query")
    state = {"query": "无依据增设行政许可", "matters": ["行政许可"], "severity": "redline",
             "is_redline": True, "risk_signals": ["无依据增设行政许可"], "measures": [{"name": "x"}]}
    result = safety_checker(state)
    print(f"  Disclaimers: {len(result['disclaimers'])}")
    print(f"  Warnings: {len(result['warnings'])}")
    assert result["warnings"] and result["warnings"][0] == REDLINE_WARNING, "红线提示应置顶"

    # Test 3: Conclusion interception
    print("\n[Test 3] Conclusion interception")
    dirty = "该文件是合法的。符合法律规定，可以直接发布。"
    clean = sanitize_conclusions(dirty)
    print(f"  before: {dirty}")
    print(f"  after : {clean}")
    assert clean != dirty and CONCLUSION_REPLACEMENT in clean

    print("\n" + "=" * 60)
    print("[SUCCESS] Safety checker tests passed!")
    print("=" * 60)
