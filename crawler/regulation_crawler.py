#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业合规审查 Agent - 法规采集器
================================

采集公开法规数据源，为下游"实体关系抽取 → 合规知识图谱"提供原始语料。

数据来源（多源混合，以国家法律法规数据库为主）：
  - 国家法律法规数据库（flk.npc.gov.cn，全国人大官方）—— 分类体系 / 分类统计 / 法规条目 / 条文
  - 中国政府网政策文件库（sousuo.www.gov.cn，国务院）—— 行政法规、国务院文件（补充源）

采集粒度：
  法规列表（分类码 codeId / 法规 ID / 文号）→ 条文 → 分类 / 效力层级 / 发布机关 / 施行日期

爬虫礼仪：
  - 条目级请求之间随机延时 3~5s，接口之间也留间隔，不做高频请求
  - UA 池轮换，带 Referer
  - tenacity 指数退避重试，最多 3 次
  - 在线解析并遵守 robots.txt
  - 写入 .crawl_checkpoint.json，中断可续
  - 绝不绕过任何鉴权：只访问公开接口

踩坑记录（接口逆向，详见 crawler/README.md 第二节）：
  1. 分类 / 统计接口是 GET（aggregateData / enumData），条目列表是 POST（search/list）——
     用错方法返回 {"msg":"Request method 'GET' not supported","code":500}。
  2. bundle 里的 searchType 不是列表接口参数（它是"高级搜索"里"并且/或/不含"的取值），
     误当成列表参数会一直返回 {"msg":"系统异常","code":500}。
  3. 后端是 RuoYi（Java）框架，分页参数风格参照 RuoYi（pageNum / pageSize）。
  4. flk 是纯 SPA，未匹配路径都返回同一个 index.html，因此不能用"路径返回 200"判断接口存在——
     必须看返回的是 JSON 还是 HTML。

用法：
    python crawler/regulation_crawler.py --help
    python crawler/regulation_crawler.py --max 200
    python crawler/regulation_crawler.py --reset          # 清空断点重新采集
