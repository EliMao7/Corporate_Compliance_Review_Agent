"""
Vector Store Module（向量检索）
提供基于 FAISS 的语义召回，以及与 Neo4j 图谱精确匹配合并的混合检索。
"""

from .search import VectorRetriever

__all__ = ["VectorRetriever"]
