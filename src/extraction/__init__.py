"""
Entity and Relation Extraction Module
Provides Pydantic schemas for knowledge graph entities and relations.
"""

from .schemas import (
    # Entities
    MatterEntity,
    RiskPointEntity,
    MeasureEntity,
    AuthorityEntity,
    DomainEntity,
    EvidenceEntity,
    GuidelineEntity,
    PrecautionEntity,
    # Relations
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
    # Comprehensive
    MatterExtractionResult,
    RelationTriple,
    # Utilities
    get_all_entity_types,
    get_all_relation_types,
)

__all__ = [
    # Entities
    "MatterEntity",
    "RiskPointEntity",
    "MeasureEntity",
    "AuthorityEntity",
    "DomainEntity",
    "EvidenceEntity",
    "GuidelineEntity",
    "PrecautionEntity",
    # Relations
    "MatterHasRiskRelation",
    "RiskMayIndicateRelation",
    "MatterGovernedByRelation",
    "AuthorityHasJurisdictionRelation",
    "MatterAddressedByRelation",
    "MeasureAppliesToRelation",
    "MeasureHasSideEffectRelation",
    "MeasureConflictsRelation",
    "MatterRequiresEvidenceRelation",
    "MatterAppliesToDomainRelation",
    "MatterRecommendsGuidelineRelation",
    "MatterHasPrecautionRelation",
    # Comprehensive
    "MatterExtractionResult",
    "RelationTriple",
    # Utilities
    "get_all_entity_types",
    "get_all_relation_types",
]
