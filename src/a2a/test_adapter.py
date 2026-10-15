# src/a2a/test_adapter.py
# 适配层单测：用「假 graph」走**真实的默认执行路径**，验证 AgentState → A2A 产物的映射
#
# 与 smoke_test.py 的区别：
#   smoke_test 替换的是 invoke_agent 本身（测协议链路）；
#   本文件替换的是最底层的 _run_graph（测我自己写的适配映射逻辑），
#   所以 executor 里的默认实现 _default_invoke_agent / _default_resume_agent
#   是**真的被执行**的，只有「图的返回值」是伪造的。
#
# 为什么不能跑真图：真图需要 Neo4j + FAISS + Redis/MySQL + LLM Key。
# 本文件把这几样都换成假数据，从而能独立验证：
#   ① 产物按「实际意图」而不是「外部期望的 skill」命名
#   ② 免责声明 / 高风险红线提示被单独落成 safety 产物
#   ③ needs_clarification → INPUT_REQUIRED → 补齐后继续，且原问题进了对话历史
#
# 运行（在项目根目录）：
#   python -m src.a2a.test_adapter

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":                       # Windows 控制台默认 GBK，避免 emoji 打印报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx                                                        # noqa: E402
from fastapi import FastAPI                                         # noqa: E402

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory  # noqa: E402
from a2a.helpers import get_artifact_text, get_data_parts, new_text_message  # noqa: E402
from a2a.types import Role, SendMessageRequest, TaskState            # noqa: E402

import src.a2a.executor as executor_mod                             # noqa: E402
from src.a2a.agent_card import AGENT_NAME, SKILL_REGULATION         # noqa: E402
from src.a2a.app import install_a2a                                 # noqa: E402

TEST_BASE = "http://a2a.adapter.test"
RESULTS: list[tuple[str, bool, str]] = []

DISCLAIMER = (
    "🔴 重要声明：本系统只提供法规依据检索与风险提示，不提供法律意见，"
    "也不对文件是否合法作出结论。以上内容请由法务或执业律师复核后使用。"
)

# 记录每一次「假图」被调用时的入参，用于断言适配层传对了东西
CALLS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def _artifact_text(artifact) -> str:
    parts = [get_artifact_text(artifact)]
    try:
        import json
        parts.append(json.dumps(get_data_parts(list(artifact.parts)), ensure_ascii=False, default=str))
    except Exception:
        pass
    return " ".join(p for p in parts if p)


# ── 假 graph：返回与 src/agents/state.py::AgentState 同构的字典 ────
async def fake_run_graph(query: str, history: list | None = None) -> dict:
    CALLS.append({"query": query, "history": list(history or [])})

    # 场景 1：审查要素不足 → 真实系统里由 matter_detector 置 needs_clarification
    if "看看" in query:
        return {
            "query": query,
            "intent": "review",
            "matters": [],
            "needs_clarification": True,
            "clarification": "这份制度是给内部员工还是外部供应商用的？请补充适用对象与文件类型。",
            "authorities": [],
            "disclaimers": [DISCLAIMER],
            "final_answer": "❓ 这份制度是给内部员工还是外部供应商用的？",
        }

    # 场景 2：补齐信息后 → 正常合规排查结果
    if "供应商管理办法" in query:
        return {
            "query": query,
            "intent": "review",
            "intent_confidence": 0.96,
            "doc_type": "制度",
            "matters": ["供应商管理", "付款结算"],
            "risk_signals": ["付款条件显失公平"],
            "severity": "medium",
            "is_redline": False,
            "authorities": [{"name": "法务部", "role": "归口", "level": "一级"}],
            "measures": [],
            "linked_entities": [{"input_entity": "供应商管理办法", "matched_entity": "供应商管理", "type": "Matter", "similarity": 1.0}],
            "disclaimers": [DISCLAIMER],
            "warnings": [],
            "final_answer": "涉及事项：供应商管理、付款结算；归口部门：法务部。建议对照《民法典》合同编复核付款条款。",
        }

    # 场景 3：高风险红线 → 安全门产出强提示
    if "审批前置" in query:
        return {
            "query": query,
            "intent": "redline",
            "is_redline": True,
            "severity": "redline",
            "risk_signals": ["变相设置审批前置", "无依据增设行政许可"],
            "matters": ["审批流程设置"],
            "disclaimers": [DISCLAIMER],
            "warnings": ["🔴 高风险红线：不得无依据增设行政许可或变相设置审批前置，请立即停止发布并报法务复核。"],
            "final_answer": "🔴 该表述触及高风险红线，请立即停止发布。",
        }

    # 场景 4：措施建议（注意：即使外部指定 regulation，图也可能判成别的）
    return {
        "query": query,
        "intent": "measure",
        "intent_confidence": 0.88,
        "doc_type": "制度",
        "matters": ["审批流程设置"],
        "measures": [{"name": "删除无依据的审批前置", "scenario": "行政许可类", "key_points": ["改由事后备案替代"]}],
        "authorities": [{"name": "法务部"}],
        "linked_entities": [],
        "disclaimers": [DISCLAIMER],
        "warnings": [],
        "final_answer": "建议删除无依据的审批前置，改由事后备案替代；具体整改方案请法务复核后确定。",
    }


def fake_load_history(session_id: str) -> list:
    CALLS.append({"history_for": session_id})
    return []


