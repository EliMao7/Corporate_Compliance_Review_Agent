"""
LangGraph Agent State Definition
Defines the global state shared across all agents in the corporate-compliance review system.

Usage:
    from src.agents.state import AgentState

    # Initialize state
    state: AgentState = {
        "query": "我们准备发一份《供应商管理办法》，帮我看下有没有跟法规冲突的地方",
        "messages": []
    }
"""

import operator
from typing import TypedDict, Optional, Annotated


# ============================================================
# Intent Types —— R1：Agent 意图类型的**唯一主人**
# ============================================================

class IntentType:
    """Intent type constants（合规审查考察链路）"""
    REVIEW = "review"          # 合规排查：描述文件/事项，想知道该查哪些法、归口谁管
    MEASURE = "measure"        # 措施建议：怎么改才合规
    REGULATION = "regulation"  # 法规知识：某法条的内容与适用范围
    REDLINE = "redline"        # 高风险红线：无依据增设许可 / 擅自加设处罚 / 超越权限
    GENERAL = "general"        # 通用咨询：非合规问题

    ALL = [REVIEW, MEASURE, REGULATION, REDLINE, GENERAL]


# ============================================================
# Severity Levels —— 统一定义于 src/knowledge_graph/schema.py（R1），此处转导入
# ============================================================

from src.knowledge_graph.schema import SeverityLevel  # noqa: E402

# 兼容旧的 .ALL 用法（本文件自测与历史引用）
SEVERITY_ALL = [lvl.value for lvl in SeverityLevel]


# ============================================================
# Agent State Definition
# ============================================================

class AgentState(TypedDict, total=False):
    """
    LangGraph Global State for Corporate Compliance Review System

    All agents read from and write to this shared state.
    List fields use Annotated[..., operator.add] for accumulation across agents.

    Fields:
        query: User's original question
        history: Prior conversation turns (injected by API by session_id)
        needs_clarification / clarification: 信息不足时的追问机制
        intent: Detected intent type (review/measure/regulation/redline/general)
        intent_confidence: 意图识别置信度
        doc_type: 文件类型（管理办法/实施细则/合同/公告/通知/行政规范性文件…）
        matters: 涉及事项（行政许可/行政处罚/招标投标/政府采购…）
        risk_signals: 高风险信号（如 "处5万元以上罚款"）
        severity: 风险严重程度 (low/medium/high/redline)
        is_redline: 是否命中高风险红线
        authorities: 归口部门 [{name, role, reason, basis}]
        measures: 合规措施 [{name, scenario, key_points}]
        regulation_answer: 法规知识回答（grounded）
        advice: 提交前自查清单
        disclaimers: 免责声明（累加，必须非空！）
        warnings: 高风险提示（累加）
        final_answer: 最终拼装的回答
        retrieved_entities: 图谱/向量检索命中实体
        retrieved_contexts: 检索上下文（供 RAGAS context_recall）
        linked_entities: 实体链接结果（用户词 → 图谱规范实体）
        messages: Debug messages for tracing agent execution
    """

    # ===== User Input =====
    query: str  # 用户原始问题

    # ===== Conversation Memory（由 API 按 session_id 注入，不参与累加）=====
    history: list  # 历史轮次 [{role, content, ...}]，供 Agent 读取上下文

    # ===== Clarification（信息不足时的追问机制）=====
    needs_clarification: bool  # 是否需要向用户追问
    clarification: str         # 追问话术

    # ===== Intent Classification =====
    intent: str  # 意图类型: review | measure | regulation | redline | general
    intent_confidence: float  # 意图识别置信度 (0-1)

    # ===== 要素识别（文件类型 / 事项 / 风险信号）=====
    doc_type: str                                   # 文件类型
    matters: Annotated[list[str], operator.add]     # 涉及事项（累加）
    risk_signals: Annotated[list[str], operator.add]  # 高风险信号（累加）
    severity: str                                   # low | medium | high | redline
    is_redline: bool                                # 是否命中高风险红线

    # ===== 归口定位 =====
    authorities: Annotated[list[dict], operator.add]  # 归口部门（累加）
    # 每条: {"name": str, "role": "主责"|"协管", "reason": str, "basis": str}

    # ===== 措施建议 =====
    measures: Annotated[list[dict], operator.add]  # 合规措施（累加）
    # 每条: {"name": str, "scenario": str, "key_points": list[str]}

    # ===== 法规知识 =====
    regulation_answer: str  # 法规知识回答（grounded）

    # ===== 提交前建议（自查清单）=====
    advice: Annotated[list[str], operator.add]  # 提交前自查清单（累加）

    # ===== Safety Check (Mandatory) =====
    disclaimers: Annotated[list[str], operator.add]  # 免责声明（累加，必须！）
    warnings: Annotated[list[str], operator.add]  # 高风险提示（累加）

    # ===== Final Output =====
    final_answer: str  # 最终回答

    # ===== Retrieved Entities =====
    retrieved_entities: Annotated[list[dict], operator.add]  # 检索到的事实实体（累加）
    # 每个实体: {"name": str, "type": str, "score": float, "source": str}
    retrieved_contexts: Annotated[list, operator.add]  # 检索上下文（供 RAGAS context_recall）

    # ===== Linked Entities (Entity Linking) =====
    linked_entities: Annotated[list[dict], operator.add]  # 实体链接结果（累加）
    # 每条链接: {"input_entity": str, "matched_entity": str, "type": str, "similarity": float}
    # 由 regulation_knowledge 的 Step0 显式实体链接产生，供 API 响应/前端展示/Text2Cypher 提示

    # ===== Debug Messages =====
    messages: Annotated[list[str], operator.add]  # 调试消息（累加）


