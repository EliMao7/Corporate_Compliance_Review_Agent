"""
System Integration Smoke Test
依次验证四个 API 端点的核心链路：
  GET  /api/health          —— 服务与依赖就绪
  GET  /api/stats           —— 知识图谱统计可读
  POST /api/consult         —— 非流式合规审查（含免责声明校验）
  POST /api/consult/stream  —— SSE 流式审查（progress≥1 + answer + done 帧序）

用法：
    python scripts/integration_test.py                    # 默认 http://localhost:8000
    python scripts/integration_test.py --base-url http://127.0.0.1:8000
    python scripts/integration_test.py --skip-consult     # 跳过消耗 LLM token 的两个审查端点

前置：已启动 API（python scripts/start_api.py）、Neo4j 与向量索引就绪。
退出码：0 = 全部通过；非 0 = 存在失败（可接入 CI）。
"""

import sys
import json
import time
import argparse
from pathlib import Path

_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests

PASS = "✅ PASS"
FAIL = "❌ FAIL"

_results: list[tuple[str, bool, str]] = []

# 访问令牌（TDD §6.4：除 /api/health 外所有端点都要求 Bearer）
_TOKEN: str = ""


def record(name: str, ok: bool, detail: str = ""):
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def _auth_headers() -> dict:
    """带令牌的请求头；无令牌时返回空 dict（健康检查等开放端点用）。"""
    return {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}


def auth(base: str, username: str, password: str) -> bool:
    """登录取令牌；账号不存在则先注册（注册成功即签发令牌）。

    换库重跑时账号可能已存在，因此登录 401 后转注册；注册 409 视为「已存在」，
    继续走登录，保证脚本可重复执行。
    """
    global _TOKEN
    try:
        r = requests.post(f"{base}/api/auth/login",
                          json={"username": username, "password": password}, timeout=10)
        if r.status_code == 401:
            r = requests.post(f"{base}/api/auth/register",
                              json={"username": username, "password": password}, timeout=10)
            if r.status_code == 409:  # 已存在但密码不同：仍按密码错误处理
                r = requests.post(f"{base}/api/auth/login",
                                  json={"username": username, "password": password}, timeout=10)
        if r.status_code not in (200, 201):
            record("认证 /api/auth", False, f"HTTP {r.status_code}: {r.text[:120]}")
            return False
        _TOKEN = r.json()["access_token"]
        record("认证 /api/auth", True, f"user={username}")
        return True
    except Exception as e:
        record("认证 /api/auth", False, f"连接失败: {e}（请先启动 API）")
        return False


def test_health(base: str):
    try:
        r = requests.get(f"{base}/api/health", timeout=10)
        body = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and body.get("agent_system_ready")
        record("GET /api/health", ok,
               f"status={body.get('status')} neo4j={body.get('neo4j_connected')} "
               f"vector={body.get('vector_index_loaded')} agent={body.get('agent_system_ready')}")
    except Exception as e:
        record("GET /api/health", False, f"连接失败: {e}（请先启动 API）")


def test_stats(base: str):
    try:
        r = requests.get(f"{base}/api/stats", timeout=10, headers=_auth_headers())
        data = r.json() if r.status_code == 200 else {}
        kg = data.get("knowledge_graph", {})
        vi = data.get("vector_index", {})
        ok = r.status_code == 200 and "error" not in kg and kg.get("total_nodes", 0) > 0
        record("GET /api/stats", ok,
               f"nodes={kg.get('total_nodes')} relations={kg.get('total_relations')} "
               f"vectors={vi.get('total_vectors')}")
    except Exception as e:
        record("GET /api/stats", False, str(e))


def test_consult(base: str, query: str):
    try:
        t0 = time.time()
        r = requests.post(f"{base}/api/consult",
                          json={"query": query, "session_id": "integration_test"},
                          timeout=90, headers=_auth_headers())
        d = r.json() if r.status_code == 200 else {}
        # 合规断言：任何回答都必须带免责声明
        ok = (r.status_code == 200 and d.get("answer") and d.get("disclaimers"))
        record("POST /api/consult", bool(ok),
               f"intent={d.get('intent')} answer={len(d.get('answer', ''))}chars "
               f"risk_signals={len(d.get('risk_signals', []))} "
               f"disclaimers={len(d.get('disclaimers', []))} "
               f"linked={len(d.get('linked_entities', []))} "
               f"duration={time.time() - t0:.1f}s")
    except Exception as e:
        record("POST /api/consult", False, str(e))


def test_consult_stream(base: str, query: str):
    """SSE 流式端点：断言帧序 progress≥1 + answer=1 + done=1 + error=0。

    帧解析假设：本协议每帧恰为一行 event: + 一行 data:（后端 _sse_format 保证
    JSON 数据单行），故逐行解析、data 行到达即派发，无需等待空行分帧。
    """
    events = {"progress": 0, "answer": 0, "error": 0, "done": 0}
    answer = {}
    t0 = time.time()
    try:
        with requests.post(f"{base}/api/consult/stream",
                           json={"query": query, "session_id": "integration_test_stream"},
                           stream=True, timeout=90, headers=_auth_headers()) as r:
            if r.status_code != 200:
                record("POST /api/consult/stream", False, f"HTTP {r.status_code}: {r.text[:100]}")
                return
            event = "message"
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:"):
                    data = raw[5:].strip()
                    if event in events:
                        events[event] += 1
                    if event == "answer":
                        try:
                            answer = json.loads(data)
                        except json.JSONDecodeError:
                            pass
                    event = "message"  # 派发后复位
        ok = (events["progress"] >= 1 and events["answer"] == 1
              and events["done"] == 1 and events["error"] == 0 and answer.get("answer"))
        record("POST /api/consult/stream", ok,
               f"progress={events['progress']} answer={events['answer']} "
               f"done={events['done']} error={events['error']} "
               f"intent={answer.get('intent')} duration={time.time() - t0:.1f}s")
    except Exception as e:
        record("POST /api/consult/stream", False, str(e))


def main():
    ap = argparse.ArgumentParser(description="系统集成冒烟测试")
    ap.add_argument("--base-url", default="http://localhost:8000", help="API 基地址")
    ap.add_argument("--query", default="我们准备发一份《供应商管理办法》，有没有跟法规冲突的地方？",
                    help="测试问题")
    ap.add_argument("--skip-consult", action="store_true",
                    help="跳过两个消耗 LLM token 的审查端点（仅测 health/stats）")
    ap.add_argument("--username", default="integration_test",
                    help="测试账号（不存在时自动注册）")
    ap.add_argument("--password", default="integration_test_2026",
                    help="测试账号密码（至少 6 位）")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print("=" * 60)
    print(f"INTEGRATION SMOKE TEST → {base}")
    print("=" * 60)

    auth(base, args.username, args.password)
    test_health(base)
    test_stats(base)
    if not args.skip_consult:
        test_consult(base, args.query)
        test_consult_stream(base, args.query)
    else:
        print("  （--skip-consult：跳过审查端点）")

    failed = [n for n, ok, _ in _results if not ok]
    print("=" * 60)
    if failed:
        print(f"[FAILED] {len(failed)} 项未通过：{failed}")
        sys.exit(1)
    print(f"[SUCCESS] 全部 {len(_results)} 项通过")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
