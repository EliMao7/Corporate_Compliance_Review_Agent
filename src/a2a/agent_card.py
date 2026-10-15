# src/a2a/agent_card.py
# A2A Agent Card：把企业合规审查系统的五条意图链路暴露为可被外部系统发现的 skills
#
# Agent Card 是 A2A 的「能力说明书」：外部 Agent（企业 OA、法务系统、合同平台、
# 其他厂商的助手）先拉这张卡，才知道：
#   这个 Agent 叫什么 / RPC 端点在哪 / 支持哪些 skill / 吃什么输入 / 吐什么输出
# 然后才决定要不要把任务委托过来。
# 公开发现路径：{base_url}/.well-known/agent-card.json
#
# ★ skill 与 src/agents/state.py 的 IntentType 严格一一对应 ——
#   外部按 skill 委托时，等价于告诉本系统「这次期望哪类能力」；
#   实际执行仍由系统自己的置信度路由与安全门决定（见 executor.py 的设计说明）。
#
# 依赖：a2a-sdk >= 1.2（1.x 起 types 为 protobuf 实现）

from __future__ import annotations

import os
from pathlib import Path

# 允许单独运行本文件自检（不走包导入）
_project_root = Path(__file__).parent.parent.parent.resolve()
import sys  # noqa: E402

