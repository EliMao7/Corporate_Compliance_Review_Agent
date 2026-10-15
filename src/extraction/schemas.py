"""
Knowledge Graph Schema Definitions
Pydantic models for 8 entity types and 12 relation types.

Based on TDD.md Section: 实体关系抽取（src/extraction/）
"""

from typing import Optional, Union
from enum import Enum

from pydantic import BaseModel, Field


# ============================================================
# Enums —— 统一定义于 src/knowledge_graph/schema.py（R1 名字唯一主人）
# 本文件仅转导入、不再就地定义。
# ============================================================

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.knowledge_graph.schema import (
    SeverityLevel,
    FrequencyLevel,
    EvidenceLevel,
)


# ============================================================
# 1. Entity Types (8 types)
# ============================================================

class MatterEntity(BaseModel):
    """
    审查事项实体
    Node label: Matter
    """
    name: str = Field(
        ...,
        description="事项名称，如：行政许可设定、招标投标、供应商管理"
    )
    description: Optional[str] = Field(
        default=None,
        description="事项描述，包含事项背景、适用范围等"
    )
    domain: Optional[str] = Field(
        default=None,
        description="适用领域，如：市场准入、公共交易、人力资源"
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="事项别名，如：许可证申领又称'行政审批'"
    )
    created_at: Optional[str] = Field(
        default=None,
        description="创建时间戳"
    )


class RiskPointEntity(BaseModel):
    """
    风险点实体
    Node label: RiskPoint
    """
    name: str = Field(
        ...,
        description="风险点名称，如：无依据增设许可、自行设定罚款数额"
    )
    description: Optional[str] = Field(
        default=None,
        description="风险点详细描述"
    )
    severity: SeverityLevel = Field(
        default=SeverityLevel.MEDIUM,
        description="严重程度：low（轻微）、medium（中等）、high（严重）、redline（红线）"
    )
    legal_basis: list[str] = Field(
        default_factory=list,
        description="涉及的上位法，如：['《行政许可法》第十六条']"
    )


class MeasureEntity(BaseModel):
    """
    合规措施实体
    Node label: Measure
    """
    name: str = Field(
        ...,
        description="措施名称，如：改为援引上位法既有条款、补充公平竞争审查"
    )
    scenario: Optional[str] = Field(
        default=None,
        description="适用场景，如：涉及市场准入的规范性文件"
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="落地要点，如：['明确援引条号', '删除自设罚则']"
    )
    contraindications: list[str] = Field(
        default_factory=list,
        description="不适用/需避免的情形，如：['上位法未授权的领域']"
    )


class AuthorityEntity(BaseModel):
    """
    归口部门实体
    Node label: Authority
    """
    name: str = Field(
        ...,
        description="部门名称，如：市场监督管理部门、发展改革部门"
    )
    description: Optional[str] = Field(
        default=None,
        description="部门职责描述"
    )
    level: Optional[str] = Field(
        default=None,
        description="层级：国家级/省级/市级"
    )
    parent_authority: Optional[str] = Field(
        default=None,
        description="上级主管部门，如：省级市场监督管理局的上级为省人民政府"
    )


class DomainEntity(BaseModel):
    """
    适用领域实体
    Node label: Domain
    """
    name: str = Field(
        ...,
        description="领域名称，如：市场准入、公共交易、安全环保、人力资源"
    )
    description: Optional[str] = Field(
        default=None,
        description="领域描述，如：涉及市场主体进入特定行业的准入管理"
    )
    category: Optional[str] = Field(
        default=None,
        description="领域类别，如：市场准入、公共交易"
    )


class EvidenceEntity(BaseModel):
    """
    依据材料实体（需比对的法规/材料）
    Node label: Evidence
    """
    name: str = Field(
        ...,
        description="依据材料名称，如：《中华人民共和国招标投标法》"
    )
    level: Optional[EvidenceLevel] = Field(
        default=None,
        description="法规效力层级：法律/行政法规/地方性法规/部门规章/司法解释"
    )
    doc_no: Optional[str] = Field(
        default=None,
        description="文号，如：主席令第21号"
    )
    issuing_body: Optional[str] = Field(
        default=None,
        description="发布机关，如：全国人民代表大会常务委员会"
    )
    effective_date: Optional[str] = Field(
        default=None,
        description="施行日期，格式 YYYY-MM-DD"
    )
    articles: list[str] = Field(
        default_factory=list,
        description="关注条款，如：['第十六条', '第十七条']"
    )


