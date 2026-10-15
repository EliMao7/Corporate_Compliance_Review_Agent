"""
Answer Fusion Agent（答案融合）
**确定性拼装，不调 LLM**：把各节点产物拼成一段结构化 Markdown。

版式约定：
- 高风险提示**置顶**（红线不淹没在普通内容里）；
- 应比对法规（含效力层级）/ 归口部门 / 措施建议 / 提交前自查清单分区呈现；
- 每条结论挂法规名称 + 条款号（来自 regulation_answer / 措施说明，本节点不自造依据）；
- 免责声明固定在回答末尾；
- 最后对拼装结果做一次结论性表述后置拦截（复用 safety_checker 的纯规则函数）。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.common.logger import setup_logger
from src.agents.state import AgentState
from src.agents.safety_checker import sanitize_conclusions

logger = setup_logger(__name__, "agents.log")


def _render_authorities(authorities: list) -> list[str]:
    lines = ["**🏛️ 归口部门**"]
    for a in authorities:
        if isinstance(a, dict):
            name = a.get("name", "")
            role = a.get("role", "")
            reason = a.get("reason", "")
            basis = a.get("basis", "")
            seg = f"- {name}（{role}）" if role else f"- {name}"
            if reason:
                seg += f"：{reason}"
            if basis:
                seg += f"（依据：{basis}）"
            lines.append(seg)
        else:
            lines.append(f"- {a}")
    return lines


def _render_measures(measures: list) -> list[str]:
    lines = ["**🛠️ 合规措施**"]
    for m in measures:
        if isinstance(m, dict):
            lines.append(f"- **{m.get('name', '')}**")
            if m.get("scenario"):
                lines.append(f"  - 适用场景：{m['scenario']}")
            for kp in (m.get("key_points") or []):
                lines.append(f"  - {kp}")
        else:
            lines.append(f"- {m}")
    return lines


class AnswerFusionAgent:
    """Answer Fusion Agent（确定性，不调 LLM）"""

    def __call__(self, state: AgentState) -> dict:
        """Process state and return update"""
        logger.info("[AnswerFusion] Fusing answers (deterministic, no LLM)...")

        intent = state.get("intent", "")
        doc_type = state.get("doc_type", "")
        matters = state.get("matters", [])
        risk_signals = state.get("risk_signals", [])
        authorities = state.get("authorities", [])
        measures = state.get("measures", [])
        regulation_answer = state.get("regulation_answer", "")
        advice = state.get("advice", [])
        disclaimers = state.get("disclaimers", [])
        warnings = state.get("warnings", [])
        needs_clarify = state.get("needs_clarification", False)
        clarification = state.get("clarification", "")

        parts: list[str] = []

        # 1) 高风险置顶（红线 / 较高风险提示放在最前）
        if warnings:
            for w in warnings:
                parts.append(w)
            parts.append("")

        # 2) 追问（信息不足）：追问话术必须进入正文
        if needs_clarify and clarification:
            parts.append("❓ " + clarification)
            parts.append("")

        # 3) 识别摘要（文件类型 / 事项 / 风险信号）
        summary = []
        if doc_type:
            summary.append(f"文件类型：{doc_type}")
        if matters:
            summary.append(f"涉及事项：{'、'.join(matters)}")
        if risk_signals:
            summary.append(f"高风险信号：{'、'.join(risk_signals)}")
        if summary:
            parts.append("**📄 识别结果**")
            parts.extend(f"- {s}" for s in summary)
            parts.append("")

        # 4) 归口部门
        if authorities:
            parts.extend(_render_authorities(authorities))
            parts.append("")

        # 5) 应比对法规 / 依据（含效力层级）——每条结论挂法规名称+条款号（来自 grounded 文本）
        if regulation_answer:
            parts.append("**📚 应比对法规与依据（含效力层级）**")
            parts.append(regulation_answer)
            parts.append("")

        # 6) 合规措施
        if measures:
            parts.extend(_render_measures(measures))
            parts.append("")

        # 7) 提交前自查清单
        if advice:
            parts.append("**✅ 提交前自查清单**")
            parts.extend(f"- {a}" for a in advice)
            parts.append("")

        # 8) 兜底：review/measure 分支若什么都没产出，给一句引导
        if not parts:
            parts.append("暂未从您的描述中识别到足够信息。请补充文件类型、涉及事项或具体问题，我再帮您梳理应比对的法规与归口部门。")
            parts.append("")

        # 9) 免责声明固定在末尾（必须非空，由 safety_checker 保证）
        if disclaimers:
            parts.append("---")
            parts.append("**免责声明**")
            parts.extend(disclaimers)

        final_answer = sanitize_conclusions("\n".join(parts).strip())

        logger.info(f"[AnswerFusion] Final answer: {len(final_answer)} chars")
        return {
            "final_answer": final_answer,
            "messages": [f"[AnswerFusion] Generated final answer: {len(final_answer)} chars"]
        }


# Singleton instance
answer_fusion = AnswerFusionAgent()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("ANSWER FUSION AGENT TEST (deterministic)")
    print("=" * 60)

    state = {
        "intent": "review",
        "doc_type": "管理办法",
        "matters": ["招标投标", "政府采购"],
        "risk_signals": ["必须经我方审批"],
        "authorities": [{"name": "发展和改革部门", "role": "主责", "reason": "招标投标监管", "basis": "《招标投标法》"}],
        "advice": ["逐条比对上位法", "确认未误设行政处罚"],
        "warnings": ["🚨 高风险红线提示：……"],
        "disclaimers": ["🔴 重要声明：本系统只提供法规依据检索与风险提示……"],
        "regulation_answer": "",
    }
    result = answer_fusion(state)
    print(result["final_answer"])
    assert result["final_answer"], "final_answer 不得为空"
    assert result["final_answer"].strip().startswith("🚨"), "高风险提示应置顶"
    assert "免责声明" in result["final_answer"], "免责声明应在末尾"

    # 结论性表述后置拦截
    state2 = dict(state, regulation_answer="该文件是合法的，可以不违反法律地直接发布。")
    out2 = answer_fusion(state2)["final_answer"]
    assert "该文件是合法的" not in out2, "结论性表述必须被拦截"

    print("\n" + "=" * 60)
    print("[SUCCESS] Answer fusion tests passed!")
    print("=" * 60)
