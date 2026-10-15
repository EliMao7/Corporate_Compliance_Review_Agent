"""
语义检索接口（FAISS 语义召回 + Neo4j 精确匹配 的合并检索器）

对外提供 `VectorRetriever`：把用户的口语化问法映射到知识图谱的规范实体，
是「合规排查 / 措施建议 / 法规知识」各分支构建检索上下文的数据入口。

两档阈值（本项目关键设计，见 TDD §5.4）：
  · search()        召回档 0.5  —— 宁多勿漏（召回漏掉的，后面再也补不回来）
  · link_entities() 链接档 0.85 —— 宁缺毋滥（链接连错，会把错误实体送进图谱查询）
两个环节的风险方向相反，故阈值分设，且都来自 config.settings。
"""

import sys
import json
import threading
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import numpy as np
import faiss

from config.settings import settings
from config.paths import DATA_INDEXES_DIR
from src.knowledge_graph.schema import NodeType
from src.common.logger import setup_logger
from src.common.embedding_model import get_embedding_model, BGE_M3_DIM

logger = setup_logger(__name__, "vector_search.log")


# ============================================================
# 阈值（唯一来源：config.settings）
# 召回阈值 retrieval_recall_threshold（0.5，宁多勿漏）
# ============================================================

try:
    _DEFAULT_RECALL_THRESHOLD = float(settings.retrieval_recall_threshold)
except AttributeError:  # 配置层尚未同步时的过渡兜底
    _DEFAULT_RECALL_THRESHOLD = 0.5


# ============================================================
# Paths
# ============================================================

FAISS_INDEX_FILE = DATA_INDEXES_DIR / "faiss.index"
ENTITIES_MAPPING_FILE = DATA_INDEXES_DIR / "entities.json"


# ============================================================
# Vector Retriever
# ============================================================