class GuidelineEntity(BaseModel):
    """
    合规指引实体
    Node label: Guideline
    """
    name: str = Field(
        ...,
        description="指引名称，如：援引上位法既有条款、开展公平竞争审查"
    )
    type: Optional[str] = Field(
        default=None,
        description="指引类型：推荐（正面示例）/避免（反面示例）"
    )
    description: Optional[str] = Field(
        default=None,
        description="指引描述"
    )


class PrecautionEntity(BaseModel):
    """
    注意事项实体
    Node label: Precaution
    """
    content: str = Field(
        ...,
        description="注意事项内容，如：涉及处罚设定的须经法定程序"
    )
    category: Optional[str] = Field(
        default=None,
        description="类别：程序要求、权限边界、表述规范等"
    )
    importance: Optional[str] = Field(
        default=None,
        description="重要程度：low/medium/high"
    )


# ============================================================
# 2. Relation Types (12 types)
# ============================================================

class MatterHasRiskRelation(BaseModel):
    """
    事项-风险点关系
    Pattern: (m:Matter)-[:HAS_RISK]->(r:RiskPoint)
    """
    matter_name: str = Field(..., description="事项名称")
    risk_name: str = Field(..., description="风险点名称")
    frequency: FrequencyLevel = Field(
        default=FrequencyLevel.COMMON,
        description="风险出现频率：rare/occasional/common/very_common"
    )


class RiskMayIndicateRelation(BaseModel):
    """
    风险点-事项关系（反向）
    Pattern: (r:RiskPoint)-[:MAY_INDICATE]->(m:Matter)
    """
    risk_name: str = Field(..., description="风险点名称")
    matter_name: str = Field(..., description="事项名称")
    probability: str = Field(
        default="medium",
        description="牵出概率：low/medium/high"
    )


class MatterGovernedByRelation(BaseModel):
    """
    事项-归口部门关系
    Pattern: (m:Matter)-[:GOVERNED_BY]->(a:Authority)
    """
    matter_name: str = Field(..., description="事项名称")
    authority_name: str = Field(..., description="归口部门名称")
    priority: int = Field(
        default=1,
        description="归口优先级：1（主责）2（协管）"
    )
    role: str = Field(
        default="主责",
        description="承担角色：主责/协管"
    )


class AuthorityHasJurisdictionRelation(BaseModel):
    """
    归口部门-事项关系（反向）
    Pattern: (a:Authority)-[:HAS_JURISDICTION_OVER]->(m:Matter)
    """
    authority_name: str = Field(..., description="归口部门名称")
    matter_name: str = Field(..., description="事项名称")


class MatterAddressedByRelation(BaseModel):
    """
    事项-合规措施关系
    Pattern: (m:Matter)-[:ADDRESSED_BY]->(me:Measure)
    """
    matter_name: str = Field(..., description="事项名称")
    measure_name: str = Field(..., description="合规措施名称")
    priority: int = Field(
        default=1,
        description="优先优先级：1（推荐）2（可选）"
    )
    applicability: Optional[str] = Field(
        default=None,
        description="适用场景补充说明"
    )


class MeasureAppliesToRelation(BaseModel):
    """
    合规措施-事项关系（反向）
    Pattern: (me:Measure)-[:APPLIES_TO]->(m:Matter)
    """
    measure_name: str = Field(..., description="合规措施名称")
    matter_name: str = Field(..., description="事项名称")


class MeasureHasSideEffectRelation(BaseModel):
    """
    合规措施-自身可能引发的风险关系
    Pattern: (me:Measure)-[:HAS_SIDE_EFFECT]->(r:RiskPoint)
    """
    measure_name: str = Field(..., description="合规措施名称")
    risk_name: str = Field(..., description="风险点名称")
    frequency: FrequencyLevel = Field(
        default=FrequencyLevel.OCCASIONAL,
        description="风险触发频率"
    )


