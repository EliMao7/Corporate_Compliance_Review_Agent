"""
Knowledge Graph Builder
Imports entities and relations from JSON files into Neo4j database.
"""

import sys
import json
from pathlib import Path
from collections import Counter
from datetime import datetime

# Ensure project root is in sys.path
_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from tqdm import tqdm

from config.settings import settings
from config.paths import DATA_KG_DIR, LOGS_DIR
from src.common.logger import setup_logger
from src.knowledge_graph.schema import NodeType, RelationType, RELATION_SCHEMAS

logger = setup_logger(__name__, "graph_builder.log")


# ============================================================
# Default Paths
# ============================================================

ENTITIES_FILE = DATA_KG_DIR / "entities.json"
RELATIONS_FILE = DATA_KG_DIR / "relations.json"


# ============================================================
# Cypher Templates
# ============================================================

NODE_CYPHER_TEMPLATES = {
    NodeType.MATTER.value: """
        MERGE (n:Matter {name: $name})
        ON CREATE SET
            n.description = $description,
            n.domain = $domain,
            n.aliases = $aliases,
            n.created_at = datetime()
        ON MATCH SET
            n.description = coalesce($description, n.description),
            n.domain = coalesce($domain, n.domain),
            n.updated_at = datetime()
    """,
    NodeType.RISK_POINT.value: """
        MERGE (n:RiskPoint {name: $name})
        ON CREATE SET
            n.description = $description,
            n.severity = $severity,
            n.legal_basis = $legal_basis
        ON MATCH SET
            n.description = coalesce($description, n.description),
            n.severity = coalesce($severity, n.severity),
            n.legal_basis = coalesce($legal_basis, n.legal_basis)
    """,
    NodeType.MEASURE.value: """
        MERGE (n:Measure {name: $name})
        ON CREATE SET
            n.scenario = $scenario,
            n.key_points = $key_points,
            n.contraindications = $contraindications
        ON MATCH SET
            n.scenario = coalesce($scenario, n.scenario),
            n.key_points = coalesce($key_points, n.key_points),
            n.contraindications = coalesce($contraindications, n.contraindications)
    """,
    NodeType.AUTHORITY.value: """
        MERGE (n:Authority {name: $name})
        ON CREATE SET
            n.description = $description,
            n.level = $level,
            n.parent_authority = $parent_authority
        ON MATCH SET
            n.description = coalesce($description, n.description),
            n.level = coalesce($level, n.level),
            n.parent_authority = coalesce($parent_authority, n.parent_authority)
    """,
    NodeType.EVIDENCE.value: """
        MERGE (n:Evidence {name: $name})
        ON CREATE SET
            n.level = $level,
            n.doc_no = $doc_no,
            n.issuing_body = $issuing_body,
            n.effective_date = $effective_date,
            n.articles = $articles
        ON MATCH SET
            n.level = coalesce($level, n.level),
            n.doc_no = coalesce($doc_no, n.doc_no),
            n.articles = coalesce($articles, n.articles)
    """,
    NodeType.DOMAIN.value: """
        MERGE (n:Domain {name: $name})
        ON CREATE SET
            n.description = $description,
            n.category = $category
        ON MATCH SET
            n.description = coalesce($description, n.description),
            n.category = coalesce($category, n.category)
    """,
    NodeType.GUIDELINE.value: """
        MERGE (n:Guideline {name: $name})
        ON CREATE SET
            n.type = $type,
            n.description = $description
        ON MATCH SET
            n.type = coalesce($type, n.type),
            n.description = coalesce($description, n.description)
    """,
    NodeType.PRECAUTION.value: """
        MERGE (n:Precaution {content: $content})
        ON CREATE SET
            n.category = $category,
            n.importance = $importance
        ON MATCH SET
            n.category = coalesce($category, n.category),
            n.importance = coalesce($importance, n.importance)
    """,
}

# 合规域暂无需要归一的历史关系命名别名（映射表未定义 legacy 关系名）。
# 保留归一入口，若将来抽取产物出现旧拼写，在此登记 旧名 -> RelationType 即可。
RELATION_TYPE_ALIASES = {}

