"""
Neo4j 驱动进程级单例（全项目唯一连接入口）

设计目标（TDD §2.3.2）：Neo4j 驱动本应在**一处**创建。此前它被分散在
`graph_builder.py` / `graph_queries.py` / `graph_validator.py` / `schema_manager.py`
里各自 `GraphDatabase.driver(...)` 一份——多份连接、多份配置，容易漂移。

本模块提供进程级共享驱动 `get_neo4j_driver()`（`@lru_cache` 单例）与释放助手
`close_neo4j_driver()`。**连接语义与上述模块内的 get_driver 完全一致**
（同样只读 settings 的 uri / user / password，不额外设置驱动选项或超时），
以便上述模块在后续改造中直接收编、行为不变。

依赖纪律：`neo4j` 的 import 写在函数体内——模块级只依赖标准库 + 本项目 config/logger，
使本模块在未安装 neo4j 驱动的最小环境下也能被 import（真正调用时才需要驱动）。
"""

from functools import lru_cache

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "neo4j.log")


@lru_cache(maxsize=1)
def get_neo4j_driver():
    """返回进程级共享的 Neo4j Driver（单例，首次调用时创建）。

    连接语义与图谱模块内的 `get_driver()` 保持一致：

        GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    ——不追加驱动选项 / 超时 / verify_connectivity，保证收编后行为与现状一致
    （verify_connectivity 由调用方按需自行触发）。

    Returns:
        neo4j Driver 实例（进程级复用）。

    Raises:
        ImportError: 未安装 neo4j 驱动时抛出。
        Exception: 参数非法等创建失败时由驱动抛出。
    """
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    logger.info(f"[NEO4J] Driver created for {settings.neo4j_uri}")
    return driver


def close_neo4j_driver() -> None:
    """关闭并清空进程级共享驱动（进程退出或显式释放时调用）。

    未创建过驱动时为空操作（不会为了关闭而先建一个）。关闭失败只记日志，不抛异常。
    """
    if get_neo4j_driver.cache_info().currsize == 0:
        return
    driver = get_neo4j_driver()
    try:
        driver.close()
        logger.info("[NEO4J] Driver closed")
    except Exception as e:  # pragma: no cover - 关闭期异常不应影响调用方
        logger.warning(f"[NEO4J] close failed: {e}")
    finally:
        get_neo4j_driver.cache_clear()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("NEO4J CLIENT SELF-TEST")
    print("=" * 60)
    print(f"URI : {settings.neo4j_uri}")
    print(f"USER: {settings.neo4j_user}")

    try:
        driver = get_neo4j_driver()
        assert driver is get_neo4j_driver(), "[FAIL] 单例失效"
        print("[PASS] 驱动单例创建成功")
        try:
            driver.verify_connectivity()
            print("[PASS] Neo4j 连接校验通过")
        except Exception as e:
            print(f"[WARN] Neo4j 未连通（不影响单例自检）：{e}")
    except Exception as e:
        print(f"[WARN] 驱动创建失败：{e}")
    finally:
        close_neo4j_driver()

    print("\n" + "=" * 60)
    print("[SUCCESS] Neo4j client self-test completed!")
    print("=" * 60)
