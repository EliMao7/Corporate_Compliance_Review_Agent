"""
Neo4j Knowledge Graph Schema Definitions
Defines 8 node types and 12 relation types for the compliance knowledge graph.

⚠️ R1（单一事实来源）：本文件是图谱节点/关系类型与值域枚举的**唯一主人**。
其余模块只能 import，不允许另定义一套。
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Node Types (8 types)
# ============================================================

class NodeType(str, Enum):
    """Neo4j node labels"""
    MATTER = "Matter"            # 审查事项
    RISK_POINT = "RiskPoint"     # 风险点
    MEASURE = "Measure"          # 合规措施
    AUTHORITY = "Authority"      # 归口部门
    DOMAIN = "Domain"            # 适用领域
    EVIDENCE = "Evidence"        # 依据材料（需比对的法规）
    GUIDELINE = "Guideline"      # 合规指引
    PRECAUTION = "Precaution"    # 注意事项


@dataclass
class NodeSchema:
    """Schema definition for a node type"""
    label: str
    properties: dict[str, str]  # property_name: data_type
    unique_constraint: Optional[str] = None  # property for unique constraint
    indexes: list[str] = field(default_factory=list)  # properties to index
    description: str = ""


# Node schemas with properties and constraints
NODE_SCHEMAS: dict[NodeType, NodeSchema] = {
    NodeType.MATTER: NodeSchema(
        label="Matter",
        properties={
            "name": "string",
            "description": "string",
            "domain": "string",
            "aliases": "list",
            "created_at": "datetime",
            "updated_at": "datetime",
        },
        unique_constraint="name",
        indexes=["name", "domain"],
        description="审查事项节点，如：行政许可设定、招标投标"
    ),
    NodeType.RISK_POINT: NodeSchema(
        label="RiskPoint",
        properties={
            "name": "string",
            "description": "string",
            "severity": "string",   # low/medium/high/redline
            "legal_basis": "list",  # 涉及的上位法
        },
        unique_constraint="name",
        indexes=["name", "severity"],
        description="风险点节点，如：无依据增设许可、自行设定罚款数额"
    ),
    NodeType.MEASURE: NodeSchema(
        label="Measure",
        properties={
            "name": "string",
            "scenario": "string",
            "key_points": "list",
            "contraindications": "list",  # 不适用/需避免的情形
        },
        unique_constraint="name",
        indexes=["name"],
        description="合规措施节点，如：改为援引上位法既有条款"
    ),
    NodeType.AUTHORITY: NodeSchema(
        label="Authority",
        properties={
            "name": "string",
            "description": "string",
            "level": "string",              # 国家级/省级/市级
            "parent_authority": "string",   # 上级主管部门
        },
        unique_constraint="name",
        indexes=["name", "level"],
        description="归口部门节点，如：市场监督管理部门、发展改革部门"
    ),
    NodeType.DOMAIN: NodeSchema(
        label="Domain",
        properties={
            "name": "string",
            "description": "string",
            "category": "string",  # 市场准入、公共交易、人力资源等
        },
        unique_constraint="name",
        indexes=["name", "category"],
        description="适用领域节点，如：市场准入、安全环保"
    ),
    NodeType.EVIDENCE: NodeSchema(
        label="Evidence",
        properties={
            "name": "string",
            "description": "string",
            "level": "string",           # 法律/行政法规/地方性法规/部门规章/司法解释/行政规范性文件
            "doc_no": "string",          # 文号，如：主席令第21号
            "issuing_body": "string",    # 发布机关
            "effective_date": "string",  # 施行日期 YYYY-MM-DD
            "articles": "list",          # 关注条款
        },
        unique_constraint="name",
        indexes=["name", "level"],
        description="依据材料节点（需比对的法规），如：《中华人民共和国招标投标法》"
    ),
    NodeType.GUIDELINE: NodeSchema(
        label="Guideline",
        properties={
            "name": "string",
            "type": "string",  # 推荐/避免
            "description": "string",
        },
        unique_constraint="name",
        indexes=["name", "type"],
        description="合规指引节点，正面示例与反面示例"
    ),
    NodeType.PRECAUTION: NodeSchema(
        label="Precaution",
        properties={
            "content": "string",
            "category": "string",    # 程序要求、权限边界、表述规范等
            "importance": "string",  # low/medium/high
        },
        indexes=["category"],
        description="注意事项节点，如：涉及处罚设定的须经法定程序"
    ),
}


# ============================================================
# Relation Types (12 types)
# ============================================================

class RelationType(str, Enum):
    """Neo4j relationship types"""
    HAS_RISK = "HAS_RISK"                                  # Matter -> RiskPoint
    MAY_INDICATE = "MAY_INDICATE"                          # RiskPoint -> Matter
    GOVERNED_BY = "GOVERNED_BY"                            # Matter -> Authority
    HAS_JURISDICTION_OVER = "HAS_JURISDICTION_OVER"        # Authority -> Matter
    ADDRESSED_BY = "ADDRESSED_BY"                          # Matter -> Measure
    APPLIES_TO = "APPLIES_TO"                              # Measure -> Matter
    HAS_SIDE_EFFECT = "HAS_SIDE_EFFECT"                    # Measure -> RiskPoint
    CONFLICTS_WITH = "CONFLICTS_WITH"                      # Measure -> Measure
    REQUIRES_EVIDENCE = "REQUIRES_EVIDENCE"                # Matter -> Evidence
    APPLIES_TO_DOMAIN = "APPLIES_TO_DOMAIN"                # Matter -> Domain
    RECOMMENDS_GUIDELINE = "RECOMMENDS_GUIDELINE"          # Matter -> Guideline
    HAS_PRECAUTION = "HAS_PRECAUTION"                      # Matter -> Precaution


# ============================================================
# Value-Vocabulary Enums（抽取层与 Agent 层共用的值域契约）
# ============================================================

class SeverityLevel(str, Enum):
    """风险严重程度（超集契约：抽取层与 Agent 层共用此定义）"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    REDLINE = "redline"      # 高风险红线：涉及许可/处罚/强制设定


