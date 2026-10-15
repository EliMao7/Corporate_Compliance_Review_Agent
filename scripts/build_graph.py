"""
构建合规知识图谱（薄封装）。

依次执行：
    python -m src.knowledge_graph.graph_builder clear --confirm
    python -m src.knowledge_graph.graph_builder build

clear+build 是 TDD 第 0 章 R5 的标准重建姿势：契约文件改动后必须先清空再导入，
避免旧标签/旧关系残留（数据是派生物，不入库）。

Usage:
    python scripts/build_graph.py
    python scripts/build_graph.py --no-clear     # 仅 build（增量，本地调试用）
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


def _run(module_args: list[str]) -> int:
    """在当前解释器下执行 `python -m src.knowledge_graph.graph_builder <args>`。"""
    cmd = [sys.executable, "-m", "src.knowledge_graph.graph_builder", *module_args]
    print(f"[RUN] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT)).returncode


def main():
    parser = argparse.ArgumentParser(description="构建合规知识图谱（clear + build）")
    parser.add_argument("--no-clear", action="store_true",
                        help="跳过 clear，仅执行 build（增量导入）")
    args = parser.parse_args()

    print("=" * 60)
    print("合规知识图谱构建（clear + build）")
    print("=" * 60)

    if not args.no_clear:
        rc = _run(["clear", "--confirm"])
        if rc != 0:
            print(f"[ERROR] clear 失败（退出码 {rc}）")
            sys.exit(rc)

    rc = _run(["build"])
    if rc != 0:
        print(f"[ERROR] build 失败（退出码 {rc}）")
        sys.exit(rc)

    print("[SUCCESS] 知识图谱构建完成")


if __name__ == "__main__":
    main()