if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from a2a.types import (  # noqa: E402
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

# ── 协议常量 ────────────────────────────────────────────────
PROTOCOL_BINDING = "JSONRPC"          # 本卡声明的传输绑定（SDK 另支持 HTTP+JSON / gRPC）
PROTOCOL_VERSION = "1.0"              # 遵循的 A2A spec 版本
AGENT_VERSION = "1.0.0"               # 本服务自身版本
RPC_PATH = "/a2a"                     # JSON-RPC 端点路径
AGENT_CARD_PATH = "/.well-known/agent-card.json"   # 公开发现路径

AGENT_NAME = "CorporateComplianceReviewAgent"

# ── skills：id 与 src/agents/state.py 的 IntentType 取值严格一致（R1）──
# 意图类型的唯一主人是 src/agents/state.py 的 IntentType，这里**导入**而非重声明，
# 从根上消除"A2A 能力清单与内部意图分叉"的可能。
from src.agents.state import IntentType as _IntentType

SKILL_REVIEW = _IntentType.REVIEW          # 合规排查
SKILL_MEASURE = _IntentType.MEASURE        # 措施建议
SKILL_REGULATION = _IntentType.REGULATION  # 法规知识
SKILL_REDLINE = _IntentType.REDLINE        # 高风险红线
SKILL_GENERAL = _IntentType.GENERAL        # 通用咨询

# 外部系统用 metadata.skill 指定「期望哪类能力」；不指定则让本系统自行判意图

_TEXT_IN = ["text/plain"]
_TEXT_OUT = ["text/plain"]


def _build_skills() -> list[AgentSkill]:
    """声明本服务对外的五项能力 —— 与 5 条意图链路一一对应，每项都可被独立委托。"""
    return [
        AgentSkill(
            id=SKILL_REVIEW,
            name="合规排查",
            description="对制度 / 合同等文件做合规排查：识别涉及事项、比对应适用法规、提示风险点并定位归口部门。",
            tags=["compliance", "review", "matter"],
            examples=["我们准备发一份《供应商管理办法》，有没有跟法规冲突的地方？", "这份合同里的付款条款合规吗？"],
            input_modes=_TEXT_IN,
            output_modes=_TEXT_OUT,
        ),
        AgentSkill(
            id=SKILL_MEASURE,
            name="措施建议",
            description="针对已识别的不合规表述或风险点，给出可落地的整改措施与修改建议；无锚点时退化为措施知识问答。",
            tags=["measure", "remediation", "advice"],
            examples=["这条条款怎么写才合规？", "无依据增设审批前置应该怎么改？"],
            input_modes=_TEXT_IN,
            output_modes=_TEXT_OUT,
        ),
        AgentSkill(
            id=SKILL_REGULATION,
            name="法规知识问答",
            description="基于知识图谱（实体链接 → 图查询 / Text2Cypher）与向量检索回答法规条文内容、适用范围等事实问题。",
            tags=["knowledge-graph", "rag", "regulation"],
            examples=["《劳动合同法》对试用期是怎么规定的？", "《优化营商环境条例》的适用范围是什么？"],
            input_modes=_TEXT_IN,
            output_modes=_TEXT_OUT,
        ),
        AgentSkill(
            id=SKILL_REDLINE,
            name="高风险红线提示",
            description="识别无依据增设行政许可 / 处罚、变相设置审批前置、超越制定权限等高风险红线信号，并给出强提示。",
            tags=["redline", "safety", "alert"],
            examples=["我们想在制度里增设一道审批前置，可以吗？", "这份文件自行设定了罚款数额"],
            input_modes=_TEXT_IN,
            output_modes=_TEXT_OUT,
        ),
        AgentSkill(
            id=SKILL_GENERAL,
            name="通用咨询",
            description="非合规类问题的日常交流；不进入安全门，避免为闲聊强加免责声明。",
            tags=["chat", "non-compliance"],
            examples=["你好，你能做什么？"],
            input_modes=_TEXT_IN,
            output_modes=_TEXT_OUT,
        ),
    ]


def resolve_public_base_url() -> str:
    """
    解析对外可达的服务基地址（写进 Agent Card 的 supportedInterfaces[].url）。

    外部系统拿到的是这个地址，所以必须是「对方能访问到的」地址：
      本机调试 → http://127.0.0.1:8000
      部署上线 → http://<内网 IP 或域名>:<端口>
    优先级：settings.a2a_public_url（可用 A2A_PUBLIC_URL 注入）> http://127.0.0.1:{api_port}
    """
    try:
        from config.settings import settings

        configured = (getattr(settings, "a2a_public_url", "") or "").strip()
        if configured:
            return configured.rstrip("/")
        port = getattr(settings, "api_port", 8000)
    except Exception:                       # 脱离项目配置时的兜底，保证卡片仍可构造
        configured = os.getenv("A2A_PUBLIC_URL", "").strip()
        if configured:
            return configured.rstrip("/")
        port = os.getenv("API_PORT", "").strip() or "8000"

    return f"http://127.0.0.1:{port}"


def build_agent_card(base_url: str | None = None) -> AgentCard:
    """
    构造 Agent Card。

    参数：base_url —— 服务基地址；不传则按 resolve_public_base_url() 解析。
    返回：AgentCard（protobuf 消息，可直接序列化为 JSON 挂到 well-known 路径）。
    """
    base = (base_url or resolve_public_base_url()).rstrip("/")

    return AgentCard(
        name=AGENT_NAME,
        description="基于知识图谱的企业合规审查系统：9 个 Agent 协同完成制度 / 合同合规排查、"
                    "归口部门定位、合规措施建议、法规条文咨询与高风险红线提示，"
                    "全部回答经强制安全门附加免责声明。",
        version=AGENT_VERSION,
        supported_interfaces=[
            AgentInterface(
                url=f"{base}{RPC_PATH}",
                protocol_binding=PROTOCOL_BINDING,
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=True),   # 支持 SSE 流式返回任务状态
        default_input_modes=_TEXT_IN,
        default_output_modes=_TEXT_OUT,
        skills=_build_skills(),
    )


if __name__ == "__main__":
    # 离线自检：不启服务，只验证卡片能构造并序列化
    import json

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from a2a.server.routes.agent_card_routes import agent_card_to_dict

    card = build_agent_card()
    print(json.dumps(agent_card_to_dict(card), ensure_ascii=False, indent=2))
    print("\nskills:", [s.id for s in card.skills])
    print("interface:", card.supported_interfaces[0].url)
    print("[SUCCESS] agent_card self-check passed")
