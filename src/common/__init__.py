"""
Common utilities package（公共模块）
提供日志、LLM 客户端、嵌入模型单例、Neo4j 驱动单例与通用助手函数。
"""

from .logger import setup_logger
from .llm import get_llm, get_extract_llm
from .embedding_model import get_embedding_model
from .neo4j_client import get_neo4j_driver, close_neo4j_driver
from .utils import clean_text, retry_on_error, timer

__all__ = [
    "setup_logger",
    "get_llm",
    "get_extract_llm",
    "get_embedding_model",
    "get_neo4j_driver",
    "close_neo4j_driver",
    "clean_text",
    "retry_on_error",
    "timer",
]
