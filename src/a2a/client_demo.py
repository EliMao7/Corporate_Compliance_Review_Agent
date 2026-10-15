# src/a2a/client_demo.py
# A2A 客户端示例：站在「外部系统」的视角（法务系统 / OA / 合同平台），演示完整的四步协作流程
#
#   ① 发现   GET /.well-known/agent-card.json   → 拿到能力清单与端点地址
#   ② 委托   POST /a2a  message/send            → 把审查任务发给远端 Agent
#   ③ 等待   接收任务状态流转（submitted → working → input-required / completed）
#   ④ 补齐   带补充信息再发一条消息，让被挂起的任务继续跑完
#
# 运行（先起服务）：
#   python -m src.a2a.app                        # 桩业务层，起在 127.0.0.1:9100
#   python -m src.a2a.client_demo                # 默认连 127.0.0.1:9100
#   python -m src.a2a.client_demo http://其他地址:端口

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import (
    get_artifact_text,
    get_data_parts,
    get_message_text,
    get_stream_response_text,
    new_text_message,
)
from a2a.types import Role, SendMessageRequest, TaskState

from src.a2a.agent_card import SKILL_REGULATION, SKILL_REVIEW

DEFAULT_BASE_URL = "http://127.0.0.1:9100"


def _is_loopback(base_url: str) -> bool:
    host = httpx.URL(base_url).host or ""
    return host in {"127.0.0.1", "localhost", "::1"}


def _make_http_client(base_url: str) -> httpx.AsyncClient:
    """
    本机联调时关掉「环境变量代理」。

    httpx 默认 trust_env=True，会读取 HTTP_PROXY / HTTPS_PROXY。
    一旦机器上存在代理（公司网关、抓包工具、容器网络），发往 127.0.0.1 的 POST
    会被代理改写请求行，服务端收到的是一个被 URL 编码的绝对地址，
    表现为「Agent Card 能取到、POST /a2a 却 404」这种很迷惑的现象。
    联调 localhost 时直接绕过代理最稳；连真实远端时仍尊重环境代理。
    """
    return httpx.AsyncClient(timeout=120.0, trust_env=not _is_loopback(base_url))


def _artifact_content(artifact) -> str:
    """
    取产物内容：文本 part 走 get_artifact_text，
    结构化 data part 要用 get_data_parts(artifact.parts) —— 这两个 API 不通用，容易踩。
    """
    chunks = [get_artifact_text(artifact)]
    try:
        data = get_data_parts(list(artifact.parts))
        if data:
            chunks.append(json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        pass
    return " ".join(c for c in chunks if c)


def _state_name(state: int) -> str:
    """把 protobuf 的 TaskState 枚举值打回可读名字。"""
    try:
        return TaskState.Name(state).replace("TASK_STATE_", "")
    except Exception:
        return str(state)


def _summarize(event) -> str:
    """把一条 StreamResponse 打成人能看懂的一行。"""
    kind = event.WhichOneof("payload")

    if kind == "task":                                  # 一次性返回的完整任务
        task = event.task
        return f"[task]   id={task.id} state={_state_name(task.status.state)} artifacts={len(task.artifacts)}"

    if kind == "status_update":                         # 状态流转事件（流式模式下的主要事件）
        update = event.status_update
        message = update.status.message
        text = ""
        if message is not None and message.parts:
            text = get_message_text(message)
        return f"[status] {_state_name(update.status.state)}  {text}".rstrip()

    if kind == "artifact_update":                       # 产物增量事件
        artifact = event.artifact_update.artifact
        return f"[artifact] {artifact.name}: {_artifact_content(artifact)[:160]}"

    if kind == "message":                               # 直接回消息（未建任务）
        return f"[message] {get_stream_response_text(event)[:160]}"

    return f"[{kind}]"


async def _send(client, text: str, *, task_id: str | None = None,
                metadata: dict | None = None, title: str = "") -> str | None:
    """
    发一条 A2A 消息，打印全程状态流转，返回本次任务的 taskId。

    参数：
        text     —— 自然语言输入
        task_id  —— 继续已有任务时传入（如补齐信息）；首次委托传 None
        metadata —— 控制信息（skill / session_id / user_id …）
    """
    if title:
        print(f"\n=== {title} ===")

    # ★ 坑：SDK 的 new_text_message 默认 role=ROLE_AGENT(2)，
    #   而 A2A 服务端要求「客户端发来的消息必须是 ROLE_USER」，不显式指定会被拒。
    message = new_text_message(text, task_id=task_id, role=Role.ROLE_USER)
    if metadata:
        # A2A 的 metadata 是 protobuf Struct，可以直接按 dict 语义写
        message.metadata.update({k: v for k, v in metadata.items() if v is not None})

    request = SendMessageRequest(message=message)

    seen_task_id = task_id
    final_state = None

    async for event in client.send_message(request):
        print("   ", _summarize(event))

        kind = event.WhichOneof("payload")
        if kind == "task":
            seen_task_id = event.task.id
            final_state = event.task.status.state
            for artifact in event.task.artifacts:
                print(f"      ↳ artifact[{artifact.name}]: {_artifact_content(artifact)[:200]}")
        elif kind == "status_update":
            seen_task_id = event.status_update.task_id or seen_task_id
            final_state = event.status_update.status.state

    if final_state is not None:
        print(f"    → 任务结束状态：{_state_name(final_state)}")
    return seen_task_id


async def run(base_url: str = DEFAULT_BASE_URL) -> None:
    async with _make_http_client(base_url) as http:
        # ── ① 发现：本 Agent 能干什么、端点在哪 ──────────────
        resolver = A2ACardResolver(httpx_client=http, base_url=base_url)
        card = await resolver.get_agent_card()
        print("① 发现远端 Agent")
        print(f"   名称：{card.name} v{card.version}")
        print(f"   端点：{card.supported_interfaces[0].url}（{card.supported_interfaces[0].protocol_binding}）")
        for skill in card.skills:
            print(f"   skill {skill.id:12s} {skill.name} —— {skill.description[:38]}")

        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)

        try:
            # ── ② + ③ 委托一个法规知识问答 ────────────────────
            await _send(
                client,
                "《劳动合同法》对试用期是怎么规定的？",
                metadata={"skill": SKILL_REGULATION, "session_id": "ext-session-001", "user_id": "legal-001"},
                title="委托任务 A：法规知识问答（外部显式指定 skill）",
            )

            # ── ② + ③ 委托一个信息不足的审查：预期停在「需补充信息」──
            clarify_task_id = await _send(
                client,
                "帮我看看这份文件",
                metadata={"skill": SKILL_REVIEW, "session_id": "ext-session-002", "user_id": "legal-001"},
                title="委托任务 B：审查要素不足（预期停在 input-required）",
            )

            # ── ④ 补齐信息，让被挂起的任务继续跑完 ────────────
            if clarify_task_id:
                await _send(
                    client,
                    "这是一份给外部供应商用的《供应商管理办法》，重点想排查付款与廉洁条款",
                    task_id=clarify_task_id,
                    metadata={"session_id": "ext-session-002", "user_id": "legal-001"},
                    title="补齐信息后继续任务 B",
                )
            else:
                print("\n（未拿到 taskId，跳过补齐信息演示）")

        finally:
            await client.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    asyncio.run(run(target))