RELATION_CYPHER_TEMPLATES = {
    RelationType.HAS_RISK.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (r:RiskPoint {name: $risk_name})
        MERGE (m)-[rel:HAS_RISK]->(r)
        ON CREATE SET rel.frequency = $frequency
    """,
    RelationType.MAY_INDICATE.value: """
        MATCH (r:RiskPoint {name: $risk_name})
        MATCH (m:Matter {name: $matter_name})
        MERGE (r)-[rel:MAY_INDICATE]->(m)
        ON CREATE SET rel.probability = $probability
    """,
    RelationType.GOVERNED_BY.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (a:Authority {name: $authority_name})
        MERGE (m)-[rel:GOVERNED_BY]->(a)
        ON CREATE SET rel.priority = $priority, rel.role = $role
    """,
    RelationType.HAS_JURISDICTION_OVER.value: """
        MATCH (a:Authority {name: $authority_name})
        MATCH (m:Matter {name: $matter_name})
        MERGE (a)-[rel:HAS_JURISDICTION_OVER]->(m)
    """,
    RelationType.ADDRESSED_BY.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (me:Measure {name: $measure_name})
        MERGE (m)-[rel:ADDRESSED_BY]->(me)
        ON CREATE SET rel.priority = $priority, rel.applicability = $applicability
    """,
    RelationType.APPLIES_TO.value: """
        MATCH (me:Measure {name: $measure_name})
        MATCH (m:Matter {name: $matter_name})
        MERGE (me)-[rel:APPLIES_TO]->(m)
    """,
    RelationType.HAS_SIDE_EFFECT.value: """
        MATCH (me:Measure {name: $measure_name})
        MATCH (r:RiskPoint {name: $risk_name})
        MERGE (me)-[rel:HAS_SIDE_EFFECT]->(r)
        ON CREATE SET rel.frequency = $frequency
    """,
    RelationType.CONFLICTS_WITH.value: """
        MATCH (me1:Measure {name: $measure1_name})
        MATCH (me2:Measure {name: $measure2_name})
        MERGE (me1)-[rel:CONFLICTS_WITH]->(me2)
        ON CREATE SET rel.severity = $severity, rel.description = $description
    """,
    RelationType.REQUIRES_EVIDENCE.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (e:Evidence {name: $evidence_name})
        MERGE (m)-[rel:REQUIRES_EVIDENCE]->(e)
        ON CREATE SET rel.necessity = $necessity
    """,
    RelationType.APPLIES_TO_DOMAIN.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (d:Domain {name: $domain_name})
        MERGE (m)-[rel:APPLIES_TO_DOMAIN]->(d)
    """,
    RelationType.RECOMMENDS_GUIDELINE.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (g:Guideline {name: $guideline_name})
        MERGE (m)-[rel:RECOMMENDS_GUIDELINE]->(g)
        ON CREATE SET rel.guideline_type = $guideline_type
    """,
    RelationType.HAS_PRECAUTION.value: """
        MATCH (m:Matter {name: $matter_name})
        MATCH (p:Precaution {content: $content})
        MERGE (m)-[rel:HAS_PRECAUTION]->(p)
        ON CREATE SET rel.importance = $importance
    """,
}


# R1 验收（导入即生效）：模板键必须全部出自契约枚举
# （合规域映射表未定义辅助实体类型；若将来引入，在此白名单登记）
_AUX_NODE_TEMPLATE_KEYS: set = set()
assert set(NODE_CYPHER_TEMPLATES) <= {nt.value for nt in NodeType} | _AUX_NODE_TEMPLATE_KEYS, \
    f"节点模板键越界: {set(NODE_CYPHER_TEMPLATES) - {nt.value for nt in NodeType} - _AUX_NODE_TEMPLATE_KEYS}"
assert set(RELATION_CYPHER_TEMPLATES) <= {rt.value for rt in RelationType}, \
    f"关系模板键越界: {set(RELATION_CYPHER_TEMPLATES) - {rt.value for rt in RelationType}}"


# 关系属性默认值（按属性名登记，跨关系复用；与 src/extraction/schemas.py 的
# 关系模型默认值保持一致）。取值本身仍以 schema.py 的 RELATION_SCHEMAS 声明为准：
# 只有"该关系声明了这个属性"才会补，未声明的不会被注入。
RELATION_PROPERTY_DEFAULTS: dict[str, object] = {
    "frequency": "common",        # HAS_RISK / HAS_SIDE_EFFECT
    "probability": "medium",      # MAY_INDICATE
    "priority": 1,                # GOVERNED_BY / ADDRESSED_BY
    "role": "主责",               # GOVERNED_BY
    "applicability": None,        # ADDRESSED_BY
    "severity": "medium",         # CONFLICTS_WITH
    "description": None,          # CONFLICTS_WITH
    "necessity": "recommended",   # REQUIRES_EVIDENCE
    "guideline_type": "推荐",     # RECOMMENDS_GUIDELINE
    "importance": "medium",       # HAS_PRECAUTION
}

