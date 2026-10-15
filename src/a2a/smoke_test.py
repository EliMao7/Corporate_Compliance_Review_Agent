# src/a2a/smoke_test.py
# A2A 链路冒烟测试（不需要 Neo4j / FAISS / Redis / MySQL / LLM Key）
#
# 为什么能脱离基础设施跑：
#   A2A 适配层（executor）把「路由」「执行」「继续」做成了可注入的接缝，
#   本测试注入桩实现，于是可以单独验证协议层是否正确 ——
#   发现 → 委托 → 状态流转 → 产物回传 → 信息不足挂起 → 补齐后继续。
#
# 运行（在项目根目录）：
#   python -m src.a2a.smoke_test

from __future__ import annotations

import asyncio
import json
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
from a2a.helpers import (                                          # noqa: E402
    get_artifact_text,
    get_data_parts,
    get_stream_response_text,
    new_text_message,
)
from a2a.types import Role, SendMessageRequest, TaskState            # noqa: E402

from src.a2a.agent_card import (                                    # noqa: E402
    AGENT_NAME,
    SKILL_GENERAL,
    SKILL_MEASURE,
    SKILL_REDLINE,
    SKILL_REGULATION,
    SKILL_REVIEW,
)
from src.a2a.app import install_a2a                                 # noqa: E402
from src.a2a.executor import ComplianceReviewExecutor               # noqa: E402

TEST_BASE = "http://a2a.test"          # 走 ASGITransport，域名不参与真实网络
RESULTS: list[tuple[str, bool, str]] = []

DISCLAIMER = (
    "🔴 重要声明：本系统只提供法规依据检索与风险提示，不提供法律意见，"
    "也不对文件是否合法作出结论。以上内容请由法务或执业律师复核后使用。"
)

CLARIFY_TEXT = (
    "为了准确定位适用的法规与归口部门，还需要您补充一些信息：\n"
    "① 文件适用的对象（内部员工 / 外部供应商）；② 文件类型（制度 / 合同 / 通知）；"
    "③ 想重点排查的方面。"
)


def _artifact_text(artifact) -> str:
    """产物内容：文本 part + 结构化 data part 都取出来（data part 用 get_artifact_text 取不到）。"""
    parts = [get_artifact_text(artifact)]
    try:
        parts.append(json.dumps(get_data_parts(list(artifact.parts)), ensure_ascii=False, default=str))
    except Exception:
        pass
    return " ".join(p for p in parts if p)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


# ── 桩：替代真实业务层，让协议链路可以单独验证 ────────────────────
async def stub_router(text: str, requested_skill: str | None = None) -> str:
    """桩路由：只认外部显式指定的 skill，其余交回系统（返回空串表示自判）。"""
    return requested_skill or ""


async def stub_invoke(skill, text, *, metadata, session_id, user_id):
    # 模拟「审查要素不足 → 触发追问」这条真实分支
    if "看看" in text or metadata.get("force_clarify"):
        return {
            "status": "input_required",
            "skill": SKILL_REVIEW,
            "content": CLARIFY_TEXT,
            "display": {
                "clarification": CLARIFY_TEXT,
                "original_query": text,
                "intent": SKILL_REVIEW,
                "matters": [],
            },
        }
    return {
        "status": "completed",
        "skill": skill or SKILL_REGULATION,
        "content": f"[stub] 已处理「{text}」（session={session_id} user={user_id}）",
        "structured": {
            "intent": skill or SKILL_REGULATION,
            "doc_type": "制度",
            "matters": ["供应商管理"],
            "risk_signals": [],
            "authorities": [{"name": "法务部"}],
            "linked_entities": [{"input_entity": "供应商管理办法", "matched_entity": "供应商管理", "similarity": 0.97}],
        },
        "safety": {
            "disclaimers": [DISCLAIMER],
            "warnings": [],
            "is_redline": False,
        },
    }


async def stub_resume(resume_token, decision):
    return {
        "status": "completed",
        "skill": SKILL_REVIEW,
        "content": f"[stub] 已结合补充信息给出审查意见：归口部门为法务部（补充：{decision['answer']}）",
        "structured": {"intent": SKILL_REVIEW, "authorities": [{"name": "法务部"}]},
        "safety": {
            "disclaimers": [DISCLAIMER],
            "warnings": [],
            "is_redline": False,
        },
    }


def build_test_app() -> FastAPI:
    app = FastAPI(title="A2A smoke test")
    install_a2a(
        app,
        executor=ComplianceReviewExecutor(
            router=stub_router, invoke_agent=stub_invoke, resume_agent=stub_resume
        ),
        public_base_url=TEST_BASE,
    )
    return app


