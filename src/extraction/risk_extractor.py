"""
RiskPoint Entity Extractor
Uses LLM to extract structured compliance risk-point information from raw text.
"""

import sys
from pathlib import Path

# Ensure project root is in sys.path
_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, ValidationError

from config.settings import settings
from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.common.utils import retry_on_error
from src.extraction.schemas import (
    RiskPointEntity,
    SeverityLevel,
)

logger = setup_logger(__name__, "extraction.log")


class RiskExtractionOutput(BaseModel):
    """风险点抽取输出模型"""
    risks: list[dict] = Field(
        default_factory=list,
        description="风险点列表，每项包含 name, description, severity, legal_basis"
    )


class RiskExtractor:
    """
    风险点实体抽取器

    从原始合规文本中抽取风险点信息。

    Usage:
        >>> extractor = RiskExtractor()
        >>> risks = extractor.extract("该办法拟无依据增设行政许可，存在超权限风险...")
        >>> for r in risks:
        ...     print(f"{r.name}: {r.severity}")
    """

    def __init__(self, temperature: float = 0.0):
        """
        初始化抽取器

        Args:
            temperature: LLM 温度参数（抽取须用 0）
        """
        self.llm = get_llm(temperature=temperature)

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个专业的法规/合规信息抽取专家。请从以下文本中提取风险点信息。

要求：
1. 风险点名称使用规范表述（如"无依据增设许可"、"自行设定罚款数额"）
2. 严重程度根据描述判断：low（轻微）、medium（中等）、high（严重）、redline（红线）
3. 只抽取文本中明确出现的风险点，不确定的不抽（宁缺毋滥）
4. 如果没有明确描述严重程度，默认为 medium
5. 严格按照 JSON 格式输出"""),
            ("user", """请从以下文本中提取风险点信息：

{text}

请以 JSON 格式输出：
{{
    "risks": [
        {{
            "name": "风险点名称",
            "description": "风险点描述（可选）",
            "severity": "low/medium/high/redline",
            "legal_basis": ["涉及的上位法（可选）"]
        }}
    ]
}}""")
        ])

        self.chain = self.prompt | self.llm.with_structured_output(
            RiskExtractionOutput,
            method="json_mode"
        )

    @retry_on_error(max_retries=3, delay=1.0)
    def extract(self, text: str) -> list[RiskPointEntity]:
        """
        从文本中抽取风险点实体

        Args:
            text: 原始合规文本

        Returns:
            list[RiskPointEntity]: 风险点实体列表
        """
        logger.info(f"[EXTRACT] Starting risk extraction, text length: {len(text)}")

        try:
            result = self.chain.invoke({"text": text})

            risks = []
            for r in result.risks:
                severity_str = r.get("severity", "medium")
                try:
                    severity = SeverityLevel(severity_str)
                except ValueError:
                    severity = SeverityLevel.MEDIUM

                risk = RiskPointEntity(
                    name=r.get("name", ""),
                    description=r.get("description"),
                    severity=severity,
                    legal_basis=r.get("legal_basis", [])
                )
                risks.append(risk)

            logger.info(f"[EXTRACT] Extracted {len(risks)} risks")
            return risks

        except ValidationError as e:
            logger.error(f"[EXTRACT] Validation error: {e}")
            raise
        except Exception as e:
            logger.error(f"[EXTRACT] Extraction failed: {e}")
            raise

    def extract_from_list(self, risk_names: list[str]) -> list[RiskPointEntity]:
        """
        从风险点名称列表创建 RiskPointEntity 对象

        Args:
            risk_names: 风险点名称列表

        Returns:
            list[RiskPointEntity]
        """
        return [
            RiskPointEntity(name=name, severity=SeverityLevel.MEDIUM)
            for name in risk_names
            if name
        ]


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Risk Extractor Test")
    print("=" * 60)

    # Test 1: Create extractor
    print("\n[TEST 1] Create RiskExtractor")
    extractor = RiskExtractor(temperature=0.0)
    print("[PASS] RiskExtractor created")

    # Test 2: Extract from list (no LLM call)
    print("\n[TEST 2] Extract from list")
    risks = extractor.extract_from_list(["无依据增设许可", "变相设置审批前置", "自行设定罚款数额", "超越制定权限"])
    print(f"[PASS] Extracted {len(risks)} risks:")
    for r in risks:
        print(f"  - {r.name} ({r.severity})")

    print("\n" + "=" * 60)
    print("[SUCCESS] Risk extractor tests passed!")
    print("=" * 60)