# ============================================================
# Helper Functions
# ============================================================

def create_initial_state(query: str, history: list | None = None) -> AgentState:
    """
    Create initial state with user query (and optional conversation history)

    Args:
        query: User's question
        history: Prior conversation turns (from session memory)

    Returns:
        Initialized AgentState
    """
    return AgentState(
        query=query,
        history=history or [],
        needs_clarification=False,
        clarification="",
        intent="",
        intent_confidence=0.0,
        doc_type="",
        matters=[],
        risk_signals=[],
        severity="",
        is_redline=False,
        authorities=[],
        measures=[],
        regulation_answer="",
        advice=[],
        disclaimers=[],
        warnings=[],
        final_answer="",
        retrieved_entities=[],
        retrieved_contexts=[],
        linked_entities=[],
        messages=[f"[START] User query: {query}"]
    )


def add_message(state: AgentState, message: str) -> dict:
    """
    Create update dict to add a debug message

    Args:
        state: Current state
        message: Message to add

    Returns:
        Update dict for state
    """
    return {"messages": [message]}


# ============================================================
# Main: Self-test
# ============================================================

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("AGENT STATE TEST")
    print("=" * 60)

    # Test 1: Create initial state
    print("\n[TEST 1] Create initial state")
    state = create_initial_state("我们准备发一份《供应商管理办法》，有没有跟法规冲突的地方？")
    print(f"  query: {state['query']}")
    print(f"  intent: '{state['intent']}' (empty)")
    print(f"  matters: {state['matters']}")
    print(f"  disclaimers: {state['disclaimers']} (空 → 由 safety_checker 强制填充)")
    print(f"  messages: {state['messages']}")

    # Test 2: Simulate agent updates
    print("\n[TEST 2] Simulate agent updates")

    # Intent classifier update
    intent_update = {
        "intent": IntentType.REVIEW,
        "intent_confidence": 0.93,
        "messages": ["[IntentClassifier] Detected: review (0.93)"]
    }
    print(f"  Intent update: {intent_update['intent']}")

    # Matter detector update
    matter_update = {
        "doc_type": "管理办法",
        "matters": ["行政许可", "招标投标"],
        "risk_signals": ["处5万元以上罚款"],
        "severity": SeverityLevel.HIGH,
        "is_redline": False,
        "messages": ["[MatterDetector] doc_type=管理办法, matters=行政许可,招标投标"]
    }
    print(f"  Matter update: {matter_update['matters']} (doc_type={matter_update['doc_type']})")

    # Authority locator update
    auth_update = {
        "authorities": [{"name": "市场监督管理部门", "role": "主责", "reason": "负责市场准入与合同监管", "basis": "《行政许可法》"}],
        "messages": ["[AuthorityLocator] Recommended: 市场监督管理部门(主责)"]
    }
    print(f"  Authority update: {[a['name'] for a in auth_update['authorities']]}")

    # Safety checker update (mandatory!)
    safety_update = {
        "disclaimers": ["🔴 重要声明：本系统只提供法规依据检索与风险提示，不提供法律意见……"],
        "warnings": [],
        "messages": ["[SafetyChecker] Added disclaimer"]
    }
    print(f"  Safety update: disclaimer added")

    # Test 3: Verify list accumulation works
    print("\n[TEST 3] Verify list accumulation")
    print("  (In LangGraph, Annotated[list, operator.add] accumulates lists)")
    print("  Example: matters / disclaimers from multiple agents will be merged")

    # Test 4: Intent types
    print("\n[TEST 4] Intent types")
    for intent in IntentType.ALL:
        print(f"  - {intent}")

    # Test 5: Severity levels
    print("\n[TEST 5] Severity levels")
    for level in SEVERITY_ALL:
        print(f"  - {level}")

    assert IntentType.ALL == ["review", "measure", "regulation", "redline", "general"], "IntentType.ALL 定义漂移"
    assert set(SEVERITY_ALL) == {"low", "medium", "high", "redline"}, "SeverityLevel 定义漂移"

    print("\n" + "=" * 60)
    print("[SUCCESS] Agent state tests passed!")
    print("=" * 60)
