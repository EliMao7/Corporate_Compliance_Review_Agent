"""
Configuration module for CorporateComplianceReviewAgent

注意：这里**不**再 `from .settings import settings` 再导出单例 ——
那样会把包属性 `config.settings` 覆盖成 Settings 实例（遮蔽同名子模块），
导致 `import config.settings as s` 拿到的是实例而非模块。
全仓库统一用 `from config.settings import settings` 引用配置单例。
"""

from .paths import (
    PROJECT_ROOT,
    DATA_DIR,
    DATA_RAW_DIR,
    DATA_PROCESSED_DIR,
    DATA_KG_DIR,
    DATA_INDEXES_DIR,
    DATA_REFERENCE_DIR,
    MODELS_DIR,
    LOGS_DIR,
)

__all__ = [
    "PROJECT_ROOT",
    "DATA_DIR",
    "DATA_RAW_DIR",
    "DATA_PROCESSED_DIR",
    "DATA_KG_DIR",
    "DATA_INDEXES_DIR",
    "DATA_REFERENCE_DIR",
    "MODELS_DIR",
    "LOGS_DIR",
]
