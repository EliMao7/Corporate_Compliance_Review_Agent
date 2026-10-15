#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
法规参考数据采集脚本
=====================

采集"国家法律法规数据库"（flk.npc.gov.cn）的公开分类与统计接口，
产出可复现、可指证的参考数据。

来源与接口（均从站点 JS bundle 逆向得到，非猜测）：
  - 分类树：GET  https://flk.npc.gov.cn/law-search/search/enumData
  - 分类统计：GET https://flk.npc.gov.cn/law-search/index/aggregateData

爬虫礼仪：
  - 只在接口间 sleep，不做高频请求
  - 带真实 UA 与 Referer
  - 不绕过任何鉴权；本脚本只访问公开 GET 接口

用法：
    python crawler/fetch_reference_data.py

产出：
    data/reference/law_catalog.json   法规分类树（宪法/法律/行政法规/... 及子类）
    data/reference/law_stats.json     各类法规数量统计
"""

import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT_DIR = ROOT / "data" / "reference"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://flk.npc.gov.cn/"}

ENDPOINTS = {
    "law_catalog": "https://flk.npc.gov.cn/law-search/search/enumData",
    "law_stats": "https://flk.npc.gov.cn/law-search/index/aggregateData",
}


def fetch_json(url: str) -> dict:
    """GET 一个公开 JSON 接口并解析。"""
    req = urllib.request.Request(url, headers=HEADERS)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    ok, fail = 0, 0

    for name, url in ENDPOINTS.items():
        try:
            payload = fetch_json(url)
            if payload.get("code") != 200:
                print(f"[WARN] {name}: 接口返回 code={payload.get('code')} msg={payload.get('msg')}")
                fail += 1
                continue

            doc = {
                "_meta": {
                    "source": "国家法律法规数据库（全国人大）",
                    "source_url": url,
                    "fetched_at": stamp,
                    "note": "本文件由 crawler/fetch_reference_data.py 采集，请勿手工编辑",
                },
                "data": payload.get("data"),
            }
            out = OUT_DIR / f"{name}.json"
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[OK] {name} -> {out.name} ({out.stat().st_size} B)")
            ok += 1
        except Exception as e:  # noqa: BLE001 - 采集失败不应中断另一个源
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            fail += 1
        time.sleep(3)  # 爬虫礼仪：接口之间留间隔

    print(f"\n完成：成功 {ok} / 失败 {fail}")
    return 1 if ok == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
