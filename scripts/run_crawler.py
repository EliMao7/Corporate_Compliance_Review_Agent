"""
运行法规采集器（薄封装）。

转发到 crawler/regulation_crawler.py，采集公开法规数据（国家法律法规数据库为主），
输出 data/raw/regulations.json，供下游清洗/抽取/建图。

Usage:
    python scripts/run_crawler.py
    python scripts/run_crawler.py --max 500      # 最多采集 N 条（透传给爬虫）
    python scripts/run_crawler.py --reset        # 清空断点重新采集
"""

import sys
import argparse
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

CRAWLER = PROJECT_ROOT / "crawler" / "regulation_crawler.py"


def main():
    parser = argparse.ArgumentParser(description="运行合规法规采集器")
    parser.add_argument("--max", type=int, default=200, help="最多采集的法规条数（默认 200）")
    parser.add_argument("--reset", action="store_true", help="清空断点重新采集")
    args = parser.parse_args()

    cmd = [sys.executable, str(CRAWLER), "--max", str(args.max)]
    if args.reset:
        cmd.append("--reset")

    print("=" * 60)
    print("法规采集（crawler/regulation_crawler.py）")
    print("=" * 60)
    print(f"[RUN] {' '.join(cmd)}")

    rc = subprocess.run(cmd, cwd=str(PROJECT_ROOT)).returncode
    if rc != 0:
        print(f"[ERROR] 采集失败（退出码 {rc}）")
        sys.exit(rc)

    print("[SUCCESS] 法规采集完成")


if __name__ == "__main__":
    main()
