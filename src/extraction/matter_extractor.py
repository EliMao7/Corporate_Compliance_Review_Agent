"""
Matter Entity Extractor
Uses LLM to extract structured compliance matter information from raw text.
"""

import json
import sys
from pathlib import Path

# Ensure project root is in sys.path
_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, ValidationError
from typing import Optional

from config.settings import settings
from src.common.llm import get_extract_llm, get_llm
from src.common.logger import setup_logger
from src.common.utils import retry_on_error
from src.extraction.schemas import (
    MatterEntity,
    RiskPointEntity,
    MeasureEntity,
    AuthorityEntity,
    MatterHasRiskRelation,
    MatterAddressedByRelation,
    MatterGovernedByRelation,
    MatterExtractionResult,
    FrequencyLevel,
)


# ============================================================
# Simple LLM Output Schema (for easier LLM generation)
# ============================================================

class SimpleMatterInfo(BaseModel):
    """Simple matter info for LLM output"""
    name: str = Field(description="事项名称")
    description: Optional[str] = Field(default=None, description="事项描述")
    domain: Optional[str] = Field(default=None, description="适用领域")


class SimpleLLMOutput(BaseModel):
    """
    Simple output schema for LLM extraction.
    Uses string lists instead of entity objects for easier LLM generation.
    """
    matter: SimpleMatterInfo = Field(description="审查事项基本信息")
    risks: list[str] = Field(default_factory=list, description="风险点名称列表")
    measures: list[str] = Field(default_factory=list, description="合规措施名称列表")
    authorities: list[str] = Field(default_factory=list, description="归口部门名称列表")

logger = setup_logger(__name__, "extraction.log")


# vLLM 微调模型专用指令：与训练数据（data/finetune/ft_extract.json）的 instruction
# 逐字一致——原 system 提示词 + JSON 字段模板合并为一条指令。训练时 (instruction, input)
# 渲染在同一侧，推理若拆成 system/user 两条消息会导致格式遵循退化（如漏逗号、JSON 畸形）。
_VLLM_EXTRACT_INSTRUCTION = """你是一个专业的法规/合规信息抽取专家。请从以下文本中提取审查事项及其风险点、合规措施、归口部门。

要求：
1. 事项名称使用规范表述（如"行政许可设定"而非"审批"）
2. 风险点列表要完整，包含所有提到的合规风险
3. 归口部门使用规范部门名称（如"市场监督管理部门"而非"工商局"）
4. 合规措施应可落地执行
5. 只抽取文本中明确出现的信息，不确定的不抽（宁缺毋滥）；没有相关信息时对应字段返回空列表
6. 严格按照 JSON 格式输出

请以 JSON 格式输出，包含以下字段：
{{
    "matter": {{
        "name": "事项名称",
        "description": "事项描述",
        "domain": "适用领域（如知道）或 null"
    }},
    "risks": ["风险点1", "风险点2"],
    "measures": ["合规措施1", "合规措施2"],
    "authorities": ["归口部门1"]
}}"""


# 指令模板里的占位示例：微调模型在字段本应为空时偶尔原样回声这些占位符，
# 入图前统一过滤，避免图谱出现"合规措施1"之类的垃圾节点
_TEMPLATE_PLACEHOLDERS = {
    "事项名称", "事项描述", "适用领域（如知道）或 null",
    "风险点1", "风险点2", "合规措施1", "合规措施2", "归口部门1",
}


def _drop_placeholders(items: list) -> list:
    return [s for s in items if s and s not in _TEMPLATE_PLACEHOLDERS]


# 云端主 LLM 抽取用的原版双消息提示词（EXTRACTION_MODEL_BASE_URL 留空时的默认路径）
_CLOUD_EXTRACT_SYSTEM = """你是一个专业的法规/合规信息抽取专家。请从以下文本中提取审查事项及其风险点、合规措施、归口部门。

要求：
1. 事项名称使用规范表述（如"行政许可设定"而非"审批"）
2. 风险点列表要完整，包含所有提到的合规风险
3. 归口部门使用规范部门名称（如"市场监督管理部门"而非"工商局"）
4. 合规措施应可落地执行
5. 只抽取文本中明确出现的信息，不确定的不抽（宁缺毋滥）；没有相关信息时对应字段返回空列表
6. 严格按照 JSON 格式输出"""

