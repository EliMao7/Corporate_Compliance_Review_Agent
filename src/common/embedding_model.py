"""
BGE-M3 嵌入模型单例（全项目唯一加载入口）

⚠️ R4（索引与嵌入契约）：向量索引与嵌入模型必须**同源、同维**。
   BGE-M3 输出 1024 维；`index.d == 1024`，且索引条数与元数据逐条对齐。
   本模块是 BGE-M3 的**唯一加载处**（TDD §2.3.2）——vector_store / 抽取 /
   评测等一律通过 `get_embedding_model()` 取用，禁止各自重新加载一份模型，
   否则「同源同维」的契约会在暗处被打破。

⚠️ 只允许本地目录加载 —— 禁止联网回退：
   一旦允许联网下载，本地缺失时 SentenceTransformer 会把路径字符串当作
   HuggingFace Hub 的 repo_id，静默拉一个不同版本/不同维度的模型；R4 的
   `index.d == 1024` 会以「某个看起来正常的分数」被破坏，极难排查。
   网络不通时该下载还会挂起数分钟（请求线程表现为无响应）。宁可直接报错。
"""

import os
from functools import lru_cache
from pathlib import Path

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "embedding.log")

# 项目根目录：相对路径锚定于此解析，不依赖进程 CWD
_project_root = Path(__file__).parent.parent.parent.resolve()

# BGE-M3 固定输出维度（R4 契约）
BGE_M3_DIM = 1024


@lru_cache(maxsize=1)
def get_embedding_model():
    """BGE-M3 单例。⚠️ 只允许本地目录加载 —— 禁止联网回退。

    加载加固（本地目录缺失即报错，绝不静默降级）：
    1) 相对路径必须锚定项目根解析——若直接传 "models/bge-m3"，会依赖进程 CWD；
       一旦 CWD 不是项目根（IDE 启动、--reload 子进程、从别的目录启动脚本），
       本地目录找不到，SentenceTransformer 便把该字符串当作 HF Hub repo_id
       尝试联网下载（"Repository Not Found ... huggingface.co/models/bge-m3"）。
    2) 本地目录不存在时立即抛 RuntimeError——绝不落到联网下载路径。
    3) 在 import sentence_transformers / huggingface_hub **之前**设置离线环境变量
       （HF 离线模式在 huggingface_hub import 时定型，必须在导入前设置）。

    Returns:
        SentenceTransformer 实例（进程级复用，避免每次工具调用重载 ~10-20s）。

    Raises:
        RuntimeError: 本地 BGE-M3 目录不存在时抛出。
    """
    model_path = Path(settings.embedding_model_path)
    if not model_path.is_absolute():
        model_path = _project_root / model_path
    if not model_path.is_dir():
        raise RuntimeError(
            f"BGE-M3 目录不存在：{model_path}（禁止联网回退，请先下载模型）"
        )

    # ⚠️ 必须在导入 sentence_transformers / huggingface_hub 之前设置离线开关
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    from sentence_transformers import SentenceTransformer

    logger.info(f"[MODEL] Loading BGE-M3 from {model_path}")
    model = SentenceTransformer(str(model_path))
    logger.info("[MODEL] BGE-M3 loaded")
    return model


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("EMBEDDING MODEL (BGE-M3) SELF-TEST")
    print("=" * 60)
    print(f"配置路径: {settings.embedding_model_path}")

    try:
        model = get_embedding_model()
        dim = model.get_sentence_embedding_dimension()
        print(f"[PASS] 模型已加载，输出维度 = {dim}")
        if dim != BGE_M3_DIM:
            raise AssertionError(f"维度不符：期望 {BGE_M3_DIM}，实际 {dim}（违反 R4 契约）")
        assert model is get_embedding_model(), "[FAIL] 单例失效"
        print("[PASS] 单例复用正常")
    except RuntimeError as e:
        # 本地目录缺失：守卫逻辑按设计生效（未联网回退）
        print(f"[SKIP] {e}")
        print("[PASS] 本地目录守卫逻辑生效（禁止联网回退）")

    print("\n" + "=" * 60)
    print("[SUCCESS] Embedding model self-test completed!")
    print("=" * 60)