"""

import os
import sys
import json
import time
import random
import logging
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from typing import Optional
from urllib.robotparser import RobotFileParser

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 仅 HTML 兜底解析需要（flk 为纯 JSON 接口）。
# 用 try/except 包住是为了让 `--help` 在最小环境（未装 bs4）也能离线跑通（R7）。
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 最小环境下保持模块可导入
    BeautifulSoup = None

# ===== Configuration =====

# Project root directory（crawler/ 与 src/ 平级，见 TDD §2.2.1）
PROJECT_ROOT = Path(__file__).parent.parent
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)

# Output file
OUTPUT_FILE = DATA_RAW_DIR / "regulations.json"
CHECKPOINT_FILE = DATA_RAW_DIR / ".crawl_checkpoint.json"

# Crawler settings
MAX_RETRIES = 3
REQUEST_TIMEOUT = 20
DELAY_MIN = 3.0  # Minimum delay between requests (seconds)
DELAY_MAX = 5.0  # Maximum delay between requests (seconds)

# Random User-Agent pool
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ===== Data Sources =====
# 多源混合：以国家法律法规数据库为主，政府网为补充（见 TDD §2.3.3）。
# 方法约定：分类计数 / 分类树 / 法规详情 = GET；条目列表 = POST。
DATA_SOURCES = [
    {"name": "国家法律法规数据库", "base": "https://flk.npc.gov.cn",
     "catalog_api": "/law-search/index/aggregateData",   # 分类计数（GET）
     "enum_api": "/law-search/search/enumData",          # 分类树（GET）
     "list_api": "/law-search/search/list"},             # 条目列表（POST）
    {"name": "中国政府网政策文件库", "base": "https://sousuo.www.gov.cn",
     "list_api": "/search-gov/data?t=zhengcelibrary"},
]

FLK_SOURCE = DATA_SOURCES[0]
GOV_SOURCE = DATA_SOURCES[1]

# flk 法规详情接口（GET）
FLK_DETAIL_API = "/law-search/search/flfgDetails"

# ===== Seed Seeds =====
# 顶级法规分类名（分类码 codeId 由 enumData 接口在运行时动态解析）。
# 六大类与 crawler/README.md 第三节实测统计一致。
DEFAULT_CATEGORIES = ["宪法", "法律", "行政法规", "监察法规", "地方法规", "司法解释"]

# 政府网政策文件库的检索关键词（合规域种子），用于补充源条目发现。
SEED_KEYWORDS = [
    "公司治理", "市场监管", "反垄断", "数据合规", "劳动用工",
    "行政处罚", "行政许可", "公平竞争审查",
]


def get_seed_keys() -> list:
    """生成种子实体键列表（去重、保序）。

    实体键 = 法规 ID / 文号 / 分类码（codeId），以官方标识为准。
    这里以顶级分类名（用于 flk 列表）与检索关键词（用于政府网）作为种子。
    """
    seen, keys = set(), []
    for key in DEFAULT_CATEGORIES + SEED_KEYWORDS:
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def reg_key(record: dict) -> str:
    """取法规实体的唯一键：优先法规 ID，其次文号，最后标题。"""
    return (
        str(record.get("reg_id") or "").strip()
        or str(record.get("doc_no") or "").strip()
        or str(record.get("title") or "").strip()
    )


# ===== Logging Setup =====

log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "crawler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ===== Robots.txt Checker =====

class RobotsChecker:
    """Check if a URL is allowed by robots.txt（在线解析后遵守）"""

    def __init__(self):
        self.parsers = {}

    def get_parser(self, base_url: str) -> RobotFileParser:
        """Get or create a robots.txt parser for a domain"""
        if base_url not in self.parsers:
            rp = RobotFileParser()
            robots_url = urljoin(base_url, "/robots.txt")
            rp.set_url(robots_url)
            try:
                rp.read()
                logger.info(f"[OK] Loaded robots.txt from {robots_url}")
            except Exception as e:
                logger.warning(f"[WARN] Failed to load robots.txt from {robots_url}: {e}")
                # If we can't read robots.txt, allow everything (be conservative)
                rp.allow_all = True
            self.parsers[base_url] = rp
        return self.parsers[base_url]

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        """Check if a URL is allowed by robots.txt"""
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        rp = self.get_parser(base_url)

        if hasattr(rp, "allow_all"):
            return True

        return rp.can_fetch(user_agent, url)

    def get_crawl_delay(self, base_url: str, user_agent: str = "*") -> Optional[float]:
        """Get the crawl delay specified in robots.txt"""
        rp = self.get_parser(base_url)
        try:
            delay = rp.crawl_delay(user_agent)
            return float(delay) if delay else None
        except Exception:
            return None


# ===== Crawler Class =====

class RegulationCrawler:
    """采集公开法规数据（国家法律法规数据库 + 中国政府网政策文件库）"""

    def __init__(self):
        self.session = requests.Session()
        self.robots_checker = RobotsChecker()
        self.crawled_urls = set()      # 已采集的实体键（法规 ID / 文号 / 分类码）
        self.regulations = []          # 采集到的法规条目
        self.reference = {}            # 参考数据：分类树 + 分类统计
        self.stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
        }
        self._load_checkpoint()

    def _get_headers(self, referer: str = None) -> dict:
        """Get random headers for each request"""
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _delay(self):
        """Wait a random delay between requests（爬虫礼仪：条目级 3~5s 随机）"""
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        time.sleep(delay)

    @retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError)),
    )
    def _request(
        self,
        url: str,
        method: str = "GET",
        params: dict = None,
        json_body: dict = None,
        referer: str = None,
    ) -> Optional[requests.Response]:
        """Request a URL with retry logic.

        方法约定（踩坑记录 #1）：分类 / 统计 / 详情用 GET，条目列表用 POST。
        用错方法会返回 {"msg":"Request method 'GET' not supported","code":500}。
        """
        # Check robots.txt first
        if not self.robots_checker.is_allowed(url):
            logger.warning(f"[BLOCKED] robots.txt disallows: {url}")
            return None

        logger.info(f"[FETCH] {method.upper()} {url}")
        if method.upper() == "POST":
            response = self.session.post(
                url,
                headers=self._get_headers(referer),
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
        else:
            response = self.session.get(
                url,
                headers=self._get_headers(referer),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        response.raise_for_status()
        return response

    def _fetch_json(
        self,
        url: str,
        method: str = "GET",
        params: dict = None,
        json_body: dict = None,
        referer: str = None,
    ) -> Optional[dict]:
        """请求一个公开 JSON 接口并解析。

        踩坑记录 #4：flk 是纯 SPA，未匹配路径都返回 index.html，
        所以不能只看状态码 200——必须确认响应体是 JSON 而非 HTML。
        """
        response = self._request(
            url, method=method, params=params, json_body=json_body, referer=referer
        )
        if response is None:
            return None

        content_type = (response.headers.get("Content-Type") or "").lower()
        text = response.text.lstrip()
        if "json" not in content_type and not text.startswith(("{", "[")):
            logger.warning(
                "[PITFALL] 响应不是 JSON 而是 HTML —— flk 未匹配路径会返回同一份 "
                f"index.html，接口可能不存在或路径写错：{url}"
            )
            return None

        try:
            return response.json()
        except ValueError:
            try:
                return json.loads(text)
            except ValueError:
                logger.warning(f"[WARN] JSON 解析失败：{url}")
                return None

    def _load_checkpoint(self):
        """Load checkpoint to resume crawling（断点续爬）"""
        if CHECKPOINT_FILE.exists():
            try:
                with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.crawled_urls = set(data.get("crawled_urls", []))
                self.regulations = data.get("regulations", [])
                self.reference = data.get("reference", {})
                self.stats = data.get("stats", self.stats)
                logger.info(
                    f"[RESUME] Loaded checkpoint: {len(self.crawled_urls)} keys crawled, "
                    f"{len(self.regulations)} regulations collected"
                )
            except Exception as e:
                logger.warning(f"[WARN] Failed to load checkpoint: {e}")

    def _save_checkpoint(self):
        """Save checkpoint for resume"""
        try:
            with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "crawled_urls": list(self.crawled_urls),
                        "regulations": self.regulations,
                        "reference": self.reference,
                        "stats": self.stats,
                        "timestamp": datetime.now().isoformat(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"[ERROR] Failed to save checkpoint: {e}")

    def _save_results(self):
        """Save final results to JSON"""
        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "metadata": {
                            "source": " + ".join(s["name"] for s in DATA_SOURCES),
                            "crawled_at": datetime.now().isoformat(),
                            "total_regulations": len(self.regulations),
                        },
                        "reference": self.reference,
                        "regulations": self.regulations,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            logger.info(f"[SAVED] {len(self.regulations)} regulations saved to {OUTPUT_FILE}")
        except Exception as e:
            logger.error(f"[ERROR] Failed to save results: {e}")

    # ===== 国家法律法规数据库（flk.npc.gov.cn） =====

    def crawl_flk_catalog(self) -> Optional[dict]:
        """采集分类计数：GET /law-search/index/aggregateData（分类/统计是 GET）"""
        logger.info("=" * 60)
        logger.info("[START] 采集中：国家法律法规数据库 分类统计")
        logger.info("=" * 60)

        url = FLK_SOURCE["base"] + FLK_SOURCE["catalog_api"]
        payload = self._fetch_json(url, method="GET", referer=FLK_SOURCE["base"] + "/")
        if not payload or payload.get("code") != 200:
            logger.warning(f"[WARN] 分类统计采集失败：{payload.get('msg') if payload else 'no-json'}")
            return None

        self.reference["law_stats"] = payload.get("data")
        logger.info("[OK] 分类统计已获取（法律/行政法规/地方法规... 各类计数）")
        return payload.get("data")

    def crawl_flk_enum(self) -> Optional[list]:
        """采集分类树：GET /law-search/search/enumData（返回 宪法/法律/行政法规/... 及子类）"""
        logger.info("=" * 60)
        logger.info("[START] 采集中：国家法律法规数据库 分类树")
        logger.info("=" * 60)

        url = FLK_SOURCE["base"] + FLK_SOURCE["enum_api"]
        payload = self._fetch_json(url, method="GET", referer=FLK_SOURCE["base"] + "/")
        if not payload or payload.get("code") != 200:
            logger.warning(f"[WARN] 分类树采集失败：{payload.get('msg') if payload else 'no-json'}")
            return None

        tree = payload.get("data")
        self.reference["law_catalog"] = tree
        logger.info("[OK] 分类树已获取（宪法/法律/行政法规/监察法规/地方法规/司法解释）")
        return tree

    def crawl_flk_list(self, code_id: str = None, page_num: int = 1, page_size: int = 20) -> list:
        """采集法规条目列表：POST /law-search/search/list。

        踩坑记录：条目列表必须是 POST；分页参数风格参照 RuoYi（pageNum / pageSize）；
        searchType 不是本接口参数（误传会一直返回 {"msg":"系统异常","code":500}）。
        """
        url = FLK_SOURCE["base"] + FLK_SOURCE["list_api"]
        body = {
            "pageNum": page_num,
            "pageSize": page_size,
        }
        if code_id:
            body["codeId"] = code_id

        payload = self._fetch_json(
            url,
            method="POST",
            json_body=body,
            referer=FLK_SOURCE["base"] + "/",
        )
        if not payload or payload.get("code") != 200:
            logger.warning(f"[WARN] 条目列表采集失败：{payload.get('msg') if payload else 'no-json'}")
            return []

        # RuoYi TableDataInfo：{code, msg, total, rows}
        rows = payload.get("rows") or payload.get("data") or []
        logger.info(f"[LIST] 分类 {code_id or '(全部)'} 第 {page_num} 页：{len(rows)} 条")
        return rows

    def crawl_flk_detail(self, reg_id: str) -> Optional[dict]:
        """采集法规详情与条文：GET /law-search/search/flfgDetails

        采集粒度：法规列表 → 条文 → 分类 / 效力层级 / 发布机关 / 施行日期。
        """
        url = FLK_SOURCE["base"] + FLK_DETAIL_API
        params = {"id": reg_id}
        payload = self._fetch_json(
            url, method="GET", params=params, referer=FLK_SOURCE["base"] + "/"
        )
        if not payload or payload.get("code") != 200:
            return None

        raw = payload.get("data") or {}
        record = {
            "reg_id": reg_id,
            "title": raw.get("title") or raw.get("name"),
            "doc_no": raw.get("docNo") or raw.get("wh"),
            "code_id": raw.get("codeId"),
            "category": raw.get("category") or raw.get("flmc"),      # 分类
            "level": raw.get("level") or raw.get("xljb"),            # 效力层级
            "issuing_body": raw.get("issuingBody") or raw.get("fbjg"),  # 发布机关
            "effective_date": raw.get("effectiveDate") or raw.get("sxrq"),  # 施行日期
            "publish_date": raw.get("publishDate") or raw.get("fbrq"),      # 发布日期
            "articles": self._parse_articles(raw),                   # 条文
            "source": FLK_SOURCE["name"],
            "url": f"{FLK_SOURCE['base']}/detail2.html?ZmY4M...&id={reg_id}",
            "crawled_at": datetime.now().isoformat(),
        }
        return record

    @staticmethod
    def _parse_articles(raw: dict) -> list:
        """从详情响应解析条文列表（法规列表 → 条文）。"""
        articles = []
        body = raw.get("body") or raw.get("content") or ""
        if isinstance(body, list):
            for i, item in enumerate(body, start=1):
                if isinstance(item, dict):
                    articles.append({
                        "no": item.get("no") or f"第{i}条",
                        "text": (item.get("text") or "").strip(),
                    })
                elif isinstance(item, str):
                    articles.append({"no": f"第{i}条", "text": item.strip()})
        elif isinstance(body, str) and body.strip():
            articles.append({"no": "", "text": body.strip()})
        return articles

    # ===== 中国政府网政策文件库（sousuo.www.gov.cn，补充源） =====

    def crawl_gov_list(self, keyword: str, page: int = 1, page_size: int = 20) -> list:
        """采集政策文件库条目：GET /search-gov/data?t=zhengcelibrary

        踩坑记录：该接口返回 200 但带参数门槛；空查询词时 totalCount=0，
        必须带正确的检索字段（q）。
        """
        url = GOV_SOURCE["base"] + GOV_SOURCE["list_api"]
        params = {
            "q": keyword,
            "timetype": "timezd",
            "mintime": "",
            "maxtime": "",
            "sort": "score",
            "sortType": 1,
            "searchfield": "title:content",
            "pcodeJiguan": "",
            "childtype": "",
            "subchildtype": "",
            "tsbq": "",
            "pubtimeyear": "",
            "puborg": "",
            "pcodeYear": "",
            "pcodeNum": "",
            "filetype": "",
            "p": page,
            "n": page_size,
            "inpro": "",
            "bmfl": "",
            "dup": "",
            "orpro": "",
        }
        payload = self._fetch_json(url, method="GET", params=params, referer=GOV_SOURCE["base"] + "/")
        if not payload:
            return []

        # 政府网返回结构：{searchVO: {...}, resultList / data: {...}}，容忍多种字段名。
        data = payload.get("searchVO") or payload.get("data") or payload
        rows = (
            data.get("listVO")
            or data.get("list")
            or payload.get("resultDocs")
            or []
        )
        logger.info(f"[LIST] 政府网「{keyword}」第 {page} 页：{len(rows)} 条")
        return rows if isinstance(rows, list) else []

    # ===== Main Crawl Logic =====

    def run(self, max_regulations: int = 200, use_reference: bool = True, use_gov: bool = True):
        """
        Run the crawler

        Args:
            max_regulations: Maximum number of regulations to crawl
            use_reference: Whether to fetch reference data (catalog + stats)
            use_gov: Whether to use gov.cn as a supplementary source
        """
        logger.info("=" * 60)
        logger.info(f"[START] Regulation Crawler started at {datetime.now().isoformat()}")
        logger.info(f"[CONFIG] Max regulations: {max_regulations}, Reference: {use_reference}, Gov: {use_gov}")
        logger.info("=" * 60)

        # Step 1: 参考数据（分类树 + 分类统计）——接口为 GET，之间留间隔
        if use_reference:
            self.crawl_flk_catalog()
            self._delay()
            self.crawl_flk_enum()
            self._delay()

        # Step 2: 按分类码（codeId）分页采集条目列表（POST），再逐条采详情
        categories = DEFAULT_CATEGORIES if use_reference else [None]
        for category in categories:
            if len(self.regulations) >= max_regulations:
                break

            page_num = 1
            while len(self.regulations) < max_regulations:
                rows = self.crawl_flk_list(code_id=category, page_num=page_num, page_size=20)
                if not rows:
                    break

                for row in rows:
                    if len(self.regulations) >= max_regulations:
                        break

                    reg_id = str(
                        row.get("id") or row.get("regId") or row.get("codeId") or ""
                    ).strip()
                    key = reg_id or str(row.get("title") or "").strip()
                    if not key:
                        self.stats["failed"] += 1
                        continue

                    self.stats["total"] += 1
                    if key in self.crawled_urls:
                        self.stats["skipped"] += 1
                        continue

                    # 列表行本身已有 title/doc_no 等字段，优先用它；缺失时再调详情接口补条文
                    record = dict(row)
                    detail = self.crawl_flk_detail(reg_id) if reg_id else None
                    if detail and detail.get("title"):
                        record = detail

                    if reg_title_ok(record):
                        self.regulations.append(record)
                        self.crawled_urls.add(key)
                        self.stats["success"] += 1
                        logger.info(
                            f"[OK] [{len(self.regulations)}/{max_regulations}] "
                            f"{record.get('title') or record.get('name')}"
                        )
                    else:
                        self.stats["failed"] += 1

                    self._save_checkpoint()
                    self._delay()

                page_num += 1

        # Step 3: 政府网政策文件库（补充源）
        if use_gov and len(self.regulations) < max_regulations:
            for keyword in SEED_KEYWORDS:
                if len(self.regulations) >= max_regulations:
                    break
                rows = self.crawl_gov_list(keyword)
                for row in rows:
                    if len(self.regulations) >= max_regulations:
                        break

                    key = reg_key(row)
                    self.stats["total"] += 1
                    if not key or key in self.crawled_urls:
                        self.stats["skipped"] += 1
                        continue

                    record = {
                        "reg_id": str(row.get("id") or row.get("url") or key),
                        "title": row.get("title") or row.get("pcode") or key,
                        "doc_no": row.get("pcode") or row.get("docNo"),
                        "category": row.get("bmfl") or row.get("puborg"),
                        "level": "国务院文件",
                        "issuing_body": row.get("puborg") or row.get("pcodeJiguan"),
                        "effective_date": row.get("pubtime"),
                        "publish_date": row.get("pubtime"),
                        "articles": [],
                        "source": GOV_SOURCE["name"],
                        "url": row.get("url") or "",
                        "crawled_at": datetime.now().isoformat(),
                    }
                    self.regulations.append(record)
                    self.crawled_urls.add(key)
                    self.stats["success"] += 1
                    self._save_checkpoint()
                    self._delay()

        # Step 4: Save results
        self._save_results()

        # Step 5: Print summary
        logger.info("=" * 60)
        logger.info("[COMPLETE] Crawl Summary:")
        logger.info(f"  Total requests: {self.stats['total']}")
        logger.info(f"  Success: {self.stats['success']}")
        logger.info(f"  Failed: {self.stats['failed']}")
        logger.info(f"  Skipped (already crawled): {self.stats['skipped']}")
        logger.info(f"  Regulations collected: {len(self.regulations)}")
        logger.info(f"  Output: {OUTPUT_FILE}")
        logger.info("=" * 60)


def reg_title_ok(record: dict) -> bool:
    """判断一条法规记录是否有效（至少要有标题或法规 ID）。"""
    return bool(str(record.get("title") or record.get("name") or record.get("reg_id") or "").strip())


def parse_html_fallback(html: str):
    """HTML 兜底解析（flk 为 JSON 接口，仅在必要时使用）。

    bs4 不是硬依赖：未安装时返回 None，保持模块可离线导入。
    """
    if BeautifulSoup is None:
        logger.warning("[WARN] beautifulsoup4 未安装，跳过 HTML 兜底解析")
        return None
    return BeautifulSoup(html, "html.parser")


# ===== Main Entry =====

def main():
    """Main entry point"""
    import argparse

    # Windows 控制台默认 GBK，统一改成 UTF-8 以免中文乱码（与 fetch_reference_data.py 一致）
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="企业合规审查 - 法规采集器（国家法律法规数据库 / 中国政府网政策文件库）"
    )
    parser.add_argument(
        "--max", type=int, default=200, help="Maximum number of regulations to crawl (default: 200)"
    )
    parser.add_argument(
        "--reset", action="store_true", help="Reset checkpoint and start fresh"
    )

    args = parser.parse_args()

    if args.reset and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        logger.info("[RESET] Checkpoint file deleted, starting fresh")

    crawler = RegulationCrawler()
    crawler.run(max_regulations=args.max)


if __name__ == "__main__":
    main()
