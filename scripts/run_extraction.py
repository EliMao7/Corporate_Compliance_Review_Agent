"""
Batch Entity Extraction Script（合规知识图谱离线抽取）
Reads processed regulation data and extracts entities/relations using LLM.

两种模式：
  1) 默认（LLM 抽取）：调 src/extraction/ 下四个抽取器，从清洗后文本抽取实体与关系。
  2) --from-seed（无 LLM）：读取人工整理的领域种子 data/reference/compliance_seed.json，
     直接转换为 entities/relations.json —— 让整条流水线在没有 LLM Key 时也能跑通。

写盘前一律按 src/knowledge_graph/schema.py 校验枚举值（R1 名字唯一主人）。

Usage:
    python scripts/run_extraction.py
    python scripts/run_extraction.py --input data/processed/regulations_processed.json
    python scripts/run_extraction.py --from-seed      # 从合规种子数据建产物（不需要 LLM）
    python scripts/run_extraction.py --resume         # 从断点续跑
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from config.paths import DATA_PROCESSED_DIR, DATA_KG_DIR, LOGS_DIR
from config.settings import settings
from src.common.logger import setup_logger
# R1 名字唯一主人：类型名一律取自契约枚举，禁止字符串字面量
from src.knowledge_graph.schema import NodeType, RelationType
# 注：LLM 抽取器（src.extraction.matter_extractor）在 run_extraction() 内懒加载，
#     以便 --from-seed 模式在无 LLM 依赖 / 抽取器尚未就绪时也能单独跑通。

logger = setup_logger(__name__, "extraction.log")


# ============================================================
# Paths
# ============================================================

INPUT_FILE = DATA_PROCESSED_DIR / "regulations_processed.json"
SEED_FILE = PROJECT_ROOT / "data" / "reference" / "compliance_seed.json"
ENTITIES_FILE = DATA_KG_DIR / "entities.json"
RELATIONS_FILE = DATA_KG_DIR / "relations.json"
CHECKPOINT_FILE = DATA_KG_DIR / ".extraction_checkpoint.json"


# ============================================================
# Enum contracts（写盘前校验用，R1）
# ============================================================

ALLOWED_NODE_TYPES = {nt.value for nt in NodeType}
ALLOWED_RELATION_TYPES = {rt.value for rt in RelationType}

# 抽取聚合体字段 -> 节点类型（MatterExtractionResult.matters/risks/measures/authorities）
ENTITY_FIELD_TO_NODE = {
    "matters": NodeType.MATTER,
    "risks": NodeType.RISK_POINT,
    "measures": NodeType.MEASURE,
    "authorities": NodeType.AUTHORITY,
}

# 关系 Pydantic 类名 -> RelationType（关系模型无 type 字段，按类名映射到契约枚举）
RELATION_CLASS_TO_TYPE = {
    "MatterHasRiskRelation": RelationType.HAS_RISK,
    "RiskMayIndicateRelation": RelationType.MAY_INDICATE,
    "MatterGovernedByRelation": RelationType.GOVERNED_BY,
    "AuthorityHasJurisdictionRelation": RelationType.HAS_JURISDICTION_OVER,
    "MatterAddressedByRelation": RelationType.ADDRESSED_BY,
    "MeasureAppliesToRelation": RelationType.APPLIES_TO,
    "MeasureHasSideEffectRelation": RelationType.HAS_SIDE_EFFECT,
    "MeasureConflictsRelation": RelationType.CONFLICTS_WITH,
    "MatterRequiresEvidenceRelation": RelationType.REQUIRES_EVIDENCE,
    "MatterAppliesToDomainRelation": RelationType.APPLIES_TO_DOMAIN,
    "MatterRecommendsGuidelineRelation": RelationType.RECOMMENDS_GUIDELINE,
    "MatterHasPrecautionRelation": RelationType.HAS_PRECAUTION,
}

# 种子关系参数键：type -> (起点键, 终点键)。键名须与 graph_builder 的 Cypher 模板参数一致
SEED_RELATION_PARAM_KEYS = {
    RelationType.HAS_RISK.value: ("matter_name", "risk_name"),
    RelationType.MAY_INDICATE.value: ("risk_name", "matter_name"),
    RelationType.GOVERNED_BY.value: ("matter_name", "authority_name"),
    RelationType.HAS_JURISDICTION_OVER.value: ("authority_name", "matter_name"),
    RelationType.ADDRESSED_BY.value: ("matter_name", "measure_name"),
    RelationType.APPLIES_TO.value: ("measure_name", "matter_name"),
    RelationType.HAS_SIDE_EFFECT.value: ("measure_name", "risk_name"),
    RelationType.CONFLICTS_WITH.value: ("measure1_name", "measure2_name"),
    RelationType.REQUIRES_EVIDENCE.value: ("matter_name", "evidence_name"),
    RelationType.APPLIES_TO_DOMAIN.value: ("matter_name", "domain_name"),
    RelationType.RECOMMENDS_GUIDELINE.value: ("matter_name", "guideline_name"),
    RelationType.HAS_PRECAUTION.value: ("matter_name", "content"),
}


# ============================================================
# Checkpoint Management
# ============================================================

class CheckpointManager:
    """Manages extraction checkpoint for resume capability"""

    def __init__(self, checkpoint_path: Path):
        self.checkpoint_path = checkpoint_path
        self.data = self._load()

    def _load(self) -> dict:
        """Load checkpoint from file"""
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[CHECKPOINT] Failed to load checkpoint: {e}")
        return {
            "processed_indices": [],
            "entities": [],
            "relations": [],
            "stats": {
                "total": 0,
                "success": 0,
                "failed": 0,
            }
        }

    def save(self):
        """Save checkpoint to file"""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def is_processed(self, index: int) -> bool:
        """Check if index already processed"""
        return index in self.data["processed_indices"]

    def add_result(self, index: int, entities: list, relations: list, success: bool):
        """Add extraction result to checkpoint"""
        self.data["processed_indices"].append(index)
        if success:
            self.data["entities"].extend(entities)
            self.data["relations"].extend(relations)
            self.data["stats"]["success"] += 1
        else:
            self.data["stats"]["failed"] += 1

    def get_stats(self) -> dict:
        """Get current stats"""
        return self.data["stats"]

    def clear(self):
        """Clear checkpoint"""
        self.data = {
            "processed_indices": [],
            "entities": [],
            "relations": [],
            "stats": {"total": 0, "success": 0, "failed": 0}
        }
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()


# ============================================================
# Entity/Relation Serialization
# ============================================================

def serialize_entities(result) -> list[dict]:
    """Convert MatterExtractionResult entities to serializable dicts（类型名取自 NodeType 枚举，R1）"""
    entities = []

    for field_name, node_type in ENTITY_FIELD_TO_NODE.items():
        for item in getattr(result, field_name, []) or []:
            entities.append({
                "type": node_type.value,
                "data": item.model_dump(mode="json"),
            })

    return entities


def serialize_relations(result) -> list[dict]:
    """Convert MatterExtractionResult relations to serializable dicts（类型名取自 RelationType 枚举，R1）"""
    relations = []

    for rel in getattr(result, "relations", []) or []:
        cls_name = type(rel).__name__
        rel_type = RELATION_CLASS_TO_TYPE.get(cls_name)
        if rel_type is None:
            logger.warning(f"[SERIALIZE] 未知关系类型，已跳过：{cls_name}")
            continue
        relations.append({
            "type": rel_type.value,
            "data": rel.model_dump(mode="json"),
        })

    return relations


# ============================================================
# Seed Mode（无需 LLM）：从人工整理的领域种子生成产物
# ============================================================

def load_seed(seed_path: Path) -> tuple[list[dict], list[dict]]:
    """读取 compliance_seed.json，转换为 (entities, relations) 列表。"""
    if not seed_path.exists():
        logger.error(f"[SEED] 种子文件不存在：{seed_path}")
        return [], []

    seed = json.loads(seed_path.read_text(encoding="utf-8"))

    entities: list[dict] = []
    for label, items in (seed.get("nodes") or {}).items():
        if label not in ALLOWED_NODE_TYPES:
            logger.warning(f"[SEED] 跳过契约之外的节点标签：{label}")
            continue
        for item in items:
            entities.append({"type": label, "data": item})

    relations: list[dict] = []
    for rel in seed.get("relations") or []:
        rel_type = rel.get("type", "")
        if rel_type not in ALLOWED_RELATION_TYPES:
            logger.warning(f"[SEED] 跳过契约之外的关系类型：{rel_type}")
            continue
        start_key, end_key = SEED_RELATION_PARAM_KEYS[rel_type]
        relations.append({
            "type": rel_type,
            "data": {start_key: rel.get("start", ""), end_key: rel.get("end", "")},
        })

    logger.info(f"[SEED] 载入种子：{len(entities)} 实体 / {len(relations)} 关系")
    return entities, relations


# ============================================================
# Main Extraction Logic
# ============================================================

def load_input_data(input_path: Path) -> list[dict]:
    """Load processed regulation data"""
    if not input_path.exists():
        logger.error(f"[ERROR] Input file not found: {input_path}")
        return []

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 兼容多种清洗产物结构
        records = data.get("regulations") or data.get("items") or data.get("data") or []
        if isinstance(data, list):
            records = data
        logger.info(f"[LOAD] Loaded {len(records)} records from {input_path}")
        return records

    except Exception as e:
        logger.error(f"[ERROR] Failed to load input: {e}")
        return []


def validate_against_schema(entities: list, relations: list):
    """写盘前按契约枚举校验（R1）：任何越界名字即报错。"""
    bad_e = sorted({e["type"] for e in entities if e.get("type") not in ALLOWED_NODE_TYPES})
    bad_r = sorted({r["type"] for r in relations if r.get("type") not in ALLOWED_RELATION_TYPES})
    if bad_e:
        raise ValueError(f"[R1] entities.json 出现契约之外的节点类型：{bad_e}")
    if bad_r:
        raise ValueError(f"[R1] relations.json 出现契约之外的关系类型：{bad_r}")


def save_results(entities: list, relations: list, stats: dict):
    """Save extraction results to files（先按契约枚举校验，再写盘）"""
    validate_against_schema(entities, relations)
    DATA_KG_DIR.mkdir(parents=True, exist_ok=True)

    # Save entities
    entities_output = {
        "metadata": {
            "extracted_at": datetime.now().isoformat(),
            "total_entities": len(entities),
            "stats": stats
        },
        "entities": entities
    }
    with open(ENTITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(entities_output, f, ensure_ascii=False, indent=2)
    logger.info(f"[SAVE] Entities saved to {ENTITIES_FILE}")

    # Save relations
    relations_output = {
        "metadata": {
            "extracted_at": datetime.now().isoformat(),
            "total_relations": len(relations)
        },
        "relations": relations
    }
    with open(RELATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(relations_output, f, ensure_ascii=False, indent=2)
    logger.info(f"[SAVE] Relations saved to {RELATIONS_FILE}")


def print_statistics(entities: list, relations: list, stats: dict):
    """Print extraction statistics"""
    print("\n" + "=" * 60)
    print("EXTRACTION STATISTICS")
    print("=" * 60)

    # Overall stats
    print(f"\n[OVERVIEW]")
    print(f"  Total records: {stats.get('total', 0)}")
    print(f"  Success: {stats.get('success', 0)}")
    print(f"  Failed: {stats.get('failed', 0)}")

    # Entity stats by type
    entity_counter = Counter(e["type"] for e in entities)
    print(f"\n[ENTITIES] Total: {len(entities)}")
    for etype, count in entity_counter.most_common():
        print(f"  {etype}: {count}")

    # Relation stats by type
    relation_counter = Counter(r["type"] for r in relations)
    print(f"\n[RELATIONS] Total: {len(relations)}")
    for rtype, count in relation_counter.most_common():
        print(f"  {rtype}: {count}")

    print("\n" + "=" * 60)


def run_from_seed():
    """种子模式：直接从 compliance_seed.json 生成产物（无需 LLM）。"""
    print("=" * 60)
    print("SEED-BASED GRAPH DATA BUILD（无需 LLM）")
    print("=" * 60)
    print(f"Seed:   {SEED_FILE}")
    print(f"Output: {ENTITIES_FILE}")
    print(f"        {RELATIONS_FILE}")
    print("=" * 60)

    entities, relations = load_seed(SEED_FILE)
    if not entities and not relations:
        print("[ERROR] 种子数据为空，未生成任何产物")
        return

    stats = {"total": len(entities), "success": len(entities), "failed": 0, "source": "seed"}
    save_results(entities, relations, stats)
    print_statistics(entities, relations, stats)

    print("\n[SUCCESS] 已从种子数据生成知识图谱产物！")
    print(f"  Entities: {ENTITIES_FILE}")
    print(f"  Relations: {RELATIONS_FILE}")


def run_extraction(
    input_path: Path = INPUT_FILE,
    resume: bool = False,
    use_llm: bool = True
):
    """
    Run batch entity extraction

    Args:
        input_path: Path to input JSON file
        resume: If True, resume from checkpoint
        use_llm: If True, use LLM extraction; else use dict-based extraction
    """
    print("=" * 60)
    print("BATCH ENTITY EXTRACTION（合规域）")
    print("=" * 60)
    print(f"Input: {input_path}")
    print(f"Resume: {resume}")
    print(f"Use LLM: {use_llm}")
    print("=" * 60)

    # Load input data
    records = load_input_data(input_path)
    if not records:
        print("[ERROR] No data to process")
        return

    # Initialize checkpoint
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    if not resume:
        checkpoint.clear()
        logger.info("[CHECKPOINT] Cleared previous checkpoint")
    else:
        processed = len(checkpoint.data["processed_indices"])
        logger.info(f"[CHECKPOINT] Resuming from checkpoint, {processed} already processed")

    # Initialize extractor（懒加载：仅 LLM 抽取路径需要）
    # temperature=0：抽取任务贪心解码最稳，避免结构漂移
    from src.extraction.matter_extractor import MatterExtractor
    extractor = MatterExtractor(temperature=0.0)

    checkpoint.data["stats"]["total"] = len(records)
    MAX_WORKERS = 8

    def process_one(idx: int, record: dict):
        """单条记录抽取（工作线程内执行）。返回 (entities, relations)"""
        name = record.get("name", record.get("title", f"record_{idx}"))

        # Build comprehensive text for LLM extraction
        text_parts = [f"文件/事项名称：{name}"]
        for field, label in (
            ("description", "描述"),
            ("content", "内容"),
            ("category", "分类"),
            ("issuing_body", "发布机关"),
            ("articles", "条款"),
        ):
            val = record.get(field)
            if not val:
                continue
            if isinstance(val, list):
                val = "；".join(str(v) for v in val)
            text_parts.append(f"{label}：{str(val)[:1500]}")
        text = "\n".join(text_parts)

        if use_llm:
            logger.info(f"[EXTRACT] Using LLM for: {name}")
            result = extractor.extract(text)
        else:
            result = extractor.extract_from_dict(record)

        return serialize_entities(result), serialize_relations(result)

    pending = [
        (idx, record)
        for idx, record in enumerate(records)
        if not (resume and checkpoint.is_processed(idx))
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one, idx, d): idx for idx, d in pending}
        done_count = 0
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Extracting", unit="record"):
            idx = futures[fut]
            name = records[idx].get("name", records[idx].get("title", f"record_{idx}"))
            try:
                entities, relations = fut.result()
                checkpoint.add_result(idx, entities, relations, success=True)
                logger.debug(f"[EXTRACT] {name}: {len(entities)} entities, {len(relations)} relations")
            except Exception as e:
                logger.error(f"[EXTRACT] Failed for {name}: {e}")
                checkpoint.add_result(idx, [], [], success=False)

            done_count += 1
            if done_count % 10 == 0:
                checkpoint.save()

    # Final save
    checkpoint.save()

    # Save final results
    all_entities = checkpoint.data["entities"]
    all_relations = checkpoint.data["relations"]
    stats = checkpoint.get_stats()

    save_results(all_entities, all_relations, stats)

    # Print statistics
    print_statistics(all_entities, all_relations, stats)

    print("\n[SUCCESS] Extraction completed!")
    print(f"  Entities: {ENTITIES_FILE}")
    print(f"  Relations: {RELATIONS_FILE}")
    print(f"  Checkpoint: {CHECKPOINT_FILE}")


# ============================================================
# CLI Entry Point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Batch Entity Extraction for Compliance Knowledge Graph"
    )
    parser.add_argument(
        "--input",
        type=str,
        default=str(INPUT_FILE),
        help=f"Input JSON file path (default: {INPUT_FILE})"
    )
    parser.add_argument(
        "--from-seed",
        action="store_true",
        help="从 data/reference/compliance_seed.json 生成产物（无需 LLM）"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint if exists"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use dict-based extraction (no LLM calls)"
    )
    parser.add_argument(
        "--clear-checkpoint",
        action="store_true",
        help="Clear checkpoint before starting"
    )

    args = parser.parse_args()

    if args.from_seed:
        run_from_seed()
        return

    if args.clear_checkpoint:
        checkpoint = CheckpointManager(CHECKPOINT_FILE)
        checkpoint.clear()
        print("[INFO] Checkpoint cleared")

    run_extraction(
        input_path=Path(args.input),
        resume=args.resume,
        use_llm=not args.no_llm
    )


if __name__ == "__main__":
    main()