class MeasureConflictsRelation(BaseModel):
    """
    合规措施之间的冲突关系
    Pattern: (me1:Measure)-[:CONFLICTS_WITH]->(me2:Measure)
    """
    measure1_name: str = Field(..., description="措施1名称")
    measure2_name: str = Field(..., description="措施2名称")
    severity: SeverityLevel = Field(
        default=SeverityLevel.MEDIUM,
        description="冲突严重程度：low/medium/high"
    )
    description: Optional[str] = Field(
        default=None,
        description="冲突描述"
    )


class MatterRequiresEvidenceRelation(BaseModel):
    """
    事项-依据材料关系
    Pattern: (m:Matter)-[:REQUIRES_EVIDENCE]->(e:Evidence)
    """
    matter_name: str = Field(..., description="事项名称")
    evidence_name: str = Field(..., description="依据材料名称")
    necessity: str = Field(
        default="recommended",
        description="必要性：required（必需）/recommended（推荐）/optional（可选）"
    )


class MatterAppliesToDomainRelation(BaseModel):
    """
    事项-适用领域关系
    Pattern: (m:Matter)-[:APPLIES_TO_DOMAIN]->(d:Domain)
    """
    matter_name: str = Field(..., description="事项名称")
    domain_name: str = Field(..., description="适用领域名称")


class MatterRecommendsGuidelineRelation(BaseModel):
    """
    事项-合规指引关系
    Pattern: (m:Matter)-[:RECOMMENDS_GUIDELINE]->(g:Guideline)
    """
    matter_name: str = Field(..., description="事项名称")
    guideline_name: str = Field(..., description="合规指引名称")
    guideline_type: str = Field(
        default="推荐",
        description="指引类型：推荐/避免"
    )


class MatterHasPrecautionRelation(BaseModel):
    """
    事项-注意事项关系
    Pattern: (m:Matter)-[:HAS_PRECAUTION]->(p:Precaution)
    """
    matter_name: str = Field(..., description="事项名称")
    # ⚠️ 字段名必须是 content：该 dict 会**原样**作为 Cypher 参数传给
    # graph_builder 的 `MATCH (p:Precaution {content: $content})`，且 content 也是
    # Precaution 节点的键属性（见 schema.py）。改名会导致建图时 ParameterMissing。
    content: str = Field(..., description="注意事项内容（= Precaution 节点键属性）")
    importance: str = Field(
        default="medium",
        description="重要程度：low/medium/high"
    )


# ============================================================
# 3. Comprehensive Extraction Result
# ============================================================

# 聚合结果中 relations 列表的元素类型：12 个关系模型的联合
RelationTriple = Union[
    MatterHasRiskRelation,
    RiskMayIndicateRelation,
    MatterGovernedByRelation,
    AuthorityHasJurisdictionRelation,
    MatterAddressedByRelation,
    MeasureAppliesToRelation,
    MeasureHasSideEffectRelation,
    MeasureConflictsRelation,
    MatterRequiresEvidenceRelation,
    MatterAppliesToDomainRelation,
    MatterRecommendsGuidelineRelation,
    MatterHasPrecautionRelation,
]


class MatterExtractionResult(BaseModel):
    """
    审查事项抽取综合结果
    包含事项及其所有相关实体和关系
    """
    # Core entities
    matters: list[MatterEntity] = Field(
        default_factory=list,
        description="审查事项列表"
    )
    risks: list[RiskPointEntity] = Field(
        default_factory=list,
        description="相关风险点列表"
    )
    measures: list[MeasureEntity] = Field(
        default_factory=list,
        description="相关合规措施列表"
    )
    authorities: list[AuthorityEntity] = Field(
        default_factory=list,
        description="相关归口部门列表"
    )

    # Relations
    relations: list[RelationTriple] = Field(
        default_factory=list,
        description="抽取到的关系列表（12 类关系模型的联合）"
    )

    # Metadata
    source_text: Optional[str] = Field(
        default=None,
        description="原始文本来源（关系指向的原文依据）"
    )
    extraction_confidence: float = Field(
        default=0.0,
        description="抽取置信度（0-1）"
    )


# ============================================================
# 4. Utility Functions
# ============================================================

def get_all_entity_types() -> list[type[BaseModel]]:
    """获取所有实体类型"""
    return [
        MatterEntity,
        RiskPointEntity,
        MeasureEntity,
        AuthorityEntity,
        DomainEntity,
        EvidenceEntity,
        GuidelineEntity,
        PrecautionEntity,
    ]


