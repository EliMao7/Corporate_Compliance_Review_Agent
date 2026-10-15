"""
LangGraph State Graph Orchestration
Orchestrates 9 agents into a complete corporate-compliance review system.

⚠️ 本图**没有并行分支**：条件边是"按意图分流，单链路执行"——一次请求只走一条主链路。
   不要把"统一汇入安全检查"理解成并行汇聚。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langgraph.graph import StateGraph, START, END

from src.common.logger import setup_logger
from src.agents.state import AgentState, IntentType, create_initial_state

# Import all agents（9 个节点，与 add_node 一一对应）
from src.agents.intent_classifier import intent_classifier
from src.agents.matter_detector import matter_detector
from src.agents.authority_locator import authority_locator
from src.agents.measure_advisor import measure_advisor
from src.agents.regulation_knowledge import regulation_knowledge
from src.agents.pre_submit_advisor import pre_submit_advisor
from src.agents.safety_checker import safety_checker
from src.agents.answer_fusion import answer_fusion
from src.agents.general_chat import general_chat

logger = setup_logger(__name__, "agents.log")


# ============================================================
# Routing Functions（两级条件路由）
# ============================================================

def route_by_intent(state: AgentState) -> str:
    """一级路由：按意图分流——review/measure 需先识别要素，其余各走其道。"""
    intent = state.get("intent", IntentType.GENERAL)

    if intent in (IntentType.REVIEW, IntentType.MEASURE):
        return "matter_detector"          # 需要先识别要素
    elif intent == IntentType.REGULATION:
        return "regulation_knowledge"     # 直接查法规
    elif intent == IntentType.REDLINE:
        return "safety_checker"           # 高风险红线直达安全检查
    else:
        return "general_chat"


def route_after_matter(state: AgentState) -> str:
    """二级路由：要素识别后再分流——review 定位归口，measure 给措施（无事项则退回查法规）。"""
    intent = state.get("intent")

    if intent == IntentType.REVIEW:
        return "authority_locator"        # 排查：定位归口
    elif intent == IntentType.MEASURE:
        # 无事项锚点时措施建议无从下手；交给能用工具的 ReAct 知识 Agent，一轮答完
        return "measure_advisor" if state.get("matters") else "regulation_knowledge"
    else:
        return "safety_checker"


# ============================================================
# Build State Graph
# ============================================================

def build_graph() -> StateGraph:
    """Build the LangGraph state graph"""

    # Create graph with AgentState
    graph = StateGraph(AgentState)

    # Add all 9 agent nodes
    graph.add_node("intent_classifier", intent_classifier)
    graph.add_node("matter_detector", matter_detector)
    graph.add_node("authority_locator", authority_locator)
    graph.add_node("measure_advisor", measure_advisor)
    graph.add_node("regulation_knowledge", regulation_knowledge)
    graph.add_node("pre_submit_advisor", pre_submit_advisor)
    graph.add_node("safety_checker", safety_checker)
    graph.add_node("answer_fusion", answer_fusion)
    graph.add_node("general_chat", general_chat)

    # Define edges
    # START -> intent_classifier
    graph.add_edge(START, "intent_classifier")

    # intent_classifier -> conditional routing（按意图分流，单链路）
    graph.add_conditional_edges(
        "intent_classifier",
        route_by_intent,
        {
            "matter_detector": "matter_detector",
            "regulation_knowledge": "regulation_knowledge",
            "safety_checker": "safety_checker",
            "general_chat": "general_chat"
        }
    )

    # matter_detector -> conditional routing（review vs measure；measure 无事项退回查法规）
    graph.add_conditional_edges(
        "matter_detector",
        route_after_matter,
        {
            "authority_locator": "authority_locator",
            "measure_advisor": "measure_advisor",
            "regulation_knowledge": "regulation_knowledge",   # measure 无锚点时的二级路由
            "safety_checker": "safety_checker"
        }
    )

    # review flow: authority_locator -> pre_submit_advisor -> safety_checker
    graph.add_edge("authority_locator", "pre_submit_advisor")
    graph.add_edge("pre_submit_advisor", "safety_checker")

    # measure flow: measure_advisor -> safety_checker
    graph.add_edge("measure_advisor", "safety_checker")

    # regulation flow: regulation_knowledge -> safety_checker
    graph.add_edge("regulation_knowledge", "safety_checker")

    # redline flow: safety_checker (direct)

    # general flow: general_chat -> END (不经安全检查，有意取舍)
    graph.add_edge("general_chat", END)

    # safety_checker -> answer_fusion -> END
    graph.add_edge("safety_checker", "answer_fusion")
    graph.add_edge("answer_fusion", END)

    return graph


# ============================================================
# Compiled Graph
# ============================================================

# Build and compile the graph
graph = build_graph()
app = graph.compile()


# ============================================================
# Run Function
# ============================================================

def run(query: str, history: list | None = None) -> AgentState:
    """
    Run the corporate-compliance review system

    Args:
        query: User's question
        history: Prior conversation turns (from session memory) for multi-turn context

    Returns:
        Final AgentState with all results
    """
    logger.info(f"[Graph] Starting review: {query[:50]}... (history turns: {len(history or [])})")

    # Create initial state (with conversation history for multi-turn memory)
    initial_state = create_initial_state(query, history=history)

    # Run the graph
    result = app.invoke(initial_state)

    logger.info("[Graph] Review completed")
    return result


# ============================================================
# Streaming Run (for SSE endpoint)
# ============================================================

# 节点名 → 中文标签（前端进度展示用）
NODE_LABELS_ZH = {
    "intent_classifier": "意图识别",
    "matter_detector": "要素识别",
    "authority_locator": "归口定位",
    "measure_advisor": "措施建议",
    "regulation_knowledge": "法规知识检索",
    "pre_submit_advisor": "提交前建议",
    "safety_checker": "安全检查",
    "answer_fusion": "答案生成",
    "general_chat": "通用对话",
}


def run_stream(query: str, history: list | None = None):
    """Streaming 运行 Agent 系统，供 SSE 端点消费。

    使用 LangGraph 双模式 stream：
    - "updates"：每个节点完成后给出 {node_name: delta} → yield ("progress", node_name, delta)
    - "values" ：每步之后给出完整 state 快照 → yield ("values", snapshot)，最后一个即终态

    注意：这是**同步生成器**（app.stream 为同步）。SSE 端点用
    asyncio.to_thread(lambda: next(gen, sentinel)) 逐个取值——哨兵模式保证
    StopIteration 不穿越线程边界进入 async 生成器（PEP 479 会将其转为 RuntimeError）。
    """
    logger.info(f"[Graph] Starting streaming review: {query[:50]}...")
    initial_state = create_initial_state(query, history=history)
    for mode, chunk in app.stream(initial_state, stream_mode=["updates", "values"]):
        if mode == "updates":
            # chunk = {node_name: state_delta}；过滤 __start__（初始状态写入，非 Agent 节点）
            for node_name, delta in chunk.items():
                if node_name == "__start__":
                    continue
                yield ("progress", node_name, delta if isinstance(delta, dict) else {})
        else:  # mode == "values"
            yield ("values", chunk)


# ============================================================
# Visualization
# ============================================================

def get_mermaid_diagram() -> str:
    """Generate Mermaid diagram of the agent flow.

    静态示意图，须与 build_graph() 的边保持一致（已对齐 route_by_intent / route_after_matter，
    含 measure·无事项 → 法规知识 的二级路由）。
    """
    return """