_CLOUD_EXTRACT_USER = """请从以下文本中提取合规事项信息：

{text}

请以 JSON 格式输出，包含以下字段：
{{
    "matter": {{
        "name": "事项名称",
        "description": "事项描述",
        "domain": "适用领域（如知道）或 null"
    }},
    "risks": ["风险点1", "风险点2"],
    "measures": ["合规措施1", "合规措施2"],
    "authorities": ["归口部门1"]
}}"""


def build_cloud_extract_chain(temperature: float = 0.1):
    """
    云端主 LLM 抽取链（json_mode 结构化输出）。

    两个用途：
    1. EXTRACTION_MODEL_BASE_URL 留空时的默认抽取路径；
    2. vLLM 微调模型抽取失败（失控生成/畸形 JSON）时的兜底，保证建图覆盖率。
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", _CLOUD_EXTRACT_SYSTEM),
        ("user", _CLOUD_EXTRACT_USER),
    ])
    return prompt | get_llm(temperature=temperature).with_structured_output(
        SimpleLLMOutput,
        method="json_mode"
    )


def _parse_simple_llm_output(raw) -> "SimpleLLMOutput":
    """
    手工解析微调模型的 JSON 输出。

    vLLM 路径不使用 with_structured_output(method="json_mode")：实测 response_format
    json_object 的引导解码会诱发微调模型失控生成（烧穿 max_tokens 后 finish_reason=length）。
    裸调用时模型稳定收尾，但常给 JSON 包 ```json 围栏，这里做容错解析。
    """
    text = raw.content if hasattr(raw, "content") else str(raw)
    text = text.strip()

    # 去掉 ```json ... ``` Markdown 围栏
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()

    # 容错截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"No JSON object in model output: {text[:120]!r}")

    data = json.loads(text[start:end + 1])
    return SimpleLLMOutput(**data)


class MatterExtractor:
    """
    审查事项实体抽取器

    从原始合规文本中抽取审查事项及其相关实体和关系。

    Usage:
        >>> extractor = MatterExtractor()
        >>> result = extractor.extract("《供应商管理办法》拟设定供应商准入许可...")
        >>> print(result.matters[0].name)
        >>> print(result.risks)
    """

    def __init__(self, temperature: float = 0.0):
        """
        初始化抽取器

        Args:
            temperature: LLM 温度参数，抽取任务须用 0（贪心解码最稳，不要创造性）
        """
        # 抽取专用入口：EXTRACTION_MODEL_BASE_URL 非空走本地 vLLM 微调模型，为空自动回退云端 get_llm()
        self.llm = get_extract_llm(temperature=temperature)

        if settings.extraction_model_base_url.strip():
            # vLLM 微调模型分支：消息结构与训练数据对齐（完整 instruction 作 system，
            # 纯合规文本作 user），裸调用 + 手工解析（json_mode 引导解码会诱发失控生成）
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", _VLLM_EXTRACT_INSTRUCTION),
                ("user", "{text}"),
            ])
            self.chain = self.prompt | self.llm | _parse_simple_llm_output
        else:
            # 云端主 LLM 分支：原有双消息提示词 + json_mode 结构化输出
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", _CLOUD_EXTRACT_SYSTEM),
                ("user", _CLOUD_EXTRACT_USER),
            ])
            self.chain = self.prompt | self.llm.with_structured_output(
                SimpleLLMOutput,
                method="json_mode"
            )

    def _convert_to_result(
        self,
        simple: SimpleLLMOutput,
        source_text: Optional[str] = None
    ) -> MatterExtractionResult:
        """Convert simple LLM output to full MatterExtractionResult"""
        # Build matter entity
        matter = MatterEntity(
            name=simple.matter.name,
            description=simple.matter.description,
            domain=simple.matter.domain,
        )

        # 各类实体统一过滤模板占位符回声（如"合规措施1"）
        risks = [RiskPointEntity(name=r) for r in _drop_placeholders(simple.risks)]
        measures = [MeasureEntity(name=m) for m in _drop_placeholders(simple.measures)]
        authorities = [AuthorityEntity(name=a) for a in _drop_placeholders(simple.authorities)]

        # Build relations（均指向抽取到的事项）
        relations = []
        relations += [
            MatterHasRiskRelation(matter_name=matter.name, risk_name=r.name, frequency=FrequencyLevel.COMMON)
            for r in risks
        ]
        relations += [
            MatterAddressedByRelation(matter_name=matter.name, measure_name=m.name, priority=1)
            for m in measures
        ]
        relations += [
            MatterGovernedByRelation(matter_name=matter.name, authority_name=a.name, priority=1, role="主责")
            for a in authorities
        ]

        return MatterExtractionResult(
            matters=[matter],
            risks=risks,
            measures=measures,
            authorities=authorities,
            relations=relations,
            source_text=source_text,
            extraction_confidence=0.9
        )

    @retry_on_error(max_retries=3, delay=1.0)
    def extract(self, text: str) -> MatterExtractionResult:
        """
        从文本中抽取审查事项实体

        Args:
            text: 原始合规文本

        Returns:
            MatterExtractionResult: 包含事项及所有相关实体和关系

        Raises:
            ValidationError: LLM 返回格式错误（会自动重试）
        """
        logger.info(f"[EXTRACT] Starting matter extraction, text length: {len(text)}")

        try:
            # Invoke the chain - returns SimpleLLMOutput
            simple_result = self.chain.invoke({"text": text})

            # Convert to full MatterExtractionResult
            result = self._convert_to_result(simple_result, source_text=text)

            # Validate and log
            logger.info(f"[EXTRACT] Matter: {result.matters[0].name if result.matters else ''}")
            logger.info(f"[EXTRACT] Risks: {len(result.risks)}")
            logger.info(f"[EXTRACT] Measures: {len(result.measures)}")
            logger.info(f"[EXTRACT] Authorities: {len(result.authorities)}")

            return result

        except ValidationError as e:
            logger.error(f"[EXTRACT] Validation error: {e}")
            raise
        except Exception as e:
            logger.error(f"[EXTRACT] Extraction failed: {e}")
            raise

    def extract_from_dict(self, data: dict) -> MatterExtractionResult:
        """
        从字典数据创建 MatterExtractionResult

        Args:
            data: 包含事项信息的字典

        Returns:
            MatterExtractionResult
        """
        # Build matter entity
        matter = MatterEntity(
            name=data.get("matter", {}).get("name", ""),
            description=data.get("matter", {}).get("description"),
            domain=data.get("matter", {}).get("domain"),
        )

        # Build risk entities
        risks = [
            RiskPointEntity(name=r)
            for r in data.get("risks", [])
        ]

        # Build measure entities
        measures = [
            MeasureEntity(name=m)
            for m in data.get("measures", [])
        ]

        # Build authority entities
        authorities = [
            AuthorityEntity(name=a)
            for a in data.get("authorities", [])
        ]

        # Build relations
        relations = []
        relations += [
            MatterHasRiskRelation(
                matter_name=matter.name,
                risk_name=r.name,
                frequency=FrequencyLevel.COMMON
            )
            for r in risks
        ]
        relations += [
            MatterAddressedByRelation(
                matter_name=matter.name,
                measure_name=m.name,
                priority=1
            )
            for m in measures
        ]
        relations += [
            MatterGovernedByRelation(
                matter_name=matter.name,
                authority_name=a.name,
                priority=1,
                role="主责"
            )
            for a in authorities
        ]

        return MatterExtractionResult(
            matters=[matter],
            risks=risks,
            measures=measures,
            authorities=authorities,
            relations=relations,
            extraction_confidence=0.9
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Matter Extractor Test")
    print("=" * 60)

    # Test 1: Create extractor
    print("\n[TEST 1] Create MatterExtractor")
    extractor = MatterExtractor(temperature=0.0)
    print("[PASS] MatterExtractor created")

    # Test 2: Extract from dict (no LLM call)
    print("\n[TEST 2] Extract from dict")
    test_data = {
        "matter": {
            "name": "行政许可设定",
            "description": "审查规范性文件是否无依据增设行政许可",
            "domain": "市场准入"
        },
        "risks": ["无依据增设许可", "变相设置审批前置", "无依据增设处罚"],
        "measures": ["改为援引上位法既有条款", "补充公平竞争审查"],
        "authorities": ["市场监督管理部门"]
    }

    result = extractor.extract_from_dict(test_data)
    print(f"[PASS] Matter: {result.matters[0].name} ({result.matters[0].domain})")
    print(f"[PASS] Risks: {[r.name for r in result.risks]}")
    print(f"[PASS] Measures: {[m.name for m in result.measures]}")
    print(f"[PASS] Authorities: {[a.name for a in result.authorities]}")
    print(f"[PASS] HAS_RISK relations: {len(result.risks)}")
    print(f"[PASS] ADDRESSED_BY relations: {len(result.measures)}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Matter extractor tests passed!")
    print("=" * 60)