class FrequencyLevel(str, Enum):
    """风险在该事项下的出现频率（HAS_RISK / HAS_SIDE_EFFECT 的关系属性）"""
    RARE = "rare"
    OCCASIONAL = "occasional"
    COMMON = "common"
    VERY_COMMON = "very_common"


class EvidenceLevel(str, Enum):
    """依据材料的效力层级（Evidence 节点的 level 属性值域）。

    前五项为法规的效力层级（法律 > 行政法规 > 地方性法规 > 部门规章 / 司法解释）；
    末项 NORMATIVE_DOC 用于"行政规范性文件"——它不是法规，但同样是审查时
    需要比对的依据材料（如国办发〔2018〕37号），故一并纳入本值域。
    """
    LAW = "法律"
    ADMIN_REG = "行政法规"
    LOCAL_REG = "地方性法规"
    DEPT_RULE = "部门规章"
    JUDICIAL = "司法解释"
    NORMATIVE_DOC = "行政规范性文件"


@dataclass
class RelationSchema:
    """Schema definition for a relation type"""
    type: str
    start_node: NodeType
    end_node: NodeType
    properties: dict[str, str] = field(default_factory=dict)
    description: str = ""


# Relation schemas with start/end nodes and properties
RELATION_SCHEMAS: dict[RelationType, RelationSchema] = {
    RelationType.HAS_RISK: RelationSchema(
        type="HAS_RISK",
        start_node=NodeType.MATTER,
        end_node=NodeType.RISK_POINT,
        properties={"frequency": "string"},  # rare/occasional/common/very_common
        description="事项-风险点关系"
    ),
    RelationType.MAY_INDICATE: RelationSchema(
        type="MAY_INDICATE",
        start_node=NodeType.RISK_POINT,
        end_node=NodeType.MATTER,
        properties={"probability": "string"},  # low/medium/high
        description="风险点-事项关系（反向）"
    ),
    RelationType.GOVERNED_BY: RelationSchema(
        type="GOVERNED_BY",
        start_node=NodeType.MATTER,
        end_node=NodeType.AUTHORITY,
        properties={
            "priority": "integer",  # 1=主责, 2=协管
            "role": "string",       # 主责/协管
        },
        description="事项-归口部门关系"
    ),
    RelationType.HAS_JURISDICTION_OVER: RelationSchema(
        type="HAS_JURISDICTION_OVER",
        start_node=NodeType.AUTHORITY,
        end_node=NodeType.MATTER,
        properties={},
        description="归口部门-事项关系（反向）"
    ),
    RelationType.ADDRESSED_BY: RelationSchema(
        type="ADDRESSED_BY",
        start_node=NodeType.MATTER,
        end_node=NodeType.MEASURE,
        properties={
            "priority": "integer",     # 1=推荐, 2=可选
            "applicability": "string"  # 适用场景补充说明
        },
        description="事项-合规措施关系"
    ),
    RelationType.APPLIES_TO: RelationSchema(
        type="APPLIES_TO",
        start_node=NodeType.MEASURE,
        end_node=NodeType.MATTER,
        properties={},
        description="合规措施-事项关系（反向）"
    ),
    RelationType.HAS_SIDE_EFFECT: RelationSchema(
        type="HAS_SIDE_EFFECT",
        start_node=NodeType.MEASURE,
        end_node=NodeType.RISK_POINT,
        properties={"frequency": "string"},
        description="合规措施-自身可能引发的风险关系"
    ),
    RelationType.CONFLICTS_WITH: RelationSchema(
        type="CONFLICTS_WITH",
        start_node=NodeType.MEASURE,
        end_node=NodeType.MEASURE,
        properties={
            "severity": "string",  # low/medium/high
            "description": "string"
        },
        description="合规措施之间的冲突关系"
    ),
    RelationType.REQUIRES_EVIDENCE: RelationSchema(
        type="REQUIRES_EVIDENCE",
        start_node=NodeType.MATTER,
        end_node=NodeType.EVIDENCE,
        properties={"necessity": "string"},  # required/recommended/optional
        description="事项-依据材料关系"
    ),
    RelationType.APPLIES_TO_DOMAIN: RelationSchema(
        type="APPLIES_TO_DOMAIN",
        start_node=NodeType.MATTER,
        end_node=NodeType.DOMAIN,
        properties={},
        description="事项-适用领域关系"
    ),
    RelationType.RECOMMENDS_GUIDELINE: RelationSchema(
        type="RECOMMENDS_GUIDELINE",
        start_node=NodeType.MATTER,
        end_node=NodeType.GUIDELINE,
        properties={"guideline_type": "string"},  # 推荐/避免
        description="事项-合规指引关系"
    ),
    RelationType.HAS_PRECAUTION: RelationSchema(
        type="HAS_PRECAUTION",
        start_node=NodeType.MATTER,
        end_node=NodeType.PRECAUTION,
        properties={"importance": "string"},
        description="事项-注意事项关系"
    ),
}