class VectorRetriever:
    """
    向量检索器（混合检索：FAISS 语义召回 + Neo4j 精确匹配）

    用法：
        >>> retriever = VectorRetriever()
        >>> results = retriever.search("无依据增设行政许可")
        >>> for r in results:
        ...     print(f"{r['name']} ({r['type']}): {r['score']:.4f}")
    """

    def __init__(
        self,
        index_path: Path = FAISS_INDEX_FILE,
        entities_path: Path = ENTITIES_MAPPING_FILE,
        score_threshold: float = _DEFAULT_RECALL_THRESHOLD,
    ):
        """
        初始化检索器

        Args:
            index_path: FAISS 索引文件路径
            entities_path: 实体元数据 JSON 文件路径
            score_threshold: 召回相似度下限（默认取 settings.retrieval_recall_threshold，0.5）
        """
        self.index_path = index_path
        self.entities_path = entities_path
        self.score_threshold = score_threshold

        self.index = None
        self.entities = []

        # 加载索引与实体元数据
        self._load_index()

    def _load_index(self):
        """加载 FAISS 索引与实体元数据，并校验 R4 契约。"""
        # 加载 FAISS 索引
        if self.index_path.exists():
            self.index = faiss.read_index(str(self.index_path))
            logger.info(f"[LOAD] FAISS index loaded: {self.index.ntotal} vectors")
        else:
            logger.error(f"[LOAD] FAISS index not found: {self.index_path}")
            raise FileNotFoundError(f"FAISS index not found: {self.index_path}")

        # 加载实体元数据
        if self.entities_path.exists():
            with open(self.entities_path, "r", encoding="utf-8") as f:
                self.entities = json.load(f)
            logger.info(f"[LOAD] Entity mapping loaded: {len(self.entities)} entities")
        else:
            logger.error(f"[LOAD] Entity mapping not found: {self.entities_path}")
            raise FileNotFoundError(f"Entity mapping not found: {self.entities_path}")

        # R4 契约：索引维度 == BGE-M3 维度（1024）
        if getattr(self.index, "d", BGE_M3_DIM) != BGE_M3_DIM:
            raise ValueError(
                f"R4 契约违例：FAISS 索引维度 {self.index.d} != {BGE_M3_DIM}"
                f"（BGE-M3 固定 1024 维，索引与嵌入必须同源同维），请清空重建索引。"
            )
        # R4 契约：索引条数 == 实体元数据条数（逐条对齐，不允许错位）
        if self.index.ntotal != len(self.entities):
            raise ValueError(
                f"R4 契约违例：FAISS 索引条数 {self.index.ntotal} != "
                f"entities.json 条数 {len(self.entities)}；索引与元数据必须逐条对齐，"
                f"请先清空再重建：python scripts/build_index.py"
            )

    def _get_embedding_model(self):
        """取进程级共享的 BGE-M3（唯一加载入口见 src.common.embedding_model）。

        只允许本地目录加载，禁止任何 HF Hub 网络回退——保证索引与嵌入同源同维（R4）。
        """
        return get_embedding_model()

    def warmup(self) -> "VectorRetriever":
        """启动预热：在主线程提前加载嵌入模型，并跑通一次 encode + FAISS 检索。

        背景：若在 uvicorn 请求工作线程中**首次** `import sentence_transformers`
        （连带首次加载 torch / pyarrow 的原生 DLL），在完整服务进程上下文中会触发
        原生访问违例（故障模块 pyarrow 的 arrow.dll，异常码 c0000005），整个进程被
        操作系统直接杀掉——没有 Python traceback，try/except 无法捕获。
        在服务启动前（uvicorn.run 之前）于主线程调用本方法即可确定性规避（已实测验证）。

        附带收益：首条知识类请求不再承担 ~10-20s 的模型加载延迟。

        Returns:
            self（便于链式调用）
        """
        model = self._get_embedding_model()
        vec = model.encode(["启动预热"], normalize_embeddings=True).astype("float32")
        if self.index is not None and self.index.ntotal > 0:
            self.index.search(vec, 1)  # 跑通一次 FAISS 检索，完成原生层初始化
        logger.info(f"[WARMUP] embedding model ready (device={getattr(model, 'device', 'unknown')})")
        return self

    def search(
        self,
        query: str,
        top_k: int = 5,
        use_graph: bool = True,
    ) -> list[dict]:
        """
        混合检索：FAISS 语义召回 + Neo4j 精确匹配

        Args:
            query: 检索文本
            top_k: 返回结果数
            use_graph: 是否叠加 Neo4j 图谱精确匹配

        Returns:
            结果 dict 列表，每项含 name / type / score / source
        """
        results = []

        # 1. FAISS 语义召回
        vector_results = self._vector_search(query, top_k * 2)  # 多召回一些供合并
        results.extend(vector_results)

        # 2. Neo4j 图谱精确匹配
        if use_graph:
            graph_results = self._graph_search(query, top_k)
            results.extend(graph_results)

        # 3. 合并去重
        merged = self._merge_results(results, top_k)

        return merged

    def _vector_search(self, query: str, top_k: int) -> list[dict]:
        """FAISS 语义召回"""
        if self.index is None:
            return []

        # 编码查询
        model = self._get_embedding_model()
        query_vec = model.encode([query], normalize_embeddings=True).astype("float32")

        # 检索
        distances, indices = self.index.search(query_vec, top_k)

        results = []
        for idx, score in zip(indices[0], distances[0]):
            if idx < len(self.entities) and score >= self.score_threshold:
                entity = self.entities[idx].copy()
                results.append({
                    "name": entity.get("name", ""),
                    "type": entity.get("type", ""),
                    "score": float(score),
                    "source": "vector",
                    "data": entity.get("data", {}),
                })

        return results

    def _graph_search(self, query: str, top_k: int) -> list[dict]:
        """Neo4j 图谱精确匹配（实体名精确命中事项 / 风险点）"""
        try:
            from src.knowledge_graph.graph_queries import GraphQueries
            queries = GraphQueries()

            results = []

            # 事项精确匹配
            matter_info = queries.get_matter_info(query)
            if matter_info:
                results.append({
                    "name": matter_info["name"],
                    "type": NodeType.MATTER.value,
                    "score": 1.0,
                    "source": "graph",
                    "data": matter_info,
                })

            # 风险点匹配 → 可能牵出的审查事项
            risk_matters = queries.get_risk_matters(query)
            for matter in risk_matters[:3]:
                results.append({
                    "name": matter,
                    "type": NodeType.MATTER.value,
                    "score": 0.9,
                    "source": "graph",
                    "data": {"matched_risk_point": query},
                })

            queries.close()
            return results

        except Exception as e:
            logger.warning(f"[GRAPH] Graph search failed: {e}")
            return []

    def _merge_results(self, results: list[dict], top_k: int) -> list[dict]:
        """合并去重（同名保留高分；同分优先图谱来源）"""
        by_name = {}
        for r in results:
            name = r["name"]
            if name not in by_name:
                by_name[name] = r
            else:
                # 保留更高分
                if r["score"] > by_name[name]["score"]:
                    by_name[name] = r
                # 同分优先图谱来源（精确匹配更可信）
                elif r["score"] == by_name[name]["score"] and r["source"] == "graph":
                    by_name[name] = r

        # 按分数排序取 top_k
        merged = sorted(by_name.values(), key=lambda x: x["score"], reverse=True)
        return merged[:top_k]

    def search_matters(self, query: str, top_k: int = 5) -> list[dict]:
        """只检索「审查事项」"""
        results = self.search(query, top_k * 2, use_graph=True)
        return [r for r in results if r["type"] == NodeType.MATTER.value][:top_k]

    def search_risks(self, query: str, top_k: int = 5) -> list[dict]:
        """只检索「风险点」"""
        results = self.search(query, top_k * 2, use_graph=False)
        return [r for r in results if r["type"] == NodeType.RISK_POINT.value][:top_k]

    def search_measures(self, query: str, top_k: int = 5) -> list[dict]:
        """只检索「合规措施」"""
        results = self.search(query, top_k * 2, use_graph=False)
        return [r for r in results if r["type"] == NodeType.MEASURE.value][:top_k]

    # ============================================================
    # Entity Linking
    # ============================================================

    @staticmethod
    def _distance_to_similarity(distances: np.ndarray, metric_type: int) -> np.ndarray:
        """把 FAISS 返回的距离换算为"越大越相似"的相似度分数。

        - METRIC_INNER_PRODUCT（本项目索引类型，向量已归一化）：内积≈cosine，原值返回
        - METRIC_L2（向量已归一化时）：cos = 1 - L2²/2
        - 其他度量：退化为 -dist，保持"越大越相似"语义
        """
        if metric_type == faiss.METRIC_INNER_PRODUCT:
            return distances
        if metric_type == faiss.METRIC_L2:
            return 1.0 - distances / 2.0
        return -distances

    def link_entities(
        self,
        terms: list[str],
        top_k: Optional[int] = None,
        threshold: Optional[float] = None,
    ) -> list[dict]:
        """
        实体链接：把用户侧的口语说法高精度地链接到知识图谱中的规范实体。

        与 search() 的定位区别：
        - search() 面向"召回"（阈值 0.5），结果用于构建检索上下文
        - link_entities() 面向"精确"（阈值默认 0.85），只输出高置信链接，
          用于 state 记录、Text2Cypher 实体提示（entity hints）与前端展示

        合规场景示例（口语 → 规范实体）：
        - 用户说"罚款"   → 图谱实体"行政处罚·罚款"
        - 用户说"拍板"   → 图谱实体"审批权限"

        Args:
            terms: 输入词列表（如 ["罚款", "拍板"]）
            top_k: 每个词考察的候选数（默认 settings.entity_link_top_k）
            threshold: 相似度阈值（默认 settings.entity_link_threshold，0.85）

        Returns:
            链接结果 dict 列表：
            [{"input_entity", "matched_entity", "type", "similarity"}]
        """
        top_k = top_k if top_k is not None else settings.entity_link_top_k
        threshold = threshold if threshold is not None else settings.entity_link_threshold

        terms = [str(t).strip() for t in (terms or []) if t and str(t).strip()]
        if not terms or self.index is None or self.index.ntotal == 0:
            return []

        try:
            model = self._get_embedding_model()
            vectors = model.encode(terms, normalize_embeddings=True).astype("float32")
            distances, indices = self.index.search(vectors, top_k)

            metric_type = getattr(self.index, "metric_type", faiss.METRIC_INNER_PRODUCT)
            sims = self._distance_to_similarity(np.asarray(distances), metric_type)

            linked = []
            seen = set()
            for term, idx_row, sim_row in zip(terms, indices, sims):
                for idx, sim in zip(idx_row, sim_row):
                    if idx < 0 or idx >= len(self.entities):
                        continue
                    if float(sim) < threshold:
                        continue
                    entity = self.entities[idx]
                    name = entity.get("name", "")
                    if not name or (term, name) in seen:
                        continue
                    seen.add((term, name))
                    linked.append({
                        "input_entity": term,
                        "matched_entity": name,
                        "type": entity.get("type", ""),
                        "similarity": round(float(sim), 4),
                    })

            if linked:
                logger.info(
                    f"[LINK] {len(terms)} terms -> {len(linked)} links "
                    f"(threshold={threshold}): {[lk['matched_entity'] for lk in linked]}"
                )
            return linked
        except Exception as e:
            logger.warning(f"[LINK] entity linking failed: {e}")
            return []