async def collect(client, text: str, *, task_id: str | None = None, metadata: dict | None = None):
    message = new_text_message(text, task_id=task_id, role=Role.ROLE_USER)
    if metadata:
        message.metadata.update({k: v for k, v in metadata.items() if v is not None})

    seen_task_id = task_id
    final_state = None
    artifacts: list[tuple[str, str]] = []

    async for event in client.send_message(SendMessageRequest(message=message)):
        kind = event.WhichOneof("payload")
        if kind == "task":
            seen_task_id = event.task.id
            final_state = event.task.status.state
            for artifact in event.task.artifacts:
                artifacts.append((artifact.name, _artifact_text(artifact)))
        elif kind == "status_update":
            seen_task_id = event.status_update.task_id or seen_task_id
            final_state = event.status_update.status.state
        elif kind == "artifact_update":
            artifact = event.artifact_update.artifact
            artifacts.append((artifact.name, _artifact_text(artifact)))

    return seen_task_id, final_state, artifacts


def _state_name(state) -> str:
    return TaskState.Name(state) if state is not None else "state=None"


async def main() -> int:
    # ★ 只替换最底层的「图」与「历史存储」，executor 的默认实现原样执行
    executor_mod._run_graph = fake_run_graph
    executor_mod._load_history = fake_load_history

    app = FastAPI(title="A2A adapter test")
    # 不传 executor → 用默认的 ComplianceReviewExecutor（走真实默认实现）
    install_a2a(app, public_base_url=TEST_BASE)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=TEST_BASE,
                                 timeout=60.0, trust_env=False) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=TEST_BASE).get_agent_card()
        check("Agent Card 名称正确", card.name == AGENT_NAME, card.name)
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)

        try:
            print("\n① 正常审查：产物应按「图判定的意图」命名")
            CALLS.clear()
            task_id, state, artifacts = await collect(
                client, "不合规的表述应该怎么改？",
                metadata={"skill": SKILL_REGULATION, "session_id": "adp-s1", "user_id": "legal-9"},
            )
            names = [a[0] for a in artifacts]
            check("走到 COMPLETED", state == TaskState.TASK_STATE_COMPLETED, _state_name(state))
            check("按实际意图(measure)命名，而非外部指定的 regulation",
                  "measure_answer" in names, str(names))
            check("结构化产物含归口/措施字段",
                  "structured" in names and any("审批前置" in t for n, t in artifacts if n == "structured"),
                  str(names))
            check("安全产物含免责声明",
                  "safety" in names and any("重要声明" in t for n, t in artifacts if n == "safety"),
                  str(names))
            check("执行时取到了业务 session_id",
                  any(c.get("history_for") == "adp-s1" for c in CALLS), str(CALLS))

            print("\n② 高风险红线：安全产物应带预警与 is_redline")
            _, state, artifacts = await collect(client, "我们想在制度里增设一道审批前置")
            names = [a[0] for a in artifacts]
            check("走到 COMPLETED", state == TaskState.TASK_STATE_COMPLETED, _state_name(state))
            check("产物名为 redline_answer", "redline_answer" in names, str(names))
            check("安全产物含红线预警",
                  any("红线" in t for n, t in artifacts if n == "safety"), str(names))
            check("安全产物标记 is_redline=true",
                  any("is_redline" in t and "true" in t.lower()
                      for n, t in artifacts if n == "safety"), str(names))

            print("\n③ 信息不足：应挂起为 INPUT_REQUIRED 并给出追问产物")
            CALLS.clear()
            clarify_id, state, artifacts = await collect(
                client, "帮我看看这份文件", metadata={"session_id": "adp-s2"},
            )
            names = [a[0] for a in artifacts]
            check("停在 INPUT_REQUIRED", state == TaskState.TASK_STATE_INPUT_REQUIRED, _state_name(state))
            check("产物名为 clarification", "clarification" in names, str(names))
            check("追问上下文带原问题",
                  any("帮我看看这份文件" in t for n, t in artifacts if n == "clarification_context"),
                  str(names))

            print("\n④ 补齐信息：原问题应进对话历史，补充内容作为新 query 重跑")
            CALLS.clear()
            same_id, state, artifacts = await collect(
                client, "这是一份给外部供应商用的《供应商管理办法》，重点想排查付款与廉洁条款",
                task_id=clarify_id, metadata={"session_id": "adp-s2"},
            )
            names = [a[0] for a in artifacts]
            check("走到 COMPLETED", state == TaskState.TASK_STATE_COMPLETED, _state_name(state))
            check("taskId 未变", same_id == clarify_id, f"{same_id} vs {clarify_id}")
            check("产物名为 review_answer", "review_answer" in names, str(names))
            rerun = [c for c in CALLS if "query" in c]
            check("补充内容作为新 query",
                  bool(rerun) and rerun[-1]["query"].startswith("这是一份给外部供应商用的"),
                  str(rerun[-1] if rerun else None)[:120])
            check("原问题被放进对话历史",
                  bool(rerun) and any("看看" in str(h) for h in rerun[-1]["history"]),
                  str(rerun[-1]["history"] if rerun else None)[:160])

        finally:
            await client.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 58}\n结果：{passed}/{total} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ❌ {name}  {detail}")
    print("=" * 58)
    if passed == total:
        print("[SUCCESS] a2a adapter test passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
