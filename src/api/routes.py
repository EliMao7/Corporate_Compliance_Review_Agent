"""
API Routes
FastAPI router with compliance-review, auth, health, and stats endpoints.
Includes timeout handling, concurrency control, request logging, and JWT auth.

路由优先级：/api/*（本文件）→ 由 main.py 负责的前端静态与 SPA 兜底。
"""

import json
import sys
import time
import uuid
import base64
import hashlib
import asyncio
from pathlib import Path

import bcrypt
import jwt

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config.settings import settings
from src.common.logger import setup_logger
from src.history.store import history_store
from src.api.models import (
    ConsultRequest,
    ConsultResponse,
    HealthResponse,
    StatsResponse,
    ErrorResponse,
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    ConversationListResponse,
    ConversationDetailResponse,
    DeleteConversationResponse
)

logger = setup_logger(__name__, "api.log")

router = APIRouter()

# Timeout configuration
# 安全上限：典型轮次 2-8s；带记忆+多 Agent+LLM 兜底的轮次偶有更慢，故放宽上限。
# 真正的体验优化应走流式输出（见 §5.4 性能优化），此处仅为防失控的天花板。
REQUEST_TIMEOUT = settings.request_timeout  # 默认 60 秒


# ============================================================
# 认证与数据隔离（§6.4）
# 密码：bcrypt（SHA-256 预哈希 + rounds=12），不可逆，任何地方不存明文。
# 令牌：JWT（HS256），无状态，过期即失效。
# 越权读取/删除他人会话一律 404（不泄露"该资源是否存在"）；未登录一律 401（健康检查除外）。
# ============================================================

_bearer = HTTPBearer(auto_error=False)

# 未登录/令牌失效时的统一回退话术（默认免责声明在 safety_checker 节点保证非空）
FALLBACK_DISCLAIMER = (
    "🔴 重要声明：本系统只提供法规依据检索与风险提示，不提供法律意见，"
    "也不对文件是否合法作出结论。以上内容请由法务或执业律师复核后使用。"
)


