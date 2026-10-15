# src/a2a/executor.py
# A2A AgentExecutor：把 A2A 的 Task 翻译成「企业合规审查 Agent 系统」的一次审查
#
# 这一层是 A2A 能力的核心适配器，职责只有三件事：
#   1. 路由：从 A2A 消息里解析出「期望用哪类能力」（外部显式指定 or 交回系统自判）
#   2. 执行：调 LangGraph 编排图跑一次审查，拿到终态
#   3. 状态回写：把终态映射成 A2A 的任务状态（提交 → 执行中 → 完成 / 需补充信息 / 失败）
#
# ★ 设计约束：本模块 **不在模块级 import 任何 src.* / config.* 业务模块**，
#   业务依赖全部延迟到方法内部。好处是这一层可以脱离 Neo4j / FAISS / Redis / LLM
#   单独起服务、单独做冒烟测试（见同目录 smoke_test.py 与 test_adapter.py）。
#
# ★ 信息不足的追问 ←→ A2A 的 TASK_STATE_INPUT_REQUIRED（本文件最值钱的一段）：
#   系统的 matter_detector / authority_locator 在事项信息不足时会置
#   needs_clarification=True + clarification=追问话术（如「这份制度是给内部员工还是
#   外部供应商用的？」）。A2A 层把这种「挂起等补充」原样表达成
#   INPUT_REQUIRED + clarification 产物；外部系统补齐信息后带同一 taskId 再发一条，
#   服务端把补充信息作为新一轮输入、原问题进对话历史，重跑并落库。

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from a2a.helpers import (
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from src.a2a.agent_card import (
    AGENT_NAME,
    SKILL_GENERAL,
    SKILL_MEASURE,
    SKILL_REDLINE,
    SKILL_REGULATION,
    SKILL_REVIEW,
)

logger = logging.getLogger(__name__)

# ── 返回契约：一次审查的结果 ────────────────────────────────────
# {"status": "completed"|"input_required"|"failed", "content": str,
#  "structured": dict|None, "display": dict|None, "resume_token": str|None,
#  "skill": str|None}
AgentResult = dict[str, Any]

# 可注入的三个接缝（生产用默认实现；测试可替换成桩，从而不需要基础设施）
RouterFn = Callable[..., Awaitable[str]]
InvokeFn = Callable[..., Awaitable[AgentResult]]
ResumeFn = Callable[..., Awaitable[AgentResult]]

# ── skill 归一化：允许外部用中文/别名指定能力 ─────────────────────
_SKILL_ALIASES: dict[str, str] = {
    "review": SKILL_REVIEW, "合规": SKILL_REVIEW, "排查": SKILL_REVIEW,
    "审查": SKILL_REVIEW, "合规排查": SKILL_REVIEW,
    "measure": SKILL_MEASURE, "措施": SKILL_MEASURE, "整改": SKILL_MEASURE,
    "建议": SKILL_MEASURE, "remediation": SKILL_MEASURE,
    "regulation": SKILL_REGULATION, "法规": SKILL_REGULATION, "条文": SKILL_REGULATION,
    "知识": SKILL_REGULATION, "qa": SKILL_REGULATION,
    "redline": SKILL_REDLINE, "红线": SKILL_REDLINE, "高风险": SKILL_REDLINE,
    "预警": SKILL_REDLINE,
    "general": SKILL_GENERAL, "通用": SKILL_GENERAL, "chat": SKILL_GENERAL,
    "闲聊": SKILL_GENERAL,
}

_VALID_SKILLS = frozenset({
    SKILL_REVIEW, SKILL_MEASURE, SKILL_REGULATION, SKILL_REDLINE, SKILL_GENERAL,
})

_SKILL_DISPLAY = {
    SKILL_REVIEW: "合规排查",
    SKILL_MEASURE: "措施建议",
    SKILL_REGULATION: "法规知识问答",
    SKILL_REDLINE: "高风险红线提示",
    SKILL_GENERAL: "通用咨询",
}


def normalize_skill(raw: Any) -> str | None:
    """把外部传来的 skill 写法归一化成标准 skill id；无法识别返回 None。"""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    if key in _VALID_SKILLS:
        return key
    return _SKILL_ALIASES.get(key)


def _struct_to_dict(value: Any) -> dict[str, Any]:
    """
    把 protobuf 的 Struct（A2A 消息 metadata 的类型）转成普通 dict。

    A2A 的 metadata 字段是 google.protobuf.Struct（不是 dict），
    外部系统通过它传「期望哪个 skill / 业务 session_id / 补充信息」等控制信息。
    """
    if value is None:
        return {}
    try:
        from google.protobuf.json_format import MessageToDict

        out = MessageToDict(value)
        return out if isinstance(out, dict) else {}
    except Exception:
        try:
            return dict(value)          # 部分 protobuf 版本支持直接 dict()
        except Exception:
            return {}


# ──────────────────────────────────────────────────────────────
# 业务侧调用（延迟导入；这两层薄封装也是测试注入假 graph 的接缝）
# ──────────────────────────────────────────────────────────────


def _load_history(session_id: str) -> list:
    """
    取多轮对话上下文（与 /api/consult 同一套 store：Redis 热缓存 → MySQL 回填）。

    A2A 是新增的对外入口，不应因为 Redis/MySQL 不可用就把整条协作链路打断，
    故失败降级为空历史（等价于单轮审查），并在日志里留痕。
    """
    if not session_id:
        return []
    try:
        from src.history.store import history_store

        return list(history_store.load_context(session_id) or [])
    except Exception as e:
        logger.warning("a2a.history_unavailable | session=%s error=%s", session_id, e)
        return []


async def _run_graph(query: str, history: list | None = None) -> dict:
    """
    跑一次审查图（src.agents.graph.run 是同步函数，放线程池里跑避免阻塞事件循环）。

    这里与 /api/consult 走的是同一个 run()，A2A 没有另起一套执行路径 ——
    差异只在入口（HTTP 表单 vs 协议消息）与出口（ConsultResponse vs Artifact）。
    """
    import asyncio

    from src.agents.graph import run

    return await asyncio.to_thread(run, query, history)


def _state_to_result(state: Any, *, skill: str | None, query: str) -> AgentResult:
    """把 LangGraph 终态（AgentState）映射成 A2A 层的统一返回契约。"""
    if not isinstance(state, dict):
        return {"status": "failed", "content": "合规审查系统返回了非预期结果，请稍后重试。"}

    # 实际意图优先用于产物命名：skill 只是外部「期望」，intent 才是系统判定的结果
    actual = str(state.get("intent") or "").strip() or None
    effective = actual or skill

    # ── 信息不足 → 挂起等补充（A2A 的 INPUT_REQUIRED）──
    if state.get("needs_clarification") and state.get("clarification"):
        return {
            "status": "input_required",
            "skill": effective,
            "content": str(state["clarification"]),
            "display": {
                "clarification": str(state["clarification"]),
                "original_query": query,
                "intent": actual or "",
                "matters": list(state.get("matters") or []),
            },
        }

    disclaimers = list(state.get("disclaimers") or [])
    warnings = list(state.get("warnings") or [])
    answer = str(state.get("final_answer") or "")

    return {
        "status": "completed",
        "skill": effective,
        "content": answer,
        "structured": {
            "intent": actual or "",
            "intent_confidence": state.get("intent_confidence"),
            "doc_type": state.get("doc_type") or "",
            "matters": list(state.get("matters") or []),
            "risk_signals": list(state.get("risk_signals") or []),
            "severity": state.get("severity") or "",
            "authorities": list(state.get("authorities") or []),
            "measures": list(state.get("measures") or []),
            "linked_entities": list(state.get("linked_entities") or []),
        },
        # 单独出一份安全产物：免责声明与高风险预警是**强制**过安全门的产出，
        # 外部系统可以据此做合规展示，不必从正文里正则捞。
        "safety": {
            "disclaimers": disclaimers,
            "warnings": warnings,
            "is_redline": bool(state.get("is_redline")),
        },
    }


# ──────────────────────────────────────────────────────────────
# 默认接缝实现
# ──────────────────────────────────────────────────────────────


async def _default_router(text: str, requested_skill: str | None = None) -> str:
    """
    默认路由：只做「归一化 + 校验」，不做第二套意图分类。

    为什么不去调 LLM 再判一次：图的第一个节点 intent_classifier 已经在做这件事，
    在 A2A 层重复判会引入「两处结论不一致」的隐患。所以这里的取值是：
      外部显式指定 skill → 作为期望能力记录下来（进 artifact 命名与 metadata）
      未指定            → 返回空串，表示「交回系统自判」

    ★ 安全边界（TDD §11.3）：这里记录的只是「外部期望」，并不绕过系统自身的
      置信度路由与安全门 —— 无论外部声明哪个 skill，实际执行仍走同一张图，
      免责声明与高风险提示照常产出，A2A 层不提供任何跳过安全门的旁路。
    """
    return normalize_skill(requested_skill) or ""


async def _default_invoke_agent(
    skill: str,
    text: str,
    *,
    metadata: dict[str, Any],
    session_id: str,
    user_id: str,
) -> AgentResult:
    """默认执行：一次完整的审查（含多轮上下文加载）。"""
    biz_session = str(metadata.get("session_id") or session_id or "")
    history = _load_history(biz_session)
    state = await _run_graph(text, history)
    return _state_to_result(state, skill=skill, query=text)


async def _default_resume_agent(resume_token: str, decision: dict[str, Any]) -> AgentResult:
    """
    补齐信息后继续：把用户补充的描述作为**新一轮输入**，
    原问题作为对话历史一起喂给同一张图，等价于用户在聊天框里接着说了下半句。

    映射关系是清楚的：A2A 的「同一 taskId 再发一条消息」
      == 本系统多轮审查里的「同一 session 的第二轮」。
    """
    answer = str(decision.get("answer") or "").strip()
    original = str(decision.get("original_query") or "").strip()
    skill = decision.get("skill") or ""

    if not answer:
        return {
            "status": "completed",
            "skill": skill,
            "content": "未收到补充信息，本轮不做处理。",
        }

    history = _load_history(resume_token)
    if original:
        history = history + [{"role": "user", "content": original}]

    state = await _run_graph(answer, history)
    return _state_to_result(state, skill=skill, query=answer)


# ──────────────────────────────────────────────────────────────
# A2A 任务执行器
# ──────────────────────────────────────────────────────────────


class ComplianceReviewExecutor(AgentExecutor):
    """
    企业合规审查系统的 A2A 执行器。

    参数（均可省略，省略时用上面的默认实现）：
        router        —— 路由函数，决定这次请求期望哪类能力
        invoke_agent  —— 首次执行函数（跑一次审查）
        resume_agent  —— 补齐信息后的继续执行函数
    """

    def __init__(
        self,
        router: RouterFn | None = None,
        invoke_agent: InvokeFn | None = None,
        resume_agent: ResumeFn | None = None,
    ) -> None:
        self._router = router or _default_router
        self._invoke_agent = invoke_agent or _default_invoke_agent
        self._resume_agent = resume_agent or _default_resume_agent
        # task_id -> {"resume_token", "skill", "query"}
        # 进程内暂存「停在需补充信息」的任务。生产多副本部署应换成 Redis/MySQL，
        # 否则补齐信息的请求可能落到没有该 state 的实例上。
        self._pending: dict[str, dict[str, Any]] = {}

    # ── 主流程 ────────────────────────────────────────────────
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = getattr(context, "message", None)

        # A2A 的状态机约束：必须先把 Task 交给客户端，之后才允许发状态/产物事件。
        # 续跑已有任务时 current_task 由框架注入，首次委托则要自己建一个。
        task = getattr(context, "current_task", None)
        if task is None:
            if message is None:
                return
            task = new_task_from_user_message(message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)

        text = get_message_text(message).strip() if message is not None else ""

        # 控制信息可能挂在消息 metadata 上，也可能挂在请求 metadata 上，两处都收
        metadata = _struct_to_dict(getattr(message, "metadata", None))
        metadata.update(_struct_to_dict(getattr(context, "metadata", None)))

        user_id = str(metadata.get("user_id") or metadata.get("operator_id") or "a2a-external")
        # 业务会话键：优先外部显式传的 session_id（便于与站内历史合并），否则用 A2A 的 contextId
        biz_session = str(metadata.get("session_id") or context_id or "")

        try:
            # ── 分支一：这是对「需补充信息」任务的回应 → 继续执行 ──
            pending = self._pending.get(task_id)
            if pending is not None and not metadata.get("restart"):
                self._pending.pop(task_id, None)
                await updater.start_work(
                    message=new_text_message(f"{AGENT_NAME} 已收到补充信息，继续审查…")
                )
                decision = {
                    "answer": metadata.get("answer") or text,
                    "original_query": pending.get("query") or "",
                    "skill": pending.get("skill") or "",
                    "user_id": user_id,
                }
                result = await self._resume_agent(pending.get("resume_token") or biz_session, decision)
                await self._emit_result(
                    updater, task_id, result, skill=pending.get("skill"), session=biz_session
                )
                return

            # ── 分支二：首次执行 ──────────────────────────────
            await updater.start_work(
                message=new_text_message("已接收合规审查请求，正在路由到合适的 Agent…")
            )

            skill = await self._router(text, metadata.get("skill"))
            logger.info("a2a.task_routed | task_id=%s expected_skill=%s", task_id, skill or "(auto)")

            result = await self._invoke_agent(
                skill,
                text,
                metadata=metadata,
                session_id=biz_session,
                user_id=user_id,
            )
            await self._emit_result(updater, task_id, result, skill=skill, session=biz_session)

        except Exception as e:
            logger.error("a2a.task_failed | task_id=%s error=%s", task_id, e, exc_info=True)
            await updater.failed(message=new_text_message(f"审查执行失败：{e}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """外部系统取消任务：清掉待补充信息的暂存，并把任务置为已取消。"""
        task = getattr(context, "current_task", None)
        task_id = (task.id if task is not None else None) or getattr(context, "task_id", None)
        context_id = (task.context_id if task is not None else None) or getattr(context, "context_id", None)

        if task_id:
            self._pending.pop(task_id, None)

        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        await updater.cancel(message=new_text_message("任务已取消。"))

    # ── 内部工具 ──────────────────────────────────────────────
    async def _emit_result(
        self,
        updater: TaskUpdater,
        task_id: str,
        result: AgentResult,
        *,
        skill: str | None = None,
        session: str = "",
    ) -> None:
        """把执行结果映射成 A2A 的任务状态与产物（Artifact）。"""
        status = result.get("status", "completed")
        effective = result.get("skill") or skill or ""

        # ① 信息不足：挂起为 INPUT_REQUIRED，等外部补齐
        if status == "input_required":
            self._pending[task_id] = {
                # 本系统的图没有 checkpointer，所以「恢复」不是回放图，
                # 而是把补充信息当新一轮输入重跑 —— 恢复键因此是业务会话键。
                "resume_token": result.get("resume_token") or session,
                "skill": effective,
                "query": (result.get("display") or {}).get("original_query", ""),
            }
            clarification = result.get("content") or "需要您补充更多信息。"
            await updater.add_artifact(
                parts=[new_text_part(clarification)],
                name="clarification",
                metadata={"description": "信息不足时的追问话术"},
            )
            display = result.get("display") or {}
            if display:
                try:
                    await updater.add_artifact(
                        parts=[new_data_part(display)],
                        name="clarification_context",
                        metadata={"description": "已识别的意图与事项快照"},
                    )
                except Exception as e:
                    logger.warning("a2a.structured_artifact_failed | %s", e)
            await updater.requires_input(message=new_text_message(clarification))
            return

        # ② 失败
        if status == "failed":
            await updater.failed(message=new_text_message(result.get("content") or "审查失败。"))
            return

        # ③ 成功：正文作为文本产物，结构化字段与安全声明各出一份 data 产物
        content = result.get("content") or ""
        await updater.add_artifact(
            parts=[new_text_part(content)],
            name=f"{effective or 'review'}_answer",
            metadata={"description": f"{_SKILL_DISPLAY.get(effective or '', '审查')}结果"},
        )

        for name, payload in (
            ("structured", result.get("structured")),
            ("safety", result.get("safety")),
        ):
            if not payload:
                continue
            try:
                await updater.add_artifact(parts=[new_data_part(payload)], name=name)
            except Exception as e:
                # data part 序列化失败不应让整个任务失败，降级成文本
                logger.warning("a2a.structured_artifact_failed | name=%s error=%s", name, e)
                await updater.add_artifact(
                    parts=[new_text_part(json.dumps(payload, ensure_ascii=False, default=str))],
                    name=name,
                )

        await updater.complete(message=new_text_message("审查已完成。"))