# ============================================================
# 进程级共享检索器（单例）
# ============================================================
# 此前每个请求都 new 一个 VectorRetriever：每次重读 FAISS 索引、每次重新加载
# BGE-M3（约 10-20s/次），且「工作线程内首次 import sentence_transformers」
# 曾引发原生崩溃（详见 VectorRetriever.warmup 文档）。改为进程级共享后：
# 索引与模型只加载一次，服务路径上不再出现重型原生库的首次加载。

_shared_retriever: Optional[VectorRetriever] = None
_shared_lock = threading.Lock()


def get_shared_retriever() -> VectorRetriever:
    """返回进程级共享的 VectorRetriever（首次调用时创建并完成预热）。

    线程安全；并发首次调用下最坏只会多加载一次模型，不会出错。
    """
    global _shared_retriever
    if _shared_retriever is None:
        with _shared_lock:
            if _shared_retriever is None:
                _shared_retriever = VectorRetriever().warmup()
    return _shared_retriever


def preload_embedding_stack() -> VectorRetriever:
    """进程启动阶段调用（uvicorn.run 之前、主线程）：预加载向量检索栈。

    创建并预热进程级共享检索器；背景详见 `VectorRetriever.warmup` 文档
    （修复「首条知识类提问导致整个服务进程原生崩溃」的 bug）。
    失败时向上抛出异常，由调用方决定降级策略。
    """
    return get_shared_retriever()