# R1 验收（导入即生效）：登记了默认值的属性名，必须都是 schema 声明过的关系属性；
# 反之，schema 声明的关系属性也必须有默认值——否则 Cypher 的 $参数 又会缺失。
_SCHEMA_RELATION_PROPS = {
    p for rs in RELATION_SCHEMAS.values() for p in rs.properties
}
assert set(RELATION_PROPERTY_DEFAULTS) == _SCHEMA_RELATION_PROPS, (
    f"关系属性默认值表与 schema 不一致："
    f"多余={set(RELATION_PROPERTY_DEFAULTS) - _SCHEMA_RELATION_PROPS}，"
    f"缺失={_SCHEMA_RELATION_PROPS - set(RELATION_PROPERTY_DEFAULTS)}"
)


# ============================================================
# Knowledge Graph Builder
# ============================================================

class KnowledgeGraphBuilder:
    """
    Knowledge Graph Builder

    Imports entities and relations from JSON files into Neo4j.

    Usage:
        >>> builder = KnowledgeGraphBuilder()
        >>> builder.build_all()
        >>> builder.print_statistics()
    """

    def __init__(
        self,
        entities_file: Path = ENTITIES_FILE,
        relations_file: Path = RELATIONS_FILE,
        batch_size: int = 500
    ):
        """
        Initialize the builder

        Args:
            entities_file: Path to entities JSON file
            relations_file: Path to relations JSON file
            batch_size: Number of records per transaction
        """
        self.entities_file = entities_file
        self.relations_file = relations_file
        self.batch_size = batch_size

        self.entities: list[dict] = []
        self.relations: list[dict] = []

        self.stats = {
            "nodes_created": Counter(),
            "relations_created": Counter(),
            "nodes_failed": 0,
            "relations_failed": 0,
        }

        self._driver = None

    def get_driver(self):
        """Get Neo4j driver (lazy initialization)"""
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
                self._driver = GraphDatabase.driver(
                    settings.neo4j_uri,
                    auth=(settings.neo4j_user, settings.neo4j_password)
                )
                logger.info(f"[NEO4J] Connected to {settings.neo4j_uri}")
            except Exception as e:
                logger.error(f"[NEO4J] Connection failed: {e}")
                raise
        return self._driver

    def close(self):
        """Close Neo4j driver"""
        if self._driver:
            self._driver.close()
            self._driver = None
            logger.info("[NEO4J] Connection closed")

    def load_data(self) -> bool:
        """
        Load entities and relations from JSON files

        Returns:
            True if data loaded successfully
        """
        # Load entities
        if self.entities_file.exists():
            try:
                with open(self.entities_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.entities = data.get("entities", [])
                logger.info(f"[LOAD] Loaded {len(self.entities)} entities from {self.entities_file}")
            except Exception as e:
                logger.error(f"[LOAD] Failed to load entities: {e}")
                return False
        else:
            logger.warning(f"[LOAD] Entities file not found: {self.entities_file}")

        # Load relations
        if self.relations_file.exists():
            try:
                with open(self.relations_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.relations = data.get("relations", [])
                logger.info(f"[LOAD] Loaded {len(self.relations)} relations from {self.relations_file}")
            except Exception as e:
                logger.error(f"[LOAD] Failed to load relations: {e}")
                return False
        else:
            logger.warning(f"[LOAD] Relations file not found: {self.relations_file}")

        return True

    def build_nodes(self) -> int:
        """
        Build nodes in Neo4j using MERGE

        Returns:
            Number of nodes processed
        """
        if not self.entities:
            logger.warning("[BUILD] No entities to build")
            return 0

        driver = self.get_driver()
        processed = 0

        logger.info(f"[BUILD] Starting node build: {len(self.entities)} entities")

        for i in tqdm(range(0, len(self.entities), self.batch_size), desc="Building nodes", unit="batch"):
            batch = self.entities[i:i + self.batch_size]

            with driver.session() as session:
                for entity in batch:
                    try:
                        node_type = entity.get("type", "")
                        data = entity.get("data", {})

                        template = NODE_CYPHER_TEMPLATES.get(node_type)
                        if not template:
                            logger.warning(f"[BUILD] Unknown node type: {node_type}")
                            self.stats["nodes_failed"] += 1
                            continue

                        # Prepare parameters
                        params = self._prepare_node_params(node_type, data)

                        # Execute
                        session.run(template, params)
                        self.stats["nodes_created"][node_type] += 1
                        processed += 1

                    except Exception as e:
                        logger.error(f"[BUILD] Failed to create node: {e}")
                        self.stats["nodes_failed"] += 1

        logger.info(f"[BUILD] Node build complete: {processed} processed")
        return processed

    def build_relations(self) -> int:
        """
        Build relations in Neo4j using MERGE

        Returns:
            Number of relations processed
        """
        if not self.relations:
            logger.warning("[BUILD] No relations to build")
            return 0

        driver = self.get_driver()
        processed = 0

        logger.info(f"[BUILD] Starting relation build: {len(self.relations)} relations")

        for i in tqdm(range(0, len(self.relations), self.batch_size), desc="Building relations", unit="batch"):
            batch = self.relations[i:i + self.batch_size]

            with driver.session() as session:
                for relation in batch:
                    try:
                        rel_type = relation.get("type", "")
                        # 归一历史命名（合规域当前为空表，保留入口以便未来兼容）[legacy-alias]
                        rel_type = RELATION_TYPE_ALIASES.get(rel_type, rel_type)
                        data = relation.get("data", {})

                        template = RELATION_CYPHER_TEMPLATES.get(rel_type)
                        if not template:
                            logger.warning(f"[BUILD] Unknown relation type: {rel_type}")
                            self.stats["relations_failed"] += 1
                            continue

                        # 补齐 schema 声明的关系属性默认值后再执行
                        params = self._prepare_relation_params(rel_type, data)

                        # Execute
                        session.run(template, params)
                        self.stats["relations_created"][rel_type] += 1
                        processed += 1

                    except Exception as e:
                        logger.error(f"[BUILD] Failed to create relation: {e}")
                        self.stats["relations_failed"] += 1

        logger.info(f"[BUILD] Relation build complete: {processed} processed")
        return processed

    def build_all(self) -> dict:
        """
        Build complete knowledge graph (nodes first, then relations)

        Returns:
            Statistics dictionary
        """
        print("=" * 60)
        print("KNOWLEDGE GRAPH BUILD")
        print("=" * 60)

        # Load data
        if not self.load_data():
            return {"error": "Failed to load data"}

        # Build nodes first
        print("\n[STEP 1] Building nodes...")
        nodes_count = self.build_nodes()

        # Build relations
        print("\n[STEP 2] Building relations...")
        relations_count = self.build_relations()

        # Print statistics
        self.print_statistics()

        return {
            "nodes": nodes_count,
            "relations": relations_count,
            "stats": self.stats
        }

    def _prepare_relation_params(self, rel_type: str, data: dict) -> dict:
        """把一条关系的 data 补齐为 Cypher 模板所需的完整参数。

        ⚠️ 为什么必须有这一步：模板里 `ON CREATE SET rel.x = $x` 引用的参数
        **必须存在**，否则 Neo4j 直接抛 `ParameterMissing`（实测踩坑：种子/抽取产物
        往往只给两端实体名、不给关系属性，45 条关系里 37 条因此建不出来，
        而 `APPLIES_TO_DOMAIN` 因为未声明属性反而是唯一成功的那批）。

        规则：以 schema.py 的 RELATION_SCHEMAS 为准，只补该关系**声明过**的属性；
        数据里已有的值优先，不被覆盖。
        """
        params = dict(data)
        try:
            schema = RELATION_SCHEMAS[RelationType(rel_type)]
        except (KeyError, ValueError):
            return params  # 未知类型：交由上层模板查表兜底
        for prop in schema.properties:
            if prop not in params:
                params[prop] = RELATION_PROPERTY_DEFAULTS.get(prop)
        return params

    def _prepare_node_params(self, node_type: str, data: dict) -> dict:
        """Prepare parameters for node creation"""
        params = {"name": data.get("name", "")}

        if node_type == "Matter":
            params["description"] = data.get("description")
            params["domain"] = data.get("domain")
            params["aliases"] = data.get("aliases", [])
        elif node_type == "RiskPoint":
            params["description"] = data.get("description")
            params["severity"] = data.get("severity", "medium")
            params["legal_basis"] = data.get("legal_basis", [])
        elif node_type == "Measure":
            params["scenario"] = data.get("scenario")
            params["key_points"] = data.get("key_points", [])
            params["contraindications"] = data.get("contraindications", [])
        elif node_type == "Authority":
            params["description"] = data.get("description")
            params["level"] = data.get("level")
            params["parent_authority"] = data.get("parent_authority")
        elif node_type == "Evidence":
            params["level"] = data.get("level")
            params["doc_no"] = data.get("doc_no")
            params["issuing_body"] = data.get("issuing_body")
            params["effective_date"] = data.get("effective_date")
            params["articles"] = data.get("articles", [])
        elif node_type == "Domain":
            params["description"] = data.get("description")
            params["category"] = data.get("category")
        elif node_type == "Guideline":
            params["type"] = data.get("type")
            params["description"] = data.get("description")
        elif node_type == "Precaution":
            params["content"] = data.get("content", data.get("name", ""))
            params["category"] = data.get("category")
            params["importance"] = data.get("importance")

        return params

    def print_statistics(self):
        """Print build statistics"""
        print("\n" + "=" * 60)
        print("BUILD STATISTICS")
        print("=" * 60)

        # Node statistics
        total_nodes = sum(self.stats["nodes_created"].values())
        print(f"\n[NODES] Total: {total_nodes}")
        for node_type, count in self.stats["nodes_created"].most_common():
            print(f"  {node_type}: {count}")

        if self.stats["nodes_failed"] > 0:
            print(f"  Failed: {self.stats['nodes_failed']}")

        # Relation statistics
        total_relations = sum(self.stats["relations_created"].values())
        print(f"\n[RELATIONS] Total: {total_relations}")
        for rel_type, count in self.stats["relations_created"].most_common():
            print(f"  {rel_type}: {count}")

        if self.stats["relations_failed"] > 0:
            print(f"  Failed: {self.stats['relations_failed']}")

        # Graph density (relations / nodes)
        if total_nodes > 0:
            density = total_relations / total_nodes
            print(f"\n[GRAPH DENSITY] {density:.2f} relations per node")

        print("\n" + "=" * 60)

    def get_graph_statistics(self) -> dict:
        """
        Get statistics from Neo4j database

        Returns:
            Dictionary with node and relation counts
        """
        driver = self.get_driver()

        with driver.session() as session:
            # Count nodes by label
            result = session.run("""
                MATCH (n)
                RETURN labels(n)[0] AS label, count(n) AS count
                ORDER BY count DESC
            """)
            nodes = {record["label"]: record["count"] for record in result}

            # Count relations by type
            result = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) AS type, count(r) AS count
                ORDER BY count DESC
            """)
            relations = {record["type"]: record["count"] for record in result}

            # Total counts
            total_nodes = sum(nodes.values())
            total_relations = sum(relations.values())

            return {
                "nodes": nodes,
                "relations": relations,
                "total_nodes": total_nodes,
                "total_relations": total_relations,
                "density": total_relations / total_nodes if total_nodes > 0 else 0
            }

    def clear_graph(self, confirm: bool = False) -> bool:
        """
        Clear all nodes and relations (DANGEROUS!)

        Args:
            confirm: Must be True to proceed

        Returns:
            True if cleared successfully
        """
        if not confirm:
            logger.warning("[CLEAR] Set confirm=True to clear graph")
            return False

        logger.warning("[CLEAR] Clearing all nodes and relations!")

        driver = self.get_driver()

        with driver.session() as session:
            # Delete all relations first
            session.run("MATCH ()-[r]-() DELETE r")
            logger.info("[CLEAR] Deleted all relations")

            # Delete all nodes
            session.run("MATCH (n) DELETE n")
            logger.info("[CLEAR] Deleted all nodes")

        return True


# ============================================================
# Main: CLI Entry Point
# ============================================================

def main():
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Knowledge Graph Builder")
    parser.add_argument(
        "action",
        choices=["build", "stats", "clear"],
        help="Action to perform"
    )
    parser.add_argument(
        "--entities",
        type=str,
        default=str(ENTITIES_FILE),
        help="Entities JSON file path"
    )
    parser.add_argument(
        "--relations",
        type=str,
        default=str(RELATIONS_FILE),
        help="Relations JSON file path"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for transactions (default: 500)"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm destructive action (for clear)"
    )

    args = parser.parse_args()

    builder = KnowledgeGraphBuilder(
        entities_file=Path(args.entities),
        relations_file=Path(args.relations),
        batch_size=args.batch_size
    )

    try:
        if args.action == "build":
            builder.build_all()
            print("[SUCCESS] graph build completed")

        elif args.action == "stats":
            stats = builder.get_graph_statistics()
            print("\n[DATABASE STATISTICS]")
            print(f"  Total nodes: {stats['total_nodes']}")
            print(f"  Total relations: {stats['total_relations']}")
            print(f"  Density: {stats['density']:.2f}")
            print("\n  Nodes by type:")
            for label, count in stats["nodes"].items():
                print(f"    {label}: {count}")
            print("\n  Relations by type:")
            for rel_type, count in stats["relations"].items():
                print(f"    {rel_type}: {count}")
            print("[SUCCESS] graph stats retrieved")

        elif args.action == "clear":
            if args.confirm:
                builder.clear_graph(confirm=True)
                print("[SUCCESS] Graph cleared")
            else:
                print("[ERROR] Use --confirm to clear graph")

    finally:
        builder.close()


if __name__ == "__main__":
    main()
