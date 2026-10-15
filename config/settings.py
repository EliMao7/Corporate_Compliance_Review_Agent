"""
Global Configuration Module
Reads configuration from .env file using pydantic_settings

企业合规审查 Agent 系统的全局配置：所有模块的地基，配置只在一处定义、一处读取。
"""

from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 锚定在项目根目录查找，不依赖运行时工作目录
# （从 scripts/、IDE 或其他 CWD 启动时都能读到同一份配置）
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.

    All settings can be overridden via environment variables or .env file.
    优先级：系统环境变量 > .env 文件 > 字段默认值。
    生产部署建议用环境变量/云密钥服务注入，不在服务器上放置 .env 文件。
    """

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== LLM Configuration =====
    model_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="LLM API base URL (DashScope compatible with OpenAI)"
    )
    model_api_key: str = Field(
        default="",
        description="LLM API key (DashScope API Key)"
    )
    model_name: str = Field(
        default="qwen-plus",
        description="LLM model name"
    )
    llm_enable_thinking: bool = Field(
        default=False,
        description="是否启用 qwen3 系列混合思考模型的 reasoning(思考)模式。"
                    "本系统多数 LLM 调用为意图分类/事项识别/归口与措施择优等结构化小任务，"
                    "默认关闭思考可大幅降低单次延迟(常见 5-20s → 1-3s)与 token 消耗；"
                    "如需复杂合规推理，可在 .env 设为 true。对非 qwen3 模型此参数被服务端忽略，无副作用。"
    )
    llm_max_retries: int = Field(
        default=1,
        description="LLM 调用失败重试次数。延迟敏感路径建议 ≤1，避免慢调用重试导致耗时翻倍。"
    )

    # ===== Extraction LLM Configuration（微调模型，仅用于实体/关系抽取）=====
    # base_url 留空则抽取任务回退到主 LLM，行为与改动前完全一致
    extraction_model_base_url: str = Field(
        default="",
        description="微调模型 vLLM 服务地址（如 http://192.168.122.3:8000/v1）。"
                    "留空=不启用，实体/关系抽取仍走主 LLM"
    )
    extraction_model_api_key: str = Field(
        default="EMPTY",
        description="vLLM 服务不校验密钥，固定填 EMPTY"
    )
    extraction_model_name: str = Field(
        default="qwen2.5-merged",
        description="vLLM --served-model-name 指定的模型名"
    )

    # ===== Neo4j Configuration =====
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        description="Neo4j database connection URI"
    )
    neo4j_user: str = Field(
        default="neo4j",
        description="Neo4j username"
    )
    neo4j_password: str = Field(
        default="12345678",
        description="Neo4j password"
    )

    # ===== Embedding Model Configuration =====
    embedding_model_path: str = Field(
        default="./models/bge-m3",
        description="Local path to BGE-M3 embedding model (relative to project root or absolute)"
    )

    # ===== Conversation History Storage（Redis 热缓存 + MySQL 冷存储）=====
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for session hot cache"
    )
    mysql_host: str = Field(default="localhost", description="MySQL host")
    mysql_port: int = Field(default=3306, description="MySQL port")
    mysql_user: str = Field(default="root", description="MySQL user")
    mysql_password: str = Field(default="123456", description="MySQL password")
    mysql_database: str = Field(default="ccr_history", description="MySQL database for conversation history")

    # ===== Logging Configuration =====
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )

    # ===== Application Configuration =====
    app_name: str = Field(
        default="CorporateComplianceReviewAgent",
        description="Application name"
    )
    app_version: str = Field(
        default="1.0.0",
        description="Application version"
    )
    api_port: int = Field(
        default=8000,
        description="FastAPI server port"
    )

    # ===== Agent Configuration =====
    enable_react_layer: bool = Field(
        default=True,
        description="法规知识分支是否在『强制检索』之上再启用可选 ReAct 多跳层（tool-calling）。"
                    "强制检索是 grounding 底线、必走；ReAct 仅作多跳补充与工具调用演示。关闭则纯强制检索，更快更省。"
    )

    # ===== 检索阈值（两档，方向相反）=====
    retrieval_recall_threshold: float = Field(
        default=0.5,
        description="向量召回相似度阈值（归一化余弦）——召回档：宁多勿漏。"
                    "召回阶段漏掉的实体后面永远补不回来，故阈值偏低、宁可多召回候选，后续再精排/过滤。"
    )
    entity_link_threshold: float = Field(
        default=0.85,
        description="实体链接相似度阈值（归一化余弦）——实体链接档：宁缺毋滥。"
                    "仅 ≥ 阈值 的图谱实体视为成功链接；链接连错会把错误实体送进图谱查询、产出错误依据，"
                    "故阈值偏高、宁可链接不上（走 LLM 兜底）。过高漏链、过低误链，可按 evals 结果调优。"
    )
    entity_link_top_k: int = Field(
        default=3,
        description="实体链接时每个输入词在 FAISS 中考察的候选数"
    )

    # ===== Text2Cypher Configuration =====
    text2cypher_enabled: bool = Field(
        default=True,
        description="是否为 ReAct 层提供 natural_language_graph_query 工具（LLM 生成只读 Cypher 查图谱）。"
                    "需同时开启 enable_react_layer 才生效。"
    )
    text2cypher_max_records: int = Field(
        default=50,
        description="Text2Cypher 单次查询返回的最大记录数（驱动层截断，防全表扫描）"
    )

    # ===== Tool Layer Transport（工具层传输方式）=====
    tool_transport: str = Field(
        default="inproc",
        description="工具层传输方式：inproc=进程内直调 LangChain @tool（默认，零跨进程开销，"
                    "评测与生产问答走这条）；mcp=经独立进程 MCP server 调用（把工具层从"
                    "『业务逻辑+LangChain+本进程』三重耦合中解出来，供跨进程/跨系统复用）。"
                    "两种传输共享同一份实现（tool_layer/tools_impl.py），行为一致。"
    )
    mcp_server_command: str = Field(
        default="",
        description="MCP server 启动可执行文件（tool_transport=mcp 时生效）。"
                    "留空=用当前解释器（sys.executable），保证子进程与主进程同一套依赖；"
                    "显式指定时须确保该解释器已安装 mcp。"
    )
    mcp_server_args: str = Field(
        default="-m,mcp_server.server",
        description="MCP server 启动参数，逗号分隔（逗号而非空格，便于 .env 书写）"
    )
    mcp_server_cwd: str = Field(
        default="",
        description="MCP server 子进程工作目录，留空=项目根（会自动注入 PYTHONPATH）"
    )

    # ===== A2A 对外协作端点（第 11 章）=====
    # 与 MCP 是两条正交链路：MCP 让本系统出去「用工具」，A2A 让外部系统进来「用本系统」。
    a2a_enabled: bool = Field(
        default=True,
        description="是否挂载 A2A 对外协作端点（Agent Card + JSON-RPC /a2a）。"
                    "关闭后主服务行为与改造前完全一致，便于灰度与回滚。"
    )
    a2a_public_url: str = Field(
        default="",
        description="写进 Agent Card 的对外可达地址（如 http://10.0.0.12:8000）。"
                    "留空=按 api_port 推断为 http://127.0.0.1:{api_port}（本机联调用）。"
                    "⚠️ 部署到服务器时必须显式配置，否则外部系统拿到的回调地址是错的。"
    )

    # ===== 服务上限（并发闸门 / 单请求超时）=====
    max_concurrency: int = Field(
        default=5,
        description="并发闸门上限：同时处理的审查请求数（防止并发打满上游 LLM API）。"
    )
    request_timeout: int = Field(
        default=60,
        description="单次审查请求的安全上限秒数（典型轮次 2-8s；真正提速走 SSE 流式）。"
    )

    # ===== 用户认证与数据隔离（第 6.4 节）=====
    auth_enabled: bool = Field(
        default=True,
        description="是否启用 JWT 认证与按用户隔离的会话数据。"
                    "关闭后所有接口不做鉴权（仅用于本地联调/评测，生产环境必须保持开启）。"
    )
    jwt_secret_key: str = Field(
        default="dev-secret-change-me-in-dotenv",
        description="JWT 签名密钥（HS256）。生产环境必须通过 .env / 环境变量注入强随机值，严禁使用默认值。"
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT 签名算法（无状态令牌，默认 HS256）"
    )
    jwt_expire_minutes: int = Field(
        default=1440,
        description="JWT 有效期（分钟），过期即失效，默认 1 天"
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    Using lru_cache ensures we only load settings once.
    """
    return Settings()


# Global settings instance
settings = get_settings()
