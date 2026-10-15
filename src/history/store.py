"""
对话历史存储（冷热分层）
Redis 热缓存（最近 12 轮会话上下文，TTL 24h）+ MySQL 8.0 冷存储（全量历史，唯一事实源）。

设计说明（与 §12.1 契约一致）：
- 读路径（三级命中）：先读 Redis `sess:{sid}` → 未命中从 MySQL 取最近 12 轮并回填 Redis → 返回。
- 写路径（双写容错）：**先写 MySQL（事实源）再写 Redis**；MySQL 失败则本轮不落 Redis
  （保证"缓存里绝不出现数据库没有的幻影数据"）；Redis 失败仅记日志降级，不影响主链路。
- Redis List 语义：LPUSH 插头（最新在头）+ LTRIM 保留 12 轮 + EXPIRE 24h；
  读出后反序即时间正序。
- 连接管理：Redis 用进程级连接池单例；MySQL 每请求新开、用完即关
  （历史接口为 def 同步路由，FastAPI 自动放入线程池，禁止跨线程共享连接）。
- `session_id == conversation_id`：一个 id 串起前端会话、Redis 热缓存、MySQL 冷存储。
- 会话归属（§6.4 / §12.4）：对话绑定 `user_id`，读取/删除他人会话一律 404。
- 滚动摘要（§12.2）：注入模型的近期历史有 token 预算上限，预算之外的较早历史压缩成
  滚动摘要，摘要**强制保留** 文件类型 / 涉及事项 / 高风险信号（一旦出现过必须保留）。
"""

import json
from datetime import datetime

import mysql.connector
import redis

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "history.log")

HOT_TURNS = 12                       # 热缓存保留轮次（与 §12.1 契约一致）
HOT_TTL_SECONDS = 24 * 3600          # 热缓存 TTL：24h（滑动续期）
TITLE_MAX_LEN = 24                   # 对话标题截断长度

# 注入模型的近期历史 token 预算（超预算的较早历史压缩成滚动摘要，§12.2）
CONTEXT_TOKEN_BUDGET = 2000

# 滚动摘要的**强制保留项**（合规域关键信息，§12.2）：
#   文件类型（管理办法 / 合同 / 公告…）
#   涉及事项（行政许可 / 招标投标 / 劳动用工…）
#   高风险信号（一旦出现过，摘要必须保留）
SUMMARY_FORCED_FIELDS = {
    "doc_type": "文件类型",
    "matters": "涉及事项",
    "risk_signals": "高风险信号",
}


def _hot_key(session_id: str) -> str:
    return f"sess:{session_id}"


def _truncate(text: str, limit: int = TITLE_MAX_LEN) -> str:
    """截断对话标题。

    注：切片终点用小写参数——check_contracts 的 Cypher 关系名扫描器
    会把「[ 数字/冒号 + 大写标识符 ]」形式的切片误判为关系名引用。
    """
    return text[0:limit]


