"""
Knowledge Graph Queries
Provides query functions for Agents to access the knowledge graph.
"""

import re
import sys
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "graph_queries.log")


class GraphQueries:
    """
    Knowledge Graph Query Interface

    Provides high-level query functions for Agents.

    Usage:
        >>> queries = GraphQueries()
        >>> risks = queries.get_matter_risks("招标投标")
        >>> matters = queries.get_risk_matters("无依据增设行政许可")
        >>> measures = queries.get_matter_measures("招标投标")
    """

    def __init__(self):
        self._driver = None

    def get_driver(self):
        """Get Neo4j driver"""
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password)
            )
        return self._driver

    def close(self):
        """Close driver"""
        if self._driver:
            self._driver.close()
            self._driver = None

    # ============================================================
    # Generic Cypher Execution (for Text2Cypher)
    # ============================================================

    # L2 安全层：写关键字拒绝正则（与只读事务构成双保险）
    _WRITE_KEYWORDS = re.compile(
        r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH)\b|CALL\s+db",
        re.IGNORECASE,
    )

    def check_cypher_syntax(self, cypher: str, params: Optional[dict] = None) -> tuple[bool, str]:
        """
        Validate Cypher syntax with EXPLAIN (plans but does not execute).

        Args:
            cypher: Cypher query string
            params: Optional query parameters

        Returns:
            (is_valid, message) tuple
        """
        try:
            from neo4j.exceptions import CypherSyntaxError
        except ImportError:  # pragma: no cover
            return False, "neo4j 驱动未安装"
        try:
            driver = self.get_driver()
            with driver.session() as session:
                session.run(f"EXPLAIN {cypher}", params or {})
            return True, "语法正确"
        except CypherSyntaxError as e:
            return False, f"语法错误: {e}"
        except Exception as e:
            return False, f"校验失败: {e}"

    def run_readonly_cypher(self, cypher: str, params: Optional[dict] = None,
                            max_records: int = 50) -> list[dict]:
        """
        Execute an LLM-generated Cypher in a READ-ONLY transaction.

        供 Text2Cypher 引擎调用。三层安全防线：
        1. L2 写关键字拒绝：发送前正则拦截 CREATE/MERGE/DELETE/SET/... 与 CALL db* 过程调用
        2. L1 只读事务：session(default_access_mode=READ_ACCESS)，驱动层保证即使绕过
           关键字检查也无法写入（数据库直接拒绝）
        3. L5 记录数截断：枚举到 max_records 立即停止，防全表扫描打爆上下文

        Args:
            cypher: Cypher query string (must be read-only)
            params: Optional query parameters
            max_records: Maximum number of records to return

        Returns:
            List of record dicts (each record via record.data())

        Raises:
            ValueError: If the query contains write keywords (L2 rejection)
            Exception: Neo4j connection/execution errors propagate to caller
                       (Text2CypherEngine 负责兜底，绝不向上打断 ReAct 循环)
        """
        # L2: 写关键字拒绝
        if self._WRITE_KEYWORDS.search(cypher):
            logger.warning(f"[run_readonly_cypher] REJECTED write attempt: {cypher[:200]}")
            raise ValueError("仅允许执行只读 Cypher 查询（检测到写操作关键字）")

        from neo4j import READ_ACCESS

        driver = self.get_driver()
        records: list[dict] = []

        with driver.session(default_access_mode=READ_ACCESS) as session:
            def _work(tx):
                result = tx.run(cypher, params or {})
                for i, record in enumerate(result):
                    if i >= max_records:  # L5: 记录数截断
                        break
                    records.append(record.data())

            session.execute_read(_work)

        return records

    # ============================================================
    # Matter Queries（审查事项）
    # ============================================================

    def get_matter_info(self, matter_name: str) -> Optional[dict]:
        """
        获取审查事项的基本信息。

        Args:
            matter_name: 事项名称

        Returns:
            事项信息字典，查无返回 None
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter {name: $name})
                RETURN m.name AS name,
                       m.description AS description,
                       m.domain AS domain
            """, name=matter_name)
            record = result.single()
            if record:
                return {
                    "name": record["name"],
                    "description": record["description"],
                    "domain": record["domain"]
                }
            return None

    def get_matter_risks(self, matter_name: str) -> list[str]:
        """
        获取某事项关联的所有风险点。

        Args:
            matter_name: 事项名称

        Returns:
            风险点名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter {name: $name})-[:HAS_RISK]->(r:RiskPoint)
                RETURN r.name AS risk
                ORDER BY r.name
            """, name=matter_name)
            return [record["risk"] for record in result]

    def get_matter_measures(self, matter_name: str) -> list[dict]:
        """
        获取某事项的合规措施建议。

        Args:
            matter_name: 事项名称

        Returns:
            措施信息字典列表（含适用场景与适用性说明）
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter {name: $name})-[rel:ADDRESSED_BY]->(me:Measure)
                RETURN me.name AS name,
                       me.scenario AS scenario,
                       rel.applicability AS applicability
                ORDER BY rel.priority, me.name
            """, name=matter_name)
            return [
                {
                    "name": record["name"],
                    "scenario": record["scenario"],
                    "applicability": record["applicability"]
                }
                for record in result
            ]

    def get_matter_authorities(self, matter_name: str) -> list[str]:
        """
        获取某事项的归口部门。

        Args:
            matter_name: 事项名称

        Returns:
            归口部门名称列表（按优先级排序）
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter {name: $name})-[rel:GOVERNED_BY]->(a:Authority)
                RETURN a.name AS authority
                ORDER BY rel.priority
            """, name=matter_name)
            return [record["authority"] for record in result]

    def get_matter_evidences(self, matter_name: str) -> list[str]:
        """
        获取某事项需要比对的依据材料。

        Args:
            matter_name: 事项名称

        Returns:
            依据材料名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter {name: $name})-[:REQUIRES_EVIDENCE]->(e:Evidence)
                RETURN e.name AS evidence
                ORDER BY e.name
            """, name=matter_name)
            return [record["evidence"] for record in result]

    def get_matter_full_info(self, matter_name: str) -> Optional[dict]:
        """
        获取某事项的完整信息。

        Args:
            matter_name: 事项名称

        Returns:
            事项完整信息字典，查无返回 None
        """
        info = self.get_matter_info(matter_name)
        if not info:
            return None

        info["risks"] = self.get_matter_risks(matter_name)
        info["measures"] = self.get_matter_measures(matter_name)
        info["authorities"] = self.get_matter_authorities(matter_name)
        info["evidences"] = self.get_matter_evidences(matter_name)

        return info

    # ============================================================
    # RiskPoint Queries（风险点）
    # ============================================================

    def get_risk_matters(self, risk_name: str) -> list[str]:
        """
        获取某风险点可能牵出的审查事项。

        Args:
            risk_name: 风险点名称

        Returns:
            事项名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (r:RiskPoint {name: $name})<-[:HAS_RISK]-(m:Matter)
                RETURN m.name AS matter
                ORDER BY m.name
            """, name=risk_name)
            return [record["matter"] for record in result]

    def get_risk_info(self, risk_name: str) -> Optional[dict]:
        """
        获取某风险点的信息。

        Args:
            risk_name: 风险点名称

        Returns:
            风险点信息字典，查无返回 None
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (r:RiskPoint {name: $name})
                RETURN r.name AS name,
                       r.description AS description,
                       r.severity AS severity
            """, name=risk_name)
            record = result.single()
            if record:
                return {
                    "name": record["name"],
                    "description": record["description"],
                    "severity": record["severity"]
                }
            return None

    # ============================================================
    # Measure Queries（合规措施）
    # ============================================================

    def get_measure_info(self, measure_name: str) -> Optional[dict]:
        """
        获取某合规措施的信息。

        Args:
            measure_name: 措施名称

        Returns:
            措施信息字典，查无返回 None
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (me:Measure {name: $name})
                RETURN me.name AS name,
                       me.scenario AS scenario,
                       me.key_points AS key_points,
                       me.contraindications AS contraindications
            """, name=measure_name)
            record = result.single()
            if record:
                return {
                    "name": record["name"],
                    "scenario": record["scenario"],
                    "key_points": record["key_points"] or [],
                    "contraindications": record["contraindications"] or []
                }
            return None

    def get_measure_matters(self, measure_name: str) -> list[str]:
        """
        获取某合规措施适用的审查事项。

        Args:
            measure_name: 措施名称

        Returns:
            事项名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (me:Measure {name: $name})-[:APPLIES_TO]->(m:Matter)
                RETURN m.name AS matter
                ORDER BY m.name
            """, name=measure_name)
            return [record["matter"] for record in result]

    def get_measure_conflicts(self, measure_name: str) -> list[dict]:
        """
        获取某合规措施与其他措施的冲突。

        Args:
            measure_name: 措施名称

        Returns:
            冲突信息字典列表（对端措施、严重程度、说明）
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (me1:Measure {name: $name})-[rel:CONFLICTS_WITH]->(me2:Measure)
                RETURN me2.name AS name,
                       rel.severity AS severity,
                       rel.description AS description
            """, name=measure_name)
            return [
                {
                    "name": record["name"],
                    "severity": record["severity"],
                    "description": record["description"]
                }
                for record in result
            ]

    # ============================================================
    # Authority Queries（归口部门）
    # ============================================================

    def get_authority_matters(self, authority_name: str) -> list[str]:
        """
        获取某归口部门管辖的审查事项。

        Args:
            authority_name: 归口部门名称

        Returns:
            事项名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            # 使用 GOVERNED_BY 关系（Matter -> Authority）反向查询
            result = session.run("""
                MATCH (m:Matter)-[:GOVERNED_BY]->(a:Authority {name: $name})
                RETURN m.name AS matter
                ORDER BY m.name
            """, name=authority_name)
            return [record["matter"] for record in result]

    # ============================================================
    # Search Queries
    # ============================================================

    def search_matters(self, keyword: str, limit: int = 10) -> list[str]:
        """
        按关键词搜索审查事项。

        Args:
            keyword: 搜索关键词
            limit: 最大返回条数

        Returns:
            事项名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Matter)
                WHERE m.name CONTAINS $keyword
                   OR m.description CONTAINS $keyword
                RETURN m.name AS name
                LIMIT $limit
            """, keyword=keyword, limit=limit)
            return [record["name"] for record in result]

    def search_risks(self, keyword: str, limit: int = 10) -> list[str]:
        """
        按关键词搜索风险点。

        Args:
            keyword: 搜索关键词
            limit: 最大返回条数

        Returns:
            风险点名称列表
        """
        driver = self.get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (r:RiskPoint)
                WHERE r.name CONTAINS $keyword
                RETURN r.name AS name
                LIMIT $limit
            """, keyword=keyword, limit=limit)
            return [record["name"] for record in result]

    # ============================================================
    # Statistics Queries
    # ============================================================

    def get_statistics(self) -> dict:
        """
        Get graph statistics

        Returns:
            Dict with statistics
        """
        driver = self.get_driver()
        with driver.session() as session:
            # Node counts
            result = session.run("""
                MATCH (n)
                RETURN labels(n)[0] AS label, count(n) AS count
            """)
            nodes = {r["label"]: r["count"] for r in result}

            # Relation counts
            result = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) AS type, count(r) AS count
            """)
            relations = {r["type"]: r["count"] for r in result}

            return {
                "nodes": nodes,
                "relations": relations,
                "total_nodes": sum(nodes.values()),
                "total_relations": sum(relations.values())
            }

def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Knowledge Graph Queries Test")
    print("=" * 60)

    queries = GraphQueries()

    try:
        # Test statistics
        print("\n[TEST 1] Graph Statistics")
        stats = queries.get_statistics()
        print(f"  Total nodes: {stats['total_nodes']}")
        print(f"  Total relations: {stats['total_relations']}")
        print(f"  Node types: {list(stats['nodes'].keys())}")

        # Test matter query
        print("\n[TEST 2] Matter Query (招标投标)")
        info = queries.get_matter_full_info("招标投标")
        if info:
            print(f"  Name: {info['name']}")
            print(f"  Risks: {info['risks']}")
            print(f"  Measures: {[m['name'] for m in info['measures']]}")
            print(f"  Authorities: {info['authorities']}")
        else:
            print("  Matter not found")

        # Test risk query
        print("\n[TEST 3] RiskPoint Query (无依据增设行政许可)")
        matters = queries.get_risk_matters("无依据增设行政许可")
        print(f"  Matters: {matters}")

        # Test search
        print("\n[TEST 4] Search Query (许可)")
        results = queries.search_matters("许可")
        print(f"  Results: {results}")

        print("\n" + "=" * 60)
        print("[SUCCESS] All query tests completed!")
        print("=" * 60)

    finally:
        queries.close()


if __name__ == "__main__":
    main()