```mermaid
graph TD
    START --> A[意图识别<br/>IntentClassifier]

    A -->|review| B[要素识别<br/>MatterDetector]
    A -->|measure| B
    A -->|regulation| E[法规知识<br/>RegulationKnowledge]
    A -->|redline| G[安全检查<br/>SafetyChecker]
    A -->|general| I[通用咨询<br/>GeneralChat]

    B -->|review| C[归口定位<br/>AuthorityLocator]
    B -->|measure 有事项| D[措施建议<br/>MeasureAdvisor]
    B -->|measure 无事项| E

    C --> F[提交前建议<br/>PreSubmitAdvisor]
    F --> G

    D --> G
    E --> G

    G --> H[答案融合<br/>AnswerFusion]
    H --> END

    I --> END

    style A fill:#e1f5ff
    style G fill:#ffebee
    style H fill:#e8f5e9
```
"""


# ============================================================
# Main: Test
# ============================================================

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("LANGGRAPH AGENT SYSTEM TEST")
    print("=" * 60)

    # Print Mermaid diagram
    print("\n[Agent Flow Diagram]")
    print(get_mermaid_diagram())

    # --- 离线构建校验（不依赖 LLM 密钥）---
    print("\n[Graph build check]")
    expected_nodes = [
        "intent_classifier", "matter_detector", "authority_locator", "measure_advisor",
        "regulation_knowledge", "pre_submit_advisor", "safety_checker", "answer_fusion",
        "general_chat",
    ]
    node_names = set(app.get_graph().nodes.keys())
    for n in expected_nodes:
        assert n in node_names, f"缺少节点: {n}"
    print(f"  9 agent nodes present: OK ({len(expected_nodes)})")
    print(f"  compiled graph nodes: {sorted(node_names)}")

    # 路由函数断言（与 TDD §2.3.7 一致，无并行分支）
    assert route_by_intent({"intent": IntentType.REVIEW}) == "matter_detector"
    assert route_by_intent({"intent": IntentType.MEASURE}) == "matter_detector"
    assert route_by_intent({"intent": IntentType.REGULATION}) == "regulation_knowledge"
    assert route_by_intent({"intent": IntentType.REDLINE}) == "safety_checker"
    assert route_by_intent({"intent": IntentType.GENERAL}) == "general_chat"
    assert route_after_matter({"intent": IntentType.REVIEW}) == "authority_locator"
    assert route_after_matter({"intent": IntentType.MEASURE, "matters": ["招标投标"]}) == "measure_advisor"
    assert route_after_matter({"intent": IntentType.MEASURE, "matters": []}) == "regulation_knowledge"
    print("  routing functions (route_by_intent / route_after_matter): OK")

    # --- 离线跑一条查询（无密钥时各节点内部兜底，链路仍应返回 final_answer）---
    test_query = "我们准备发一份《供应商管理办法》，帮我看下有没有跟法规冲突的地方"
    print(f"\n[Test Query] {test_query}")
    print("-" * 60)

    result = {}
    try:
        result = run(test_query)
    except Exception as e:
        print(f"  [note] 在线查询未完成（可能需要配置 LLM 密钥）: {e}")

    if result:
        print("\n[Results]")
        print(f"  Intent: {result.get('intent', '')}")
        print(f"  Confidence: {result.get('intent_confidence', 0):.2f}")
        print(f"  doc_type: {result.get('doc_type', '')}")
        print(f"  matters: {result.get('matters', [])}")
        print(f"  risk_signals: {result.get('risk_signals', [])}")
        print(f"  authorities: {[a.get('name') if isinstance(a, dict) else a for a in result.get('authorities', [])]}")
        print(f"  disclaimers: {len(result.get('disclaimers', []))} items")
        print(f"  final_answer: {len(result.get('final_answer', ''))} chars")

        print("\n[Agent Execution Chain]")
        for msg in result.get("messages", []):
            print(f"  {msg}")

        print("\n[Final Answer]")
        print("-" * 60)
        print(result.get("final_answer", ""))
        print("-" * 60)

    print("\n" + "=" * 60)
    print("[SUCCESS] Graph built and compiled successfully!")
    print("=" * 60)