# ============================================================
# Main: Self-test
# ============================================================

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("VECTOR RETRIEVER TEST")
    print("=" * 60)

    # Initialize retriever
    print("\n[INIT] Loading VectorRetriever...")
    retriever = VectorRetriever()
    print("[INIT] VectorRetriever loaded")

    # Test queries
    test_queries = [
        "无依据增设行政许可",
        "供应商管理办法要查哪些法",
        "自行设定罚款数额",
    ]

    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"Query: \"{query}\"")
        print("=" * 60)

        results = retriever.search(query, top_k=5)

        if results:
            print("Results:")
            for i, r in enumerate(results, 1):
                print(f"  {i}. [{r['type']}] {r['name']} (score: {r['score']:.4f}, source: {r['source']})")
        else:
            print("  No results found")

    # Entity linking test
    print("\n" + "=" * 60)
    print("ENTITY LINKING TEST")
    print("=" * 60)
    link_terms = ["罚款", "拍板", "许可"]
    links = retriever.link_entities(link_terms)
    if links:
        for lk in links:
            print(f"  {lk['input_entity']} -> {lk['matched_entity']} "
                  f"({lk['type']}, sim={lk['similarity']:.4f})")
    else:
        print("  No links above threshold")

    print("\n" + "=" * 60)
    print("[SUCCESS] Vector retriever tests completed!")
    print("=" * 60)
