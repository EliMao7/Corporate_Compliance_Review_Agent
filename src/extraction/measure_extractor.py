"""
Measure Entity Extractor
Uses LLM to extract structured compliance measure information from raw text.
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
from src.extraction.schemas import MeasureEntity

logger = setup_logger(__name__, "extraction.log")


class MeasureExtractionOutput(BaseModel):
    """合规措施抽取输出模型"""
    measures: list[dict] = Field(
        default_factory=list,
        description="合规措施列表，每项包含 name, scenario, key_points, contraindications"
    )


class MeasureExtractor:
    """
    合规措施实体抽取器

    从原始合规文本中抽取合规措施信息。

    Usage:
        >>> extractor = MeasureExtractor()
        >>> measures = extractor.extract("建议改为援引上位法既有条款，并补充公平竞争审查...")
        >>> for m in measures:
        ...     print(f"{m.name}: {m.scenario}")
    """

    def __init__(self, temperature: float = 0.0):
        """
        初始化抽取器

        Args:
            temperature: LLM 温度参数（抽取须用 0）
        """
        self.llm = get_llm(temperature=temperature)

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个专业的法规/合规信息抽取专家。请从以下文本中提取合规措施信息。

要求：
1. 措施名称使用规范表述（如"改为援引上位法既有条款"而非"修改一下"）
2. 措施应可落地执行，说明适用场景
3. 落地要点与不适用情形要完整列出
4. 只抽取文本中明确出现的措施，不确定的不抽（宁缺毋滥）
5. 严格按照 JSON 格式输出"""),
            ("user", """请从以下文本中提取合规措施信息：

{text}

请以 JSON 格式输出：
{{
    "measures": [
        {{
            "name": "措施名称",
            "scenario": "适用场景",
            "key_points": ["落地要点1", "落地要点2"],
            "contraindications": ["不适用情形1"]
        }}
    ]
}}""")
        ])

        self.chain = self.prompt | self.llm.with_structured_output(
            MeasureExtractionOutput,
            method="json_mode"
        )

    @retry_on_error(max_retries=3, delay=1.0)
    def extract(self, text: str) -> list[MeasureEntity]:
        """
        从文本中抽取合规措施实体

        Args:
            text: 原始合规文本

        Returns:
            list[MeasureEntity]: 合规措施实体列表
        """
        logger.info(f"[EXTRACT] Starting measure extraction, text length: {len(text)}")

        try:
            result = self.chain.invoke({"text": text})

            measures = []
            for m in result.measures:
                measure = MeasureEntity(
                    name=m.get("name", ""),
                    scenario=m.get("scenario"),
                    key_points=m.get("key_points", []),
                    contraindications=m.get("contraindications", [])
                )
                measures.append(measure)

            logger.info(f"[EXTRACT] Extracted {len(measures)} measures")
            return measures

        except ValidationError as e:
            logger.error(f"[EXTRACT] Validation error: {e}")
            raise
        except Exception as e:
            logger.error(f"[EXTRACT] Extraction failed: {e}")
            raise

    def extract_from_list(self, measure_names: list[str]) -> list[MeasureEntity]:
        """
        从措施名称列表创建 MeasureEntity 对象

        Args:
            measure_names: 措施名称列表

        Returns:
            list[MeasureEntity]
        """
        return [
            MeasureEntity(name=name)
            for name in measure_names
            if name
        ]


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Measure Extractor Test")
    print("=" * 60)

    # Test 1: Create extractor
    print("\n[TEST 1] Create MeasureExtractor")
    extractor = MeasureExtractor(temperature=0.0)
    print("[PASS] MeasureExtractor created")

    # Test 2: Extract from list (no LLM call)
    print("\n[TEST 2] Extract from list")
    measures = extractor.extract_from_list(["改为援引上位法既有条款", "补充公平竞争审查", "删除自设罚则"])
    print(f"[PASS] Extracted {len(measures)} measures:")
    for m in measures:
        print(f"  - {m.name}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Measure extractor tests passed!")
    print("=" * 60)
