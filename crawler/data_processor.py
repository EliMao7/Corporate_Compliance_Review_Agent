#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业合规审查 Agent - 法规数据清洗器
===================================

读取爬虫产出的原始法规数据（data/raw/regulations.json），
做去重、字段清洗、效力层级标准化与统计，输出：

  - data/processed/regulations.json   清洗后数据（供程序消费）
  - data/processed/regulations.xlsx   清洗后数据（供人工查看）

用法：
    python crawler/data_processor.py --help
    python crawler/data_processor.py
"""

import json
import logging
import re
import sys
from pathlib import Path
from datetime import datetime
from collections import Counter

# pandas 仅用于导出 Excel，导入失败时不影响 JSON 输出（见 save_excel）
try:
    import pandas as pd
except ImportError:  # pragma: no cover - 最小环境下保持模块可导入
    pd = None

# ===== Configuration =====

PROJECT_ROOT = Path(__file__).parent.parent
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
DATA_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

INPUT_FILE = DATA_RAW_DIR / "regulations.json"
OUTPUT_JSON = DATA_PROCESSED_DIR / "regulations.json"
OUTPUT_XLSX = DATA_PROCESSED_DIR / "regulations.xlsx"

# Logging setup
log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "data_processor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 效力层级标准化映射（宪法 > 法律 > 行政法规 > 监察法规 > 部门规章 > 地方法规 > 司法解释）。
# 注意：此处只做「别名 → 规范名」的归一，不改变效力层级的判定口径。
LEVEL_MAPPING = {
    "法律法规": "法律",
    "法律": "法律",
    "行政法规": "行政法规",
    "监察法规": "监察法规",
    "部门规章": "部门规章",
    "地方性法规": "地方法规",
    "地方性规章": "地方法规",
    "地方法规": "地方法规",
    "司法解释": "司法解释",
    "宪法": "宪法",
    "国务院文件": "国务院文件",
}


class DataProcessor:
    """Process and clean raw regulation data"""

    def __init__(self):
        self.raw_data = []
        self.cleaned_data = []
        self.stats = {
            "total_raw": 0,
            "duplicates_removed": 0,
            "empty_removed": 0,
            "cleaned": 0,
        }

    def load_raw_data(self):
        """Load raw data from JSON file"""
        if not INPUT_FILE.exists():
            logger.error(f"[ERROR] Raw data file not found: {INPUT_FILE}")
            return False

        try:
            with open(INPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.raw_data = data.get("regulations", [])
            self.stats["total_raw"] = len(self.raw_data)
            logger.info(f"[LOADED] {len(self.raw_data)} raw records")
            return True

        except Exception as e:
            logger.error(f"[ERROR] Failed to load raw data: {e}")
            return False

    def clean_text(self, text: str) -> str:
        """Clean text: remove extra spaces and special characters"""
        if not text:
            return ""

        # Remove extra whitespace
        text = " ".join(text.split())

        # Remove special characters (keep Chinese, English, numbers, punctuation)
        text = re.sub(r'[^\w\s一-鿿，。、；：！？""''（）《》〔〕]+', '', text)

        return text.strip()

    def standardize_level(self, level: str) -> str:
        """把效力层级的各种写法归一为规范名（如 法律法规 → 法律）"""
        if not level:
            return ""

        level = level.strip()

        # Check if it's in our mapping
        if level in LEVEL_MAPPING:
            return LEVEL_MAPPING[level]

        # Try to find a match
        for key, value in LEVEL_MAPPING.items():
            if key in level:
                return value if value else ""

        # If no match, return as is (but cleaned)
        return self.clean_text(level)

    def remove_duplicates(self):
        """Remove duplicate regulations based on 法规 ID / 文号 / 标题"""
        seen_keys = set()
        unique_data = []

        for record in self.raw_data:
            key = (
                str(record.get("reg_id") or "").strip()
                or str(record.get("doc_no") or "").strip()
                or str(record.get("title") or "").strip()
            )

            if not key:
                self.stats["empty_removed"] += 1
                continue

            if key in seen_keys:
                self.stats["duplicates_removed"] += 1
                continue

            seen_keys.add(key)
            unique_data.append(record)

        self.raw_data = unique_data
        logger.info(
            f"[DEDUP] Removed {self.stats['duplicates_removed']} duplicates, "
            f"{self.stats['empty_removed']} empty records"
        )

    def clean_articles(self, articles) -> list:
        """清洗条文列表（保留条目编号与正文）。"""
        cleaned = []
        if not isinstance(articles, list):
            return cleaned
        for item in articles:
            if isinstance(item, dict):
                no = self.clean_text(item.get("no", ""))
                text = self.clean_text(item.get("text", ""))
                if no or text:
                    cleaned.append({"no": no, "text": text})
            elif isinstance(item, str):
                text = self.clean_text(item)
                if text:
                    cleaned.append({"no": "", "text": text})
        return cleaned

    def clean_records(self):
        """Clean individual records"""
        cleaned = []

        for record in self.raw_data:
            cleaned_record = {
                "reg_id": self.clean_text(record.get("reg_id", "")),
                "title": self.clean_text(record.get("title") or record.get("name", "")),
                "doc_no": self.clean_text(record.get("doc_no") or record.get("wh", "")),
                "code_id": self.clean_text(record.get("code_id", "")),
                "category": self.clean_text(record.get("category", "")),
                "level": self.standardize_level(record.get("level", "")),
                "issuing_body": self.clean_text(record.get("issuing_body", "")),
                "effective_date": self.clean_text(record.get("effective_date", "")),
                "publish_date": self.clean_text(record.get("publish_date", "")),
                "articles": self.clean_articles(record.get("articles", [])),
                "source": record.get("source", "unknown"),
                "url": record.get("url", ""),
                "crawled_at": record.get("crawled_at", ""),
            }

            # Only keep records with at least a title or 法规 ID
            if cleaned_record["title"] or cleaned_record["reg_id"]:
                cleaned.append(cleaned_record)
                self.stats["cleaned"] += 1

        self.cleaned_data = cleaned
        logger.info(f"[CLEAN] {len(cleaned)} records cleaned")

    def generate_statistics(self):
        """Generate data statistics"""
        if not self.cleaned_data:
            logger.warning("[WARN] No cleaned data to analyze")
            return

        # Count by source
        source_counter = Counter(d["source"] for d in self.cleaned_data)

        # 按效力层级统计
        level_counter = Counter(d["level"] for d in self.cleaned_data if d["level"])

        # 按分类统计
        category_counter = Counter(d["category"] for d in self.cleaned_data if d["category"])

        # 条文总数
        all_articles = []
        for d in self.cleaned_data:
            all_articles.extend(d["articles"])

        # Field coverage
        fields = ["doc_no", "category", "level", "issuing_body", "effective_date", "articles"]
        coverage = {}
        for field in fields:
            count = sum(1 for d in self.cleaned_data if d.get(field))
            coverage[field] = f"{count}/{len(self.cleaned_data)} ({count/len(self.cleaned_data)*100:.1f}%)"

        # Print statistics
        logger.info("=" * 60)
        logger.info("[STATS] Data Statistics:")
        logger.info(f"  Total records: {len(self.cleaned_data)}")
        logger.info(f"  By source: {dict(source_counter)}")
        logger.info(f"  Unique levels: {len(level_counter)}")
        logger.info(f"  Unique categories: {len(category_counter)}")
        logger.info(f"  Total articles: {len(all_articles)}")
        logger.info(f"  Top 10 levels: {level_counter.most_common(10)}")
        logger.info(f"  Top 10 categories: {category_counter.most_common(10)}")
        logger.info(f"  Field coverage: {coverage}")
        logger.info("=" * 60)

        return {
            "total": len(self.cleaned_data),
            "by_source": dict(source_counter),
            "unique_levels": len(level_counter),
            "unique_categories": len(category_counter),
            "total_articles": len(all_articles),
            "top_levels": level_counter.most_common(10),
            "top_categories": category_counter.most_common(10),
            "field_coverage": coverage,
        }

    def save_json(self):
        """Save cleaned data as JSON"""
        try:
            output = {
                "metadata": {
                    "processed_at": datetime.now().isoformat(),
                    "total_regulations": len(self.cleaned_data),
                    "stats": self.stats,
                },
                "regulations": self.cleaned_data,
            }

            with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

            logger.info(f"[SAVED] JSON saved to {OUTPUT_JSON}")

        except Exception as e:
            logger.error(f"[ERROR] Failed to save JSON: {e}")

    def save_excel(self):
        """Save cleaned data as Excel for manual review"""
        if pd is None:
            logger.warning("[WARN] pandas 未安装，跳过 Excel 导出")
            return
        try:
            # Convert to DataFrame
            df = pd.DataFrame(self.cleaned_data)

            # Convert list fields to strings for Excel
            if "articles" in df.columns:
                df["articles"] = df["articles"].apply(
                    lambda x: "\n".join(
                        (a.get("no", "") + a.get("text", "")) if isinstance(a, dict) else str(a)
                        for a in (x or [])
                    )
                )

            # Save to Excel
            with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Regulations")

            logger.info(f"[SAVED] Excel saved to {OUTPUT_XLSX}")

        except Exception as e:
            logger.error(f"[ERROR] Failed to save Excel: {e}")

    def process(self):
        """Run the full processing pipeline"""
        logger.info("=" * 60)
        logger.info("[START] Data Processing Pipeline")
        logger.info("=" * 60)

        # Step 1: Load raw data
        if not self.load_raw_data():
            return False

        # Step 2: Remove duplicates
        self.remove_duplicates()

        # Step 3: Clean records
        self.clean_records()

        # Step 4: Generate statistics
        stats = self.generate_statistics()

        # Step 5: Save outputs
        self.save_json()
        self.save_excel()

        logger.info("=" * 60)
        logger.info("[COMPLETE] Data Processing Summary:")
        logger.info(f"  Raw records: {self.stats['total_raw']}")
        logger.info(f"  Duplicates removed: {self.stats['duplicates_removed']}")
        logger.info(f"  Empty removed: {self.stats['empty_removed']}")
        logger.info(f"  Cleaned records: {self.stats['cleaned']}")
        logger.info("=" * 60)

        return True


def main():
    """Main entry point"""
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="企业合规审查 - 法规数据清洗器（原始 JSON → 清洗 JSON + Excel）"
    )
    parser.parse_args()

    processor = DataProcessor()
    success = processor.process()

    if success:
        print("\n[SUCCESS] Data processing completed!")
        print(f"  JSON output: {OUTPUT_JSON}")
        print(f"  Excel output: {OUTPUT_XLSX}")
        print(f"  Log file: {log_dir / 'data_processor.log'}")
    else:
        print("\n[FAILED] Data processing failed. Check logs for details.")


if __name__ == "__main__":
    main()
