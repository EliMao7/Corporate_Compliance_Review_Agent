# src/a2a/app.py
# 把 A2A 的三类路由装配到 FastAPI 上
#
# A2A 服务端由三块组成（缺一不可）：
#   1. Agent Card 路由   —— GET /.well-known/agent-card.json，供外部「发现」本 Agent
#   2. JSON-RPC 路由     —— POST /a2a，A2A 的主传输绑定（Agent Card 里声明的就是它）
#   3. REST 路由（可选） —— POST /a2a/message:send 等，给不方便发 JSON-RPC 的调用方
#
# ★ 挂载顺序要求：必须在 src/api/main.py 的「前端 SPA 兜底路由」**之前**调用。
#   那条兜底是 @app.get("/{full_path:path}")，会吞掉所有 GET；
#   Starlette 按注册顺序匹配，先注册的 Agent Card 路由才能拿到
#   /.well-known/agent-card.json 这个 GET 请求。

from __future__ import annotations

import logging
from pathlib import Path
import sys

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI  # noqa: E402

from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes.agent_card_routes import create_agent_card_routes  # noqa: E402
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi  # noqa: E402
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes  # noqa: E402
from a2a.server.routes.rest_routes import create_rest_routes  # noqa: E402
from a2a.server.tasks import InMemoryTaskStore  # noqa: E402

from src.a2a.agent_card import AGENT_CARD_PATH, RPC_PATH, build_agent_card  # noqa: E402
from src.a2a.executor import ComplianceReviewExecutor  # noqa: E402

logger = logging.getLogger(__name__)


def install_a2a(
    app: FastAPI,
    *,
    rpc_path: str = RPC_PATH,
    card_path: str = AGENT_CARD_PATH,
    include_rest: bool = True,
    executor: ComplianceReviewExecutor | None = None,
    public_base_url: str | None = None,
) -> DefaultRequestHandler:
    """
    在既有 FastAPI 应用上启用 A2A 对外协作端点。

    参数：
        app             —— 目标 FastAPI 应用（挂在主应用上，Agent Card 才能落在根路径）
        rpc_path        —— JSON-RPC 端点路径，默认 /a2a
        card_path       —— Agent Card 发现路径，默认 /.well-known/agent-card.json
        include_rest    —— 是否同时开启 REST 绑定
        executor        —— 自定义执行器（默认用 ComplianceReviewExecutor）
        public_base_url —— 写进 Agent Card 的对外地址；不传则按配置解析

    返回：DefaultRequestHandler（便于测试或扩展）
    """
    card = build_agent_card(public_base_url)
    agent_executor = executor or ComplianceReviewExecutor()

    handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=InMemoryTaskStore(),      # 任务态存内存；多副本部署应换持久化 TaskStore
        agent_card=card,
    )

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card, card_url=card_path),
        jsonrpc_routes=create_jsonrpc_routes(
            handler,
            rpc_url=rpc_path,
            enable_v0_3_compat=True,         # 兼容仍按 A2A 0.3 报文格式调用的旧客户端
        ),
        rest_routes=create_rest_routes(handler, path_prefix=rpc_path) if include_rest else None,
    )

    logger.info(
        "a2a.installed | rpc=%s card=%s rest=%s base=%s",
        rpc_path, card_path, include_rest, card.supported_interfaces[0].url,
    )
    return handler


def build_standalone_app(public_base_url: str | None = None) -> FastAPI:
    """
    只含 A2A 端点的独立应用，用于本机联调：
        python -m src.a2a.app            # 起在 127.0.0.1:9100
    这样即使 Neo4j / FAISS / Redis / LLM 没起，也能先把 A2A 协议链路单独验通。
    """
    app = FastAPI(title="CorporateComplianceReviewAgent A2A Endpoint", version="1.0.0")
    install_a2a(app, public_base_url=public_base_url or "http://127.0.0.1:9100")

    @app.get("/health", tags=["系统"])
    async def health() -> dict:
        return {"status": "ok", "protocol": "A2A", "binding": "JSONRPC"}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_standalone_app(), host="127.0.0.1", port=9100)