# ============================================================
# Utility Functions
# ============================================================

def get_node_schema(node_type: NodeType) -> NodeSchema:
    """Get schema for a node type"""
    return NODE_SCHEMAS[node_type]


def get_relation_schema(relation_type: RelationType) -> RelationSchema:
    """Get schema for a relation type"""
    return RELATION_SCHEMAS[relation_type]


def get_all_node_types() -> list[NodeType]:
    """Get all node types"""
    return list(NodeType)


def get_all_relation_types() -> list[RelationType]:
    """Get all relation types"""
    return list(RelationType)


# ============================================================
# Cypher Generation
# ============================================================

def generate_constraint_cypher() -> list[str]:
    """Generate Cypher statements for unique constraints"""
    statements = []
    for node_type, schema in NODE_SCHEMAS.items():
        if schema.unique_constraint:
            prop = schema.unique_constraint
            stmt = f"""
CREATE CONSTRAINT {schema.label.lower()}_{prop}_unique IF NOT EXISTS
FOR (n:{schema.label}) REQUIRE n.{prop} IS UNIQUE"""
            statements.append(stmt.strip())
    return statements


def generate_index_cypher() -> list[str]:
    """Generate Cypher statements for indexes"""
    statements = []
    for node_type, schema in NODE_SCHEMAS.items():
        for prop in schema.indexes:
            if prop != schema.unique_constraint:  # Skip if already has constraint
                stmt = f"""
CREATE INDEX {schema.label.lower()}_{prop}_idx IF NOT EXISTS
FOR (n:{schema.label}) ON (n.{prop})"""
                statements.append(stmt.strip())
    return statements