def _prehash(password: str) -> bytes:
    """SHA-256 预哈希并 base64，规避 bcrypt 72 字节截断。"""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    """生成不可逆密码哈希（SHA-256 预哈希 + bcrypt rounds=12）。"""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码是否匹配存储的哈希。"""
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, username: str) -> tuple[str, int]:
    """签发无状态 JWT（HS256），返回 (token, 有效期秒数)。"""
    expires_in = settings.jwt_expire_minutes * 60
    payload = {
        "sub": user_id,
        "username": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str) -> dict:
    """解码并校验 JWT；失败抛 HTTPException(401)。"""
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """从 Authorization: Bearer <JWT> 解析当前用户；未登录/失效 → 401。

    auth_enabled=false 时跳过鉴权（仅限本地联调/评测；生产必须保持开启）。
    """
    if not settings.auth_enabled:
        return {"user_id": "anonymous", "username": "anonymous"}
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return {"user_id": user_id, "username": payload.get("username", "")}


# ============================================================
# 认证端点：注册 / 登录
# ============================================================

@router.post(
    "/auth/register",
    response_model=TokenResponse,
    tags=["认证"],
    summary="注册",
    description="用户名 + 密码注册；密码只存不可逆哈希。注册成功即返回访问令牌。"
)
def register(req: RegisterRequest):
    """注册新用户并签发令牌。"""
    if history_store.get_user_by_username(req.username) is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    user_id = uuid.uuid4().hex
    history_store.create_user(user_id, req.username, hash_password(req.password))
    token, expires_in = create_access_token(user_id, req.username)
    logger.info(f"[AUTH] Registered user '{req.username}' (id={user_id})")
    return TokenResponse(
        access_token=token, expires_in=expires_in, user_id=user_id, username=req.username
    )


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    tags=["认证"],
    summary="登录",
    description="用户名 + 密码登录；校验成功后返回无状态 JWT。"
)
def login(req: LoginRequest):
    """校验凭据并签发令牌。用户名不存在与密码错误返回同一提示，不泄露账号是否存在。"""
    user = history_store.get_user_by_username(req.username)
    if user is None or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token, expires_in = create_access_token(user["id"], user["username"])
    logger.info(f"[AUTH] Login success: '{req.username}' (id={user['id']})")
    return TokenResponse(
        access_token=token, expires_in=expires_in, user_id=user["id"], username=user["username"]
    )


# ============================================================
# Request Logging Helper
# ============================================================

def log_request(
    query: str,
    intent: str,
    duration_ms: int,
    has_disclaimer: bool,
    success: bool
):
    """Log request details for monitoring"""
    status = "✅" if success else "❌"
    logger.info(
        f"[REQUEST] {status} "
        f"query='{query[:30]}...' | "
        f"intent={intent} | "
        f"duration={duration_ms}ms | "
        f"disclaimer={'Y' if has_disclaimer else 'N'}"
    )


# ============================================================
# POST /api/consult - Compliance Review Endpoint
# ============================================================

@router.post(
    "/consult",
    response_model=ConsultResponse,
    responses={
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse}
    },
    tags=["合规审查"],
    summary="合规审查接口",
    description="接收文件/事项描述，返回法规依据检索与风险提示（60秒超时，5并发限制）"
)
async def consult(req: ConsultRequest, user: dict = Depends(get_current_user)):
    """
    Process user compliance-review request

    - Accepts user question
    - Runs through Agent system with timeout and concurrency control
    - Returns review result with disclaimers
    """
    from src.api.main import app_state

    # Check if agent system is ready
    if not app_state.agent_system_ready:
        raise HTTPException(
            status_code=503,
            detail="Agent system not ready. Please try again later."
        )

    start_time = time.time()
    app_state.total_requests += 1

    logger.info(f"[API] Review request: '{req.query[:50]}...' (session: {req.session_id}, user: {user['user_id']})")

    try:
        # Acquire semaphore for concurrency control
        async with app_state.request_semaphore:
            # Call Agent system with timeout
            from src.agents.graph import run

            # 加载多轮上下文（三级命中：Redis 热缓存 → MySQL 回填）
            history = history_store.load_context(req.session_id)

            try:
                # Run in thread pool to not block event loop
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, run, req.query, history
                    ),
                    timeout=REQUEST_TIMEOUT
                )

                duration_ms = int((time.time() - start_time) * 1000)

                # Build response
                disclaimers = result.get("disclaimers", [])
                response = ConsultResponse(
                    answer=result.get("final_answer", ""),
                    intent=result.get("intent", ""),
                    intent_confidence=result.get("intent_confidence", 0.0),
                    doc_type=result.get("doc_type", ""),
                    matters=result.get("matters", []),
                    risk_signals=result.get("risk_signals", []),
                    authorities=result.get("authorities", []),
                    measures=result.get("measures", []),
                    disclaimers=disclaimers,
                    warnings=result.get("warnings", []),
                    linked_entities=result.get("linked_entities", []),
                    duration_ms=duration_ms
                )

                # Log request
                log_request(
                    query=req.query,
                    intent=response.intent,
                    duration_ms=duration_ms,
                    has_disclaimer=len(disclaimers) > 0,
                    success=True
                )

                # 双写对话历史（先 MySQL 再 Redis；任一失败降级，不使请求失败）
                meta = {
                    "doc_type": response.doc_type,
                    "matters": response.matters,
                    "risk_signals": response.risk_signals,
                }
                history_store.persist_turn(
                    req.session_id, "user", req.query, user_id=user["user_id"]
                )
                history_store.persist_turn(
                    req.session_id, "assistant", response.answer,
                    intent=response.intent, user_id=user["user_id"], meta=meta
                )

                return response

            except asyncio.TimeoutError:
                duration_ms = int((time.time() - start_time) * 1000)
                app_state.total_errors += 1

                log_request(
                    query=req.query,
                    intent="timeout",
                    duration_ms=duration_ms,
                    has_disclaimer=False,
                    success=False
                )

                raise HTTPException(
                    status_code=504,
                    detail=f"Request timeout after {REQUEST_TIMEOUT}s. Please try a simpler question."
                )

    except HTTPException:
        raise
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        app_state.total_errors += 1

        log_request(
            query=req.query,
            intent="error",
            duration_ms=duration_ms,
            has_disclaimer=False,
            success=False
        )

        logger.error(f"[API] Review failed: {e}")

        err_answer = "抱歉，系统暂时无法处理您的请求。请稍后重试，或请法务 / 执业律师复核。"
        # 记录本轮，避免上下文断裂
        history_store.persist_turn(req.session_id, "user", req.query, user_id=user["user_id"])
        history_store.persist_turn(
            req.session_id, "assistant", err_answer, intent="error", user_id=user["user_id"]
        )

        # Return friendly error
        return ConsultResponse(
            answer=err_answer,
            intent="error",
            disclaimers=[FALLBACK_DISCLAIMER],
            duration_ms=duration_ms
        )


# ============================================================
# POST /api/consult/stream - Streaming Review Endpoint (SSE)
# ============================================================

# 流结束哨兵：next(gen, _STREAM_DONE) 避免 StopIteration 穿越线程边界
# 进入 async 生成器（PEP 479 会把 async 生成器内的 StopIteration 转为 RuntimeError）
_STREAM_DONE = object()


def _sse_format(data: str, event: str | None = None) -> str:
    """把一条消息格式化为 SSE 帧：event:/data: 行 + 空行结尾（WHATWG SSE 规范）。"""
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _facts_from_delta(delta: dict) -> dict:
    """从节点 state 增量中提取可展示给用户的部分事实（progress 帧用）。"""
    facts = {}
    if delta.get("intent"):
        facts["intent"] = delta["intent"]
    if delta.get("doc_type"):
        facts["doc_type"] = delta["doc_type"]
    if delta.get("matters"):
        facts["matters"] = delta["matters"]
    if delta.get("risk_signals"):
        facts["risk_signals"] = delta["risk_signals"]
    if delta.get("authorities"):
        facts["authorities"] = delta["authorities"]
    if delta.get("linked_entities"):
        facts["linked_entities"] = [lk.get("matched_entity") for lk in delta["linked_entities"]]
    if delta.get("needs_clarification"):
        facts["needs_clarification"] = True
    return facts


def _build_response_from_state(state: dict, start_time: float) -> ConsultResponse:
    """从终态构建标准响应（/consult 与 /consult/stream 共用字段口径）。"""
    return ConsultResponse(
        answer=state.get("final_answer", ""),
        intent=state.get("intent", ""),
        intent_confidence=state.get("intent_confidence", 0.0),
        doc_type=state.get("doc_type", ""),
        matters=state.get("matters", []),
        risk_signals=state.get("risk_signals", []),
        authorities=state.get("authorities", []),
        measures=state.get("measures", []),
        disclaimers=state.get("disclaimers", []),
        warnings=state.get("warnings", []),
        linked_entities=state.get("linked_entities", []),
        duration_ms=int((time.time() - start_time) * 1000),
    )


@router.post(
    "/consult/stream",
    tags=["合规审查"],
    summary="流式审查接口（SSE）",
    description="与 /consult 相同的结果，但以 Server-Sent Events 流式推送："
                "event: progress（每个 Agent 节点完成的进度）→ event: answer（完整响应 JSON）"
                "→ event: done（[DONE] 结束标记）。客户端断开时提前终止。"
)
async def consult_stream(req: ConsultRequest, request: Request,
                         user: dict = Depends(get_current_user)):
    """SSE 流式审查。

    桥接设计：LangGraph app.stream 是同步生成器，本端点是 async def。
    用 asyncio.to_thread(lambda: next(gen, _STREAM_DONE)) 逐个取值——
    每个节点一次线程调度，既能在每步之间检查客户端断开，又不长时间占用线程池。
    """
    from src.api.main import app_state

    if not app_state.agent_system_ready:
        raise HTTPException(
            status_code=503,
            detail="Agent system not ready. Please try again later."
        )

    start_time = time.time()
    app_state.total_requests += 1
    logger.info(f"[API] Streaming review request: '{req.query[:50]}...' (session: {req.session_id}, user: {user['user_id']})")

    async def event_generator():
        from src.agents.graph import run_stream, NODE_LABELS_ZH

        final_state = None
        try:
            async with app_state.request_semaphore:
                history = history_store.load_context(req.session_id)
                gen = run_stream(req.query, history)
                while True:
                    # 先查客户端断开，再取下一块
                    if await request.is_disconnected():
                        logger.info("[API] Stream client disconnected, stopping early")
                        return
                    try:
                        chunk = await asyncio.wait_for(
                            asyncio.to_thread(lambda: next(gen, _STREAM_DONE)),
                            timeout=REQUEST_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        app_state.total_errors += 1
                        logger.error(f"[API] Stream timeout after {REQUEST_TIMEOUT}s")
                        yield _sse_format(
                            json.dumps({"message": f"处理超时（{REQUEST_TIMEOUT}s），请简化问题后重试"},
                                       ensure_ascii=False),
                            event="error",
                        )
                        return
                    if chunk is _STREAM_DONE:
                        break

                    kind = chunk[0]
                    if kind == "progress":
                        _, node_name, delta = chunk
                        payload = {
                            "node": node_name,
                            "label": NODE_LABELS_ZH.get(node_name, node_name),
                            "facts": _facts_from_delta(delta),
                        }
                        yield _sse_format(json.dumps(payload, ensure_ascii=False), event="progress")
                    else:  # kind == "values"：完整快照，最后一个即终态
                        final_state = chunk[1]
        except Exception as e:
            app_state.total_errors += 1
            logger.error(f"[API] Streaming review failed: {e}")
            yield _sse_format(
                json.dumps({"message": f"系统错误：{e}"}, ensure_ascii=False),
                event="error",
            )
            return

        # 终答帧：与 /consult 同口径的完整响应
        if final_state is not None:
            response = _build_response_from_state(final_state, start_time)
            log_request(
                query=req.query,
                intent=response.intent,
                duration_ms=response.duration_ms,
                has_disclaimer=len(response.disclaimers) > 0,
                success=True,
            )
            # 双写对话历史（与 /consult 一致）
            meta = {
                "doc_type": response.doc_type,
                "matters": response.matters,
                "risk_signals": response.risk_signals,
            }
            history_store.persist_turn(
                req.session_id, "user", req.query, user_id=user["user_id"]
            )
            history_store.persist_turn(
                req.session_id, "assistant", response.answer,
                intent=response.intent, user_id=user["user_id"], meta=meta
            )
            yield _sse_format(response.model_dump_json(), event="answer")

        yield _sse_format("[DONE]", event="done")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 防反向代理（Nginx 等）缓冲 SSE
        },
    )


# ============================================================
# GET /api/health - Health Check Endpoint
# ============================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["系统"],
    summary="健康检查",
    description="检查服务状态、Neo4j 连接、模型加载状态（唯一无需登录的接口）"
)
async def health():
    """
    Health check endpoint

    Returns status of all system components.
    """
    from src.api.main import app_state

    # Determine overall status
    if app_state.agent_system_ready and app_state.vector_index_ready and app_state.neo4j_ready:
        status = "healthy"
    elif app_state.agent_system_ready:
        status = "degraded"
    else:
        status = "unhealthy"

    return HealthResponse(
        status=status,
        neo4j_connected=app_state.neo4j_ready,
        vector_index_loaded=app_state.vector_index_ready,
        agent_system_ready=app_state.agent_system_ready
    )


# ============================================================
# GET /api/stats - System Statistics Endpoint
# ============================================================

@router.get(
    "/stats",
    response_model=StatsResponse,
    tags=["系统"],
    summary="系统统计",
    description="返回知识图谱、向量索引、API 统计信息"
)
async def stats(user: dict = Depends(get_current_user)):
    """
    System statistics endpoint

    Returns knowledge graph stats, vector index stats, and API stats.
    """
    from src.api.main import app_state

    # Knowledge graph stats
    kg_stats = {}
    try:
        from src.knowledge_graph.graph_queries import GraphQueries
        queries = GraphQueries()
        kg_data = queries.get_statistics()
        kg_stats = {
            "total_nodes": kg_data.get("total_nodes", 0),
            "total_relations": kg_data.get("total_relations", 0),
            "nodes_by_type": kg_data.get("nodes", {}),
            "relations_by_type": kg_data.get("relations", {}),
            "density": round(kg_data.get("density", 0), 2)
        }
        queries.close()
    except Exception as e:
        kg_stats = {"error": str(e)}

    # Vector index stats
    vi_stats = {}
    try:
        from config.paths import DATA_INDEXES_DIR

        index_file = DATA_INDEXES_DIR / "faiss.index"
        entities_file = DATA_INDEXES_DIR / "entities.json"

        if index_file.exists():
            import faiss
            index = faiss.read_index(str(index_file))
            vi_stats["total_vectors"] = index.ntotal
            vi_stats["dimension"] = index.d

        if entities_file.exists():
            with open(entities_file, encoding="utf-8") as f:
                entities = json.load(f)
            vi_stats["total_entities"] = len(entities)

    except Exception as e:
        vi_stats = {"error": str(e)}

    # API stats
    api_stats = {
        "version": settings.app_version,
        "total_requests": app_state.total_requests,
        "total_errors": app_state.total_errors,
        "concurrency_limit": settings.max_concurrency,
        "request_timeout": REQUEST_TIMEOUT
    }

    return StatsResponse(
        knowledge_graph=kg_stats,
        vector_index=vi_stats,
        api=api_stats
    )


# ============================================================
# 对话历史管理（Redis 热缓存 + MySQL 冷存储，§12）
# 历史接口用 def 同步路由：FastAPI 自动放入线程池，匹配
# store 层"MySQL 每请求新开连接、用完即关"的连接管理约定。
# 越权读取 / 删除他人会话一律 404（§6.4）。
# ============================================================

@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    tags=["对话历史"],
    summary="对话历史列表",
    description="按 updated_at 倒序分页列出本人对话（LIMIT ≤ 200），读 MySQL"
)
def list_conversations(limit: int = 50, user: dict = Depends(get_current_user)):
    """对话历史列表（左侧历史栏数据源，仅本人会话）"""
    return ConversationListResponse(
        conversations=history_store.list_conversations(
            user["user_id"], limit=min(max(limit, 1), 200)
        )
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    tags=["对话历史"],
    summary="恢复单个对话",
    description="返回该对话全部消息（时间正序），读 MySQL；非本人会话返回 404"
)
def get_conversation(conversation_id: str, user: dict = Depends(get_current_user)):
    """恢复单个对话（单击历史栏项时调用）；越权访问按不存在处理 → 404"""
    if not history_store.conversation_exists(conversation_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetailResponse(
        conversation_id=conversation_id,
        messages=history_store.get_messages(conversation_id),
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=DeleteConversationResponse,
    tags=["对话历史"],
    summary="删除对话",
    description="同步清空 MySQL 两表 + 对应 Redis key；非本人会话返回 404"
)
def delete_conversation(conversation_id: str, user: dict = Depends(get_current_user)):
    """删除对话（MySQL 与 Redis 同步清空）；越权删除按不存在处理 → 404"""
    if not history_store.conversation_exists(conversation_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    history_store.delete_conversation(conversation_id, user["user_id"])
    return DeleteConversationResponse(deleted=conversation_id)