def get_all_relation_types() -> list[type[BaseModel]]:
    """获取所有关系类型"""
    return [
        MatterHasRiskRelation,
        RiskMayIndicateRelation,
        MatterGovernedByRelation,
        AuthorityHasJurisdictionRelation,
        MatterAddressedByRelation,
        MeasureAppliesToRelation,
        MeasureHasSideEffectRelation,
        MeasureConflictsRelation,
        MatterRequiresEvidenceRelation,
        MatterAppliesToDomainRelation,
        MatterRecommendsGuidelineRelation,
        MatterHasPrecautionRelation,
    ]


# ============================================================
# Main: Self-test
# ============================================================

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Fix Windows console encoding
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    sys.path.insert(0, str(Path(__file__).parent.parent.parent.resolve()))

    print("=" * 60)
    print("Knowledge Graph Schema Validation")
    print("=" * 60)

    # Test entity types
    print("\n[TEST 1] Entity Types (8)")
    entities = get_all_entity_types()
    print(f"  Total: {len(entities)}")
    for e in entities:
        print(f"  - {e.__name__}")
    assert len(entities) == 8, "Expected 8 entity types"

    # Test relation types
    print("\n[TEST 2] Relation Types (12)")
    relations = get_all_relation_types()
    print(f"  Total: {len(relations)}")
    for r in relations:
        print(f"  - {r.__name__}")
    assert len(relations) == 12, "Expected 12 relation types"

    # Test entity instantiation
    print("\n[TEST 3] Entity Instantiation")
    matter = MatterEntity(
        name="行政许可设定",
        description="审查规范性文件是否无依据增设行政许可",
        domain="市场准入",
        aliases=["行政审批"]
    )
    print(f"  Matter: {matter.name} ({matter.domain})")

    risk = RiskPointEntity(
        name="无依据增设许可",
        severity=SeverityLevel.REDLINE,
        legal_basis=["《行政许可法》第十六条"]
    )
    print(f"  RiskPoint: {risk.name} ({risk.severity})")

    measure = MeasureEntity(
        name="改为援引上位法既有条款",
        scenario="涉及市场准入的规范性文件",
        key_points=["明确援引条号", "删除自设许可"],
        contraindications=["上位法未授权的领域"]
    )
    print(f"  Measure: {measure.name} ({measure.scenario})")

    authority = AuthorityEntity(
        name="市场监督管理部门",
        description="负责市场主体准入与事中事后监管"
    )
    print(f"  Authority: {authority.name}")

    # Test relation instantiation
    print("\n[TEST 4] Relation Instantiation")
    rel1 = MatterHasRiskRelation(
        matter_name="行政许可设定",
        risk_name="无依据增设许可",
        frequency=FrequencyLevel.COMMON
    )
    print(f"  HAS_RISK: {rel1.matter_name} -> {rel1.risk_name}")

    rel2 = MatterAddressedByRelation(
        matter_name="行政许可设定",
        measure_name="改为援引上位法既有条款",
        priority=1,
        applicability="上位法已明确许可的情形"
    )
    print(f"  ADDRESSED_BY: {rel2.matter_name} -> {rel2.measure_name}")

    rel3 = MatterGovernedByRelation(
        matter_name="行政许可设定",
        authority_name="市场监督管理部门",
        priority=1,
        role="主责"
    )
    print(f"  GOVERNED_BY: {rel3.matter_name} -> {rel3.authority_name}")

    # Test comprehensive result
    print("\n[TEST 5] Comprehensive Extraction Result")
    result = MatterExtractionResult(
        matters=[matter],
        risks=[risk],
        measures=[measure],
        authorities=[authority],
        relations=[rel1, rel2, rel3],
        extraction_confidence=0.95
    )
    print(f"  Matters: {len(result.matters)}")
    print(f"  Risks: {len(result.risks)}")
    print(f"  Measures: {len(result.measures)}")
    print(f"  Authorities: {len(result.authorities)}")
    print(f"  Relations: {len(result.relations)}")
    print(f"  Confidence: {result.extraction_confidence}")

    print("\n" + "=" * 60)
    print("[SUCCESS] All schema tests passed!")
    print("=" * 60)