def _now() -> datetime:
    return datetime.now()


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按字符近似，仅用于历史预算裁剪）。"""
    return max(1, len(text) // 2)


class HistoryStore:
    """对话历史管理器：MySQL 冷存储 + Redis 热缓存"""

    def __init__(self):
        self._redis_pool: redis.Redis | None = None
        self._schema_ready = False

    # ============================================================
    # Redis 层（热缓存）
    # ============================================================

    def _get_redis(self) -> redis.Redis:
        """连接池单例（惰性创建）"""
        if self._redis_pool is None:
            self._redis_pool = redis.Redis.from_url(
                settings.redis_url, decode_responses=True
            )
        return self._redis_pool

    def get_hot(self, session_id: str) -> list[dict] | None:
        """读热缓存；Redis 不可用/未命中返回 None（调用方回源 MySQL）。"""
        try:
            items = self._get_redis().lrange(_hot_key(session_id), 0, HOT_TURNS - 1)
        except Exception as e:
            logger.warning(f"[HISTORY] Redis read failed, fallback to MySQL: {e}")
            return None
        if not items:
            return None
        turns = []
        for raw in reversed(items):  # LPUSH 插头 → 反序即时间正序
            try:
                turns.append(json.loads(raw))
            except Exception:
                continue
        return turns or None

    def append_hot(self, session_id: str, turn: dict) -> bool:
        """写热缓存；失败仅记日志返回 False（缓存可缺，不可幻）。"""
        try:
            r = self._get_redis()
            key = _hot_key(session_id)
            r.lpush(key, json.dumps(turn, ensure_ascii=False))
            r.ltrim(key, 0, HOT_TURNS - 1)
            r.expire(key, HOT_TTL_SECONDS)  # 滑动续期
            return True
        except Exception as e:
            logger.warning(f"[HISTORY] Redis write failed, degraded (MySQL is source of truth): {e}")
            return False

    def _backfill_hot(self, session_id: str, turns: list[dict]) -> None:
        """把 MySQL 回源的轮次按时间正序逐条 LPUSH，重建与 append_hot 相同的头部布局。"""
        try:
            r = self._get_redis()
            key = _hot_key(session_id)
            for t in turns:
                r.lpush(key, json.dumps(t, ensure_ascii=False))
            r.expire(key, HOT_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[HISTORY] Redis backfill failed, degraded: {e}")

    def delete_hot(self, session_id: str) -> None:
        try:
            self._get_redis().delete(_hot_key(session_id))
        except Exception as e:
            logger.warning(f"[HISTORY] Redis delete failed: {e}")

    # ============================================================
    # MySQL 层（冷存储，唯一事实源）
    # ============================================================

    def _bootstrap_conn(self):
        """不带库连接，用于建库建表（首次使用自动执行）。"""
        return mysql.connector.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )

    def _conn(self):
        """每请求新开的业务连接（用完即关，禁止跨线程共享）。"""
        if not self._schema_ready:
            self._ensure_schema()
        return mysql.connector.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )

    def _ensure_schema(self) -> None:
        """建库 + 建表（IF NOT EXISTS，幂等）。"""
        conn = self._bootstrap_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"USE `{settings.mysql_database}`")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            VARCHAR(64)  PRIMARY KEY,
                    username      VARCHAR(64)  NOT NULL,
                    password_hash VARCHAR(128) NOT NULL,
                    created_at    DATETIME     NOT NULL,
                    UNIQUE KEY uk_username (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id         VARCHAR(64)  PRIMARY KEY,
                    user_id    VARCHAR(64)  NOT NULL DEFAULT '',
                    title      VARCHAR(64)  NOT NULL DEFAULT '',
                    created_at DATETIME     NOT NULL,
                    updated_at DATETIME     NOT NULL,
                    INDEX idx_user_updated (user_id, updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    conversation_id VARCHAR(64)  NOT NULL,
                    role            VARCHAR(16)  NOT NULL,
                    content         MEDIUMTEXT   NOT NULL,
                    intent          VARCHAR(32)  NOT NULL DEFAULT '',
                    created_at      DATETIME     NOT NULL,
                    INDEX idx_conv_id (conversation_id, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            # 兼容旧库：若 conversations 表已存在但缺 user_id 列，补上（幂等）
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'conversations' AND column_name = 'user_id'",
                (settings.mysql_database,),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "ALTER TABLE conversations ADD COLUMN user_id VARCHAR(64) NOT NULL DEFAULT ''"
                )
            conn.commit()
            cur.close()
            self._schema_ready = True
            logger.info("[HISTORY] MySQL schema ensured (users / conversations / messages)")
        finally:
            conn.close()

    # ---------- 用户（§6.4） ----------

    def create_user(self, user_id: str, username: str, password_hash: str) -> None:
        """新建用户（密码只存不可逆哈希）。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (id, username, password_hash, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, username, password_hash, _now()),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> dict | None:
        """按用户名取用户（含 password_hash，仅供登录校验使用）。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username, password_hash FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            cur.close()
            return row
        finally:
            conn.close()

    def get_user_by_id(self, user_id: str) -> dict | None:
        """按 ID 取用户（不含哈希）。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            cur.close()
            return row
        finally:
            conn.close()

    # ---------- 读 ----------

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """本人对话列表，按 updated_at 倒序（LIMIT 分页）。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
            cur.close()
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    def conversation_exists(self, conversation_id: str, user_id: str | None = None) -> bool:
        """会话是否存在；给定 user_id 时仅当归属该用户才算存在（越权 → 视为不存在 → 404）。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            if user_id is None:
                cur.execute("SELECT 1 FROM conversations WHERE id = %s", (conversation_id,))
            else:
                cur.execute(
                    "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id),
                )
            row = cur.fetchone()
            cur.close()
            return row is not None
        finally:
            conn.close()

    def get_messages(self, conversation_id: str) -> list[dict]:
        """该对话全部消息，按写入顺序（id 正序）。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT role, content, intent, created_at FROM messages "
                "WHERE conversation_id = %s ORDER BY id ASC",
                (conversation_id,),
            )
            rows = cur.fetchall()
            cur.close()
            return [
                {
                    "role": r["role"],
                    "content": r["content"],
                    "intent": r["intent"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ---------- 写 ----------

    def insert_message(self, conversation_id: str, role: str, content: str,
                       intent: str = "", user_id: str = "") -> None:
        """写入一条消息；conversation 不存在则自动创建（title = 首条内容截断，绑定属主）。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            now = _now()
            cur.execute("SELECT id FROM conversations WHERE id = %s", (conversation_id,))
            if cur.fetchone() is None:
                title = _truncate(content)
                cur.execute(
                    "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (conversation_id, user_id or "", title, now, now),
                )
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, intent, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, role, content, intent or "", now),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now, conversation_id),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_conversation(self, conversation_id: str, user_id: str | None = None) -> None:
        """删除对话及全部消息：清 MySQL 两表 + DEL 对应 Redis key（可限定属主）。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            if user_id is not None and not self.conversation_exists(conversation_id, user_id):
                return
            cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
            cur.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.delete_hot(conversation_id)

    # ============================================================
    # 统一入口
    # ============================================================

    def persist_turn(self, session_id: str, role: str, content: str,
                     intent: str = "", user_id: str = "", meta: dict | None = None) -> bool:
        """双写一轮：先 MySQL（事实源）再 Redis（缓存）。

        MySQL 失败 → 本轮同样不落 Redis（防幻影数据），记错误日志，返回 False；
        MySQL 成功、Redis 失败 → 仅降级（读路径未命中会回源），返回 True。

        meta 可携带本轮的结构化要素（doc_type / matters / risk_signals），
        供滚动摘要强制保留（§12.2）；写入 Redis 热缓存时一并保存。
        """
        turn = {"role": role, "content": content}
        if intent:
            turn["intent"] = intent
        if meta:
            for key in SUMMARY_FORCED_FIELDS:
                value = meta.get(key)
                if value:
                    turn[key] = value
        try:
            self.insert_message(session_id, role, content, intent, user_id=user_id)
        except Exception as e:
            logger.error(f"[HISTORY] MySQL write failed (source of truth), skip Redis for this turn: {e}")
            return False
        self.append_hot(session_id, turn)
        return True

    def load_context(self, session_id: str) -> list[dict]:
        """三级命中读取多轮上下文：Redis → MySQL 回填 → 空列表。"""
        turns = self.get_hot(session_id)
        if turns is not None:
            return turns
        rows = self.get_messages(session_id)
        if not rows:
            return []
        turns = [
            {k: v for k, v in (
                ("role", r["role"]), ("content", r["content"]), ("intent", r["intent"])
            ) if v}
            for r in rows
        ][-HOT_TURNS:]
        self._backfill_hot(session_id, turns)
        logger.info(f"[HISTORY] Context backfilled from MySQL: session={session_id} turns={len(turns)}")
        return turns

    def build_rolling_summary(self, turns: list[dict],
                              token_budget: int = CONTEXT_TOKEN_BUDGET) -> dict:
        """按 token 预算裁剪历史并生成滚动摘要（§12.2）。

        从最新一轮往前保留到预算耗尽；预算之外的较早历史压缩成摘要文本，
        摘要**强制保留** 文件类型 / 涉及事项 / 高风险信号（一旦出现过必须保留）。

        返回 {"recent": [近期轮次], "summary": str}。
        """
        if not turns:
            return {"recent": [], "summary": ""}

        recent: list[dict] = []
        used = 0
        split_index = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            cost = _estimate_tokens(str(turns[i].get("content", "")))
            if used + cost > token_budget and recent:
                break
            used += cost
            recent.insert(0, turns[i])
            split_index = i

        older = turns[:split_index]
        if not older:
            return {"recent": recent, "summary": ""}

        forced = {key: [] for key in SUMMARY_FORCED_FIELDS}
        for t in older:
            for key in SUMMARY_FORCED_FIELDS:
                value = t.get(key)
                if not value:
                    continue
                values = value if isinstance(value, list) else [value]
                for v in values:
                    if v and v not in forced[key]:
                        forced[key].append(v)

        parts = [
            f"{label}：{'、'.join(forced[key])}"
            for key, label in SUMMARY_FORCED_FIELDS.items()
            if forced[key]
        ]
        summary = "【较早对话摘要】" + ("；".join(parts) if parts else "无关键要素")
        return {"recent": recent, "summary": summary}


# 全局单例
history_store = HistoryStore()