async def collect(client, text: str, *, task_id: str | None = None, metadata: dict | None = None):
    """发一条消息，收集全部事件，返回 (taskId, 末态, 纯文本, 产物列表)。"""
    # 注意：new_text_message 的 role 默认是 ROLE_AGENT(2)，客户端发出的必须是 ROLE_USER(1)
    message = new_text_message(text, task_id=task_id, role=Role.ROLE_USER)
    if metadata:
        message.metadata.update({k: v for k, v in metadata.items() if v is not None})

    seen_task_id = task_id
    final_state = None
    texts: list[str] = []
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
            msg = event.status_update.status.message
            if msg is not None and msg.parts:
                texts.append(get_stream_response_text(event))

        elif kind == "artifact_update":
            artifact = event.artifact_update.artifact
            artifacts.append((artifact.name, _artifact_text(artifact)))

        elif kind == "message":
            texts.append(get_stream_response_text(event))

    return seen_task_id, final_state, "\n".join(t for t in texts if t), artifacts


def _state_name(state) -> str:
    return TaskState.Name(state) if state is not None else "state=None"


async def main() -> int:
    app = build_test_app()
    expected_skills = {
        SKILL_REVIEW, SKILL_MEASURE, SKILL_REGULATION, SKILL_REDLINE, SKILL_GENERAL,
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=TEST_BASE,
                                 timeout=60.0, trust_env=False) as http:
        # ── ① 发现阶段 ────────────────────────────────────────
        print("\n① Agent Card 发现")
        resolver = A2ACardResolver(httpx_client=http, base_url=TEST_BASE)
        card = await resolver.get_agent_card()
        check("拉到 Agent Card", card.name == AGENT_NAME, f"name={card.name} version={card.version}")
        check("声明了 5 个 skill", len(card.skills) == 5, ",".join(s.id for s in card.skills))
        check("skill 与 5 条意图链路一一对应",
              {s.id for s in card.skills} == expected_skills,
              str(sorted(s.id for s in card.skills)))
        check("接口地址正确", card.supported_interfaces[0].url == f"{TEST_BASE}/a2a",
              card.supported_interfaces[0].url)
        check("声明支持流式", bool(card.capabilities.streaming))

        # well-known 路径也要能直接访问（外部系统不一定用 SDK）
        raw = await http.get("/.well-known/agent-card.json")
        check("well-known 路径可访问", raw.status_code == 200 and raw.json()["name"] == AGENT_NAME,
              f"HTTP {raw.status_code}")

        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)

        try:
            # ── ② 委托一个正常任务 ────────────────────────────
            print("\n② 委托任务：法规知识问答（外部显式指定 skill）")
            task_id, state, text, artifacts = await collect(
                client, "《劳动合同法》对试用期是怎么规定的？",
                metadata={"skill": SKILL_REGULATION, "user_id": "legal-001"},
            )
            names = [a[0] for a in artifacts]
            check("任务走完到 COMPLETED", state == TaskState.TASK_STATE_COMPLETED, _state_name(state))
            check("拿到正文产物", any(n.endswith("_answer") for n in names), str(names))
            check("正文内容正确", any("stub" in a[1] for a in artifacts))
            check("拿到结构化产物", "structured" in names, str(names))
            check("拿到安全产物（免责声明）", "safety" in names and any(
                "重要声明" in t for n, t in artifacts if n == "safety"), str(names))

            # ── ③ 委托一个信息不足的任务 → 预期挂起等补充 ──────
            print("\n③ 委托任务：审查要素不足（预期停在 INPUT_REQUIRED）")
            clarify_task_id, state, text, artifacts = await collect(
                client, "帮我看看这份文件",
                metadata={"skill": SKILL_REVIEW, "user_id": "legal-001"},
            )
            names = [a[0] for a in artifacts]
            check("任务停在 INPUT_REQUIRED", state == TaskState.TASK_STATE_INPUT_REQUIRED,
                  _state_name(state))
            check("回传了追问话术", "clarification" in names, str(names))
            check("追问话术内容正确", any("补充" in t for n, t in artifacts if n == "clarification"))
            check("回传了追问上下文", "clarification_context" in names, str(names))

            # ── ④ 补齐信息 → 同一任务继续 ─────────────────────
            print("\n④ 补齐信息后继续同一个任务")
            if clarify_task_id:
                same_id, state, text, artifacts = await collect(
                    client, "这是一份给外部供应商用的《供应商管理办法》，重点想排查付款与廉洁条款",
                    task_id=clarify_task_id,
                    metadata={"user_id": "legal-001"},
                )
                check("补齐后走到 COMPLETED", state == TaskState.TASK_STATE_COMPLETED, _state_name(state))
                check("任务是同一个（taskId 未变）", same_id == clarify_task_id,
                      f"{same_id} vs {clarify_task_id}")
                check("产物含补充信息", any("补充" in a[1] for a in artifacts), str(artifacts)[:200])
            else:
                check("拿到继续执行所需的 taskId", False, "clarify_task_id 为空")

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
        print("[SUCCESS] a2a smoke test passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
