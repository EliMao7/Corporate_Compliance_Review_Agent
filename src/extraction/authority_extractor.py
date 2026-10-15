"""
Authority Entity Extractor
Uses LLM to extract structured competent-authority information from raw text.
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
from src.extraction.schemas import AuthorityEntity

logger = setup_logger(__name__, "extraction.log")


# Standard authority name mapping
AUTHORITY_MAPPING = {
    "市监局": "市场监督管理部门",
    "市场监管局": "市场监督管理部门",
    "工商局": "市场监督管理部门",
    "质监局": "市场监督管理部门",
    "发改委": "发展改革部门",
    "发改局": "发展改革部门",
    "人社局": "人力资源和社会保障部门",
    "人事局": "人力资源和社会保障部门",
    "环保局": "生态环境部门",
    "生态环境局": "生态环境部门",
    "住建局": "住房和城乡建设部门",
    "财政局": "财政部门",
    "卫健委": "卫生健康部门",
}


class AuthorityExtractionOutput(BaseModel):
    """归口部门抽取输出模型"""
    authorities: list[dict] = Field(
        default_factory=list,
        description="归口部门列表，每项包含 name, description, level"
    )


class AuthorityExtractor:
    """
    归口部门实体抽取器

    从原始合规文本中抽取归口部门信息。

    Usage:
        >>> extractor = AuthorityExtractor()
        >>> authorities = extractor.extract("该事项由市场监督管理部门主责...")
        >>> for a in authorities:
        ...     print(a.name)
    """

    def __init__(self, temperature: float = 0.0):
        """
        初始化抽取器

        Args:
            temperature: LLM 温度参数（抽取须用 0）
        """
        self.llm = get_llm(temperature=temperature)

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个专业的法规/合规信息抽取专家。请从以下文本中提取归口部门信息。

要求：
1. 部门名称使用规范名称（如"市场监督管理部门"而非"工商局"）
2. 如果文本中使用非规范名称，请转换为规范名称
3. 层级取值为：国家级/省级/市级
4. 只抽取文本中明确出现的部门，不确定的不抽（宁缺毋滥）
5. 严格按照 JSON 格式输出"""),
            ("user", """请从以下文本中提取归口部门信息：

{text}

请以 JSON 格式输出：
{{
    "authorities": [
        {{
            "name": "部门规范名称",
            "description": "部门职责描述（可选）",
            "level": "国家级/省级/市级（如知道）"
        }}
    ]
}}""")
        ])

        self.chain = self.prompt | self.llm.with_structured_output(
            AuthorityExtractionOutput,
            method="json_mode"
        )

    @retry_on_error(max_retries=3, delay=1.0)
    def extract(self, text: str) -> list[AuthorityEntity]:
        """
        从文本中抽取归口部门实体

        Args:
            text: 原始合规文本

        Returns:
            list[AuthorityEntity]: 归口部门实体列表
        """
        logger.info(f"[EXTRACT] Starting authority extraction, text length: {len(text)}")

        try:
            result = self.chain.invoke({"text": text})

            authorities = []
            for a in result.authorities:
                name = self._standardize_name(a.get("name", ""))
                authority = AuthorityEntity(
                    name=name,
                    description=a.get("description"),
                    level=a.get("level")
                )
                authorities.append(authority)

            logger.info(f"[EXTRACT] Extracted {len(authorities)} authorities")
            return authorities

        except ValidationError as e:
            logger.error(f"[EXTRACT] Validation error: {e}")
            raise
        except Exception as e:
            logger.error(f"[EXTRACT] Extraction failed: {e}")
            raise

    def _standardize_name(self, name: str) -> str:
        """
        标准化部门名称

        Args:
            name: 原始部门名称

        Returns:
            标准化后的部门名称
        """
        if not name:
            return ""

        name = name.strip()

        # Check exact match
        if name in AUTHORITY_MAPPING:
            return AUTHORITY_MAPPING[name]

        # Check partial match
        for key, value in AUTHORITY_MAPPING.items():
            if key in name:
                return value

        return name

    def extract_from_list(self, authority_names: list[str]) -> list[AuthorityEntity]:
        """
        从部门名称列表创建 AuthorityEntity 对象

        Args:
            authority_names: 部门名称列表

        Returns:
            list[AuthorityEntity]
        """
        return [
            AuthorityEntity(name=self._standardize_name(name))
            for name in authority_names
            if name
        ]


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Authority Extractor Test")
    print("=" * 60)

    # Test 1: Create extractor
    print("\n[TEST 1] Create AuthorityExtractor")
    extractor = AuthorityExtractor(temperature=0.0)
    print("[PASS] AuthorityExtractor created")

    # Test 2: Extract from list with standardization
    print("\n[TEST 2] Extract from list with name standardization")
    authorities = extractor.extract_from_list(["市监局", "发改委", "财政局"])
    print(f"[PASS] Extracted {len(authorities)} authorities:")
    for a in authorities:
        print(f"  - {a.name}")

    # Test 3: Name standardization
    print("\n[TEST 3] Name standardization")
    assert extractor._standardize_name("市监局") == "市场监督管理部门"
    assert extractor._standardize_name("发改委") == "发展改革部门"
    assert extractor._standardize_name("财政局") == "财政部门"
    print("[PASS] Name standardization works correctly")

    print("\n" + "=" * 60)
    print("[SUCCESS] Authority extractor tests passed!")
    print("=" * 60)