def generate_full_schema_cypher() -> str:
    """Generate complete schema initialization Cypher script"""
    lines = [
        "// ============================================",
        "// Compliance Knowledge Graph Schema",
        "// Generated by schema.py",
        "// ============================================",
        "",
        "// ===== Unique Constraints =====",
    ]

    for stmt in generate_constraint_cypher():
        lines.append(stmt + ";")
        lines.append("")

    lines.append("// ===== Indexes =====")

    for stmt in generate_index_cypher():
        lines.append(stmt + ";")
        lines.append("")

    lines.append("// ===== Verification =====")
    lines.append("CALL db.constraints();")
    lines.append("CALL db.indexes();")

    return "\n".join(lines)


def generate_llm_schema_text() -> str:
    """Generate a compact graph Schema description for LLM prompts (Text2Cypher).

    供 Text2Cypher 引擎注入系统提示词：让 LLM 看着 Schema 生成合法 Cypher。
    纯静态生成（基于 NODE_SCHEMAS / RELATION_SCHEMAS），不依赖数据库连接。
    三段式：节点类型（含属性）/ 关系类型（含方向三元组）/ 查询约定（只读+LIMIT）。
    """
    lines = ["【节点类型】"]
    for schema in NODE_SCHEMAS.values():
        props = ", ".join(schema.properties.keys())
        lines.append(f"- {schema.label}（{schema.description}）属性: {props}")

    lines.append("")
    lines.append("【关系类型】格式：(起点)-[:关系]->(终点)")
    for schema in RELATION_SCHEMAS.values():
        rel_props = (
            f"，关系属性: {', '.join(schema.properties.keys())}"
            if schema.properties else ""
        )
        lines.append(
            f"- ({schema.start_node.value})-[:{schema.type}]->({schema.end_node.value}) "
            f"{schema.description}{rel_props}"
        )

    lines.append("")
    lines.append("【查询约定】")
    lines.append("- 节点一般用 name 属性匹配；Precaution 节点用 content 属性")
    lines.append("- 只读查询：仅可用 MATCH / OPTIONAL MATCH / WHERE / WITH / UNWIND / RETURN，"
                 "禁止 CREATE / MERGE / DELETE / SET / REMOVE 等任何写操作")
    lines.append("- 查询末尾必须附带 LIMIT（不超过 50）")
    return "\n".join(lines)


# ============================================================
# Main: Self-test
# ============================================================

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Knowledge Graph Schema Validation")
    print("=" * 60)

    # Test node types
    print("\n[NODE TYPES] 8 types")
    for nt in get_all_node_types():
        schema = get_node_schema(nt)
        print(f"  {nt.value}: {len(schema.properties)} properties, constraint={schema.unique_constraint}")

    # Test relation types
    print("\n[RELATION TYPES] 12 types")
    for rt in get_all_relation_types():
        schema = get_relation_schema(rt)
        print(f"  {schema.type}: {schema.start_node.value} -> {schema.end_node.value}")

    assert len(get_all_node_types()) == 8, "Expected 8 node types"
    assert len(get_all_relation_types()) == 12, "Expected 12 relation types"

    # Generate Cypher
    print("\n[CONSTRAINTS]")
    constraints = generate_constraint_cypher()
    print(f"  Generated {len(constraints)} unique constraints")

    print("\n[INDEXES]")
    indexes = generate_index_cypher()
    print(f"  Generated {len(indexes)} indexes")

    # Show sample Cypher
    print("\n[SAMPLE CYPHER]")
    print("-" * 40)
    print(generate_full_schema_cypher()[:500])
    print("...")

    # LLM-facing schema text
    print("\n[LLM SCHEMA TEXT]")
    print("-" * 40)
    llm_text = generate_llm_schema_text()
    print(llm_text[:400])
    print(f"... ({len(llm_text)} chars total)")

    print("\n" + "=" * 60)
    print("[SUCCESS] Schema validation passed!")
    print("=" * 60)
