"""
API Request/Response Models
Pydantic models for API endpoints.

⚠️ R1/R3：本文件是**前后端响应字段契约的唯一主人**（11 个响应字段）。
其余位置（routes.py / 前端）只能引用这些字段，不允许各自另写一份 dict。
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ============================================================
# Request Models
# ============================================================

class ConsultRequest(BaseModel):
    """合规审查请求模型"""
    query: str = Field(..., description="User question", min_length=1, max_length=1000)
    session_id: str = Field(default="default", description="Session ID for tracking")

    class Config:
        json_schema_extra = {
            "example": {
                "query": "我们准备发一份《供应商管理办法》，帮我看下有没有跟法规冲突的地方",
                "session_id": "u_123_s_456"
            }
        }


# ============================================================
# Response Models
# ============================================================

class ConsultResponse(BaseModel):
    """合规审查响应模型（R3 契约：11 个响应字段 + timestamp）"""
    answer: str = Field(..., description="AI 生成的最终回答（Markdown）")
    intent: str = Field(default="", description="识别到的意图类型（review/measure/regulation/redline/general）")
    intent_confidence: float = Field(default=0.0, description="意图识别置信度 (0-1)")
    doc_type: str = Field(default="", description="识别到的文件类型（管理办法/合同/公告…）")
    matters: list[str] = Field(default_factory=list, description="涉及事项")
    risk_signals: list[str] = Field(default_factory=list, description="识别到的高风险信号")
    authorities: list[dict] = Field(default_factory=list, description="归口部门（含角色/依据）")
    measures: list[dict] = Field(default_factory=list, description="合规措施建议")
    disclaimers: list[str] = Field(default_factory=list, description="免责声明（必有）")
    warnings: list[str] = Field(default_factory=list, description="高风险提示")
    linked_entities: list[dict] = Field(
        default_factory=list,
        description="实体链接结果：用户口语词 → 图谱规范实体 "
                    "[{input_entity, matched_entity, type, similarity}]"
    )
    duration_ms: int = Field(default=0, description="Processing time in milliseconds")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class HealthResponse(BaseModel):
    """Health check response model"""
    status: str = Field(..., description="Service status: healthy/degraded/unhealthy")
    neo4j_connected: bool = Field(default=False, description="Neo4j connection status")
    vector_index_loaded: bool = Field(default=False, description="Vector index status")
    agent_system_ready: bool = Field(default=False, description="Agent system status")
    version: str = Field(default="1.0.0")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class StatsResponse(BaseModel):
    """System statistics response model"""
    knowledge_graph: dict = Field(default_factory=dict, description="Knowledge graph stats")
    vector_index: dict = Field(default_factory=dict, description="Vector index stats")
    api: dict = Field(default_factory=dict, description="API stats")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class ErrorResponse(BaseModel):
    """Error response model"""
    error: str = Field(..., description="Error message")
    detail: Optional[str] = Field(default=None, description="Error detail")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# Auth Models（§6.4 用户认证与数据隔离）
# ============================================================

class RegisterRequest(BaseModel):
    """注册请求模型"""
    username: str = Field(..., description="用户名", min_length=3, max_length=64)
    password: str = Field(..., description="密码（服务端只存不可逆哈希，绝不存明文）", min_length=6, max_length=128)


class LoginRequest(BaseModel):
    """登录请求模型"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class TokenResponse(BaseModel):
    """登录/注册成功后返回的无状态令牌"""
    access_token: str = Field(..., description="JWT（HS256）")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(default=0, description="有效期秒数")
    user_id: str = Field(default="", description="用户 ID")
    username: str = Field(default="", description="用户名")


# ============================================================
# Conversation History Models（对话历史管理，§12 企业方案）
# ============================================================

class ConversationItem(BaseModel):
    """Conversation list item"""
    id: str = Field(..., description="对话 ID（= 前端 session_id）")
    title: str = Field(default="", description="对话标题（首问截断）")
    created_at: str = Field(default="", description="创建时间")
    updated_at: str = Field(default="", description="最后更新时间")


class ConversationListResponse(BaseModel):
    """Conversation list response"""
    conversations: list[ConversationItem] = Field(default_factory=list, description="按 updated_at 倒序（仅本人会话）")


class MessageItem(BaseModel):
    """Single message of a conversation"""
    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息内容")
    intent: str = Field(default="", description="意图类型")
    created_at: str = Field(default="", description="写入时间")


class ConversationDetailResponse(BaseModel):
    """Conversation detail response"""
    conversation_id: str = Field(..., description="对话 ID")
    messages: list[MessageItem] = Field(default_factory=list, description="按时间正序")


class DeleteConversationResponse(BaseModel):
    """Delete conversation response"""
    deleted: str = Field(..., description="被删除的对话 ID")
