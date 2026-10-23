# -*- coding: utf-8 -*-
"""跨群用户记忆 —— SQLite + FTS5 存储层

设计要点：
- 主键是 **QQ 号**（不是会话），所以同一个人在 A 群、B 群说的话归到同一条记忆线
- 不用 embedding：DeepSeek 没有 embedding API，且要省调用成本
- 中文用「逐字切分」+ FTS5 unicode61 实现关键词检索
  （unicode61 不切中文，把「今天天气」当一个 token；逐字切分成「今 天 天 气」后
   就能用短语匹配任意子串，实测有效）
- FTS 用 external content 表，触发器保持同步，不额外存一份原文

线程模型：AstrBot 的 handler 都在同一个事件循环里，但为了安全所有读写都用
一把锁串行化，连接 check_same_thread=False。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 单独跑测试脚本时没有 astrbot
    import logging

    logger = logging.getLogger("user_memory")

# ---------------------------------------------------------------- 中文切分

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF      # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 基本区
        or 0xF900 <= code <= 0xFAFF   # 兼容区
        or 0x3040 <= code <= 0x30FF   # 日文假名
    )


_TOKEN_CHARS = set("_-'")


def segment_cjk(text: str) -> str:
    """把文本切成分词友好的形式：中文逐字、英文/数字按词。

    >>> segment_cjk("今天钓鱼go1")
    '今 天 钓 鱼 go1'
    """
    if not text:
        return ""
    parts: list[str] = []
    buf: list[str] = []

    def _flush():
        if buf:
            parts.append("".join(buf))
            buf.clear()

    for ch in text:
        if _is_cjk(ch):
            _flush()
            parts.append(ch)
        elif ch.isalnum() or ch in _TOKEN_CHARS:
            buf.append(ch)
        else:
            _flush()
    _flush()
    return " ".join(parts)


def _fts_tokens(text: str) -> list[str]:
    """把检索词切成安全 token。

    注意：只剔除会破坏 FTS5 语法的字符（双引号、括号、星号、冒号等），
    中文字符必须保留——它们本身就是 token。
    """
    out: list[str] = []
    for tok in segment_cjk(text).split():
        tok = tok.replace('"', "").replace("*", "")
        tok = re.sub(r"[\(\)\:\.\^\-]", "", tok)
        tok = tok.strip()
        if tok:
            out.append(tok)
    return out


# ---------------------------------------------------------------- 存储

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS users (
    qq          TEXT PRIMARY KEY,
    nickname    TEXT,
    first_seen  REAL,
    last_seen   REAL,
    msg_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_groups (
    qq         TEXT NOT NULL,
    group_id   TEXT NOT NULL,
    group_name TEXT,
    nickname   TEXT,
    first_seen REAL,
    last_seen  REAL,
    msg_count  INTEGER DEFAULT 0,
    PRIMARY KEY (qq, group_id)
);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    qq         TEXT NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_facts_qq ON facts(qq);

-- 好感度：机器人对群友的单向好感（0-100，初始 40）
CREATE TABLE IF NOT EXISTS affinity (
    qq          TEXT PRIMARY KEY,
    score       REAL NOT NULL DEFAULT 40,
    updated_at  REAL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    qq         TEXT NOT NULL,
    nickname   TEXT,
    group_id   TEXT,
    group_name TEXT,
    content    TEXT NOT NULL,
    seg        TEXT NOT NULL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_qq ON messages(qq, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    seg,
    content='messages',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, seg) VALUES (new.id, new.seg);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.id, old.seg);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.id, old.seg);
    INSERT INTO messages_fts(rowid, seg) VALUES (new.id, new.seg);
END;
"""

SCHEMA_VERSION = 3  # v3: 新增 affinity 好感度表

# 好感度取值范围与初始值
AFFINITY_MIN = 0.0
AFFINITY_MAX = 100.0
AFFINITY_DEFAULT = 40.0

# 好感度分档 → 行为倾向（给模型看的自然语言描述）
AFFINITY_TIERS = [
    (85, "非常亲近", "基本有求必应，撒娇、肉麻的要求也愿意配合，最多嘴上嘟囔一句"),
    (65, "很熟", "愿意配合大部分要求，可能会先嘴硬两句再照做"),
    (45, "一般熟", "看心情配合，会调侃，可能先反问或者开玩笑"),
    (25, "不太熟", "不太情愿配合，会敷衍、岔开话题或者装没听见"),
    (0, "没什么好感", "明显不配合，会敷衍、冷处理甚至直接回怼"),
]


def affinity_tier(score: float) -> tuple[str, str]:
    """好感度分数 → (档位名, 行为描述)。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = AFFINITY_DEFAULT
    for threshold, name, desc in AFFINITY_TIERS:
        if s >= threshold:
            return name, desc
    return AFFINITY_TIERS[-1][1], AFFINITY_TIERS[-1][2]


class MemoryStore:
    """一个插件实例持有一个 MemoryStore。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        old_version = self._read_schema_version()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self._writes_since_prune = 0
        # v1 → v2：清洗旧数据里的 @昵称(QQ号)
        if old_version < 2 and self._has_column_ratio_rows():
            try:
                n = self.migrate_clean_at_mentions()
                if n:
                    logger.info(f"[user_memory] 已清洗 {n} 条旧消息的 @ 提及")
            except Exception as e:
                logger.error(f"[user_memory] 清洗旧消息失败: {e}")

    def _read_schema_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _has_column_ratio_rows(self) -> bool:
        try:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE content LIKE '%(%)%' LIMIT 1"
            ).fetchone()
            return row is not None
        except Exception:
            return False

    # ---------------------------------------------------------- 内部
    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------- 写入
    def record_message(
        self,
        qq: str,
        nickname: str,
        group_id: str,
        group_name: str,
        content: str,
        created_at: float | None = None,
        max_per_user: int = 2000,
    ) -> bool:
        """记录一条发言。返回 True 表示真的入库了。"""
        qq = str(qq or "").strip()
        content = (content or "").strip()
        if not qq or not content:
            return False
        ts = float(created_at or time.time())
        seg = segment_cjk(content)
        if not seg:
            return False

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO messages(qq, nickname, group_id, group_name, content, seg, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (qq, nickname or "", group_id or "", group_name or "", content, seg, ts),
            )
            cur.execute(
                "INSERT INTO users(qq, nickname, first_seen, last_seen, msg_count)"
                " VALUES(?,?,?,?,1)"
                " ON CONFLICT(qq) DO UPDATE SET"
                "   nickname=excluded.nickname,"
                "   last_seen=excluded.last_seen,"
                "   msg_count=users.msg_count+1",
                (qq, nickname or "", ts, ts),
            )
            cur.execute(
                "INSERT INTO user_groups(qq, group_id, group_name, nickname, first_seen, last_seen, msg_count)"
                " VALUES(?,?,?,?,?,?,1)"
                " ON CONFLICT(qq, group_id) DO UPDATE SET"
                "   group_name=COALESCE(NULLIF(excluded.group_name,''), user_groups.group_name),"
                "   nickname=excluded.nickname,"
                "   last_seen=excluded.last_seen,"
                "   msg_count=user_groups.msg_count+1",
                (qq, group_id or "", group_name or "", nickname or "", ts, ts),
            )
            if max_per_user and max_per_user > 0:
                cur.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "  SELECT id FROM messages WHERE qq=? ORDER BY id DESC LIMIT -1 OFFSET ?"
                    ")",
                    (qq, int(max_per_user)),
                )
            self._commit()
            self._writes_since_prune += 1

        return True

    def add_fact(self, qq: str, content: str, source: str = "manual") -> int:
        qq = str(qq or "").strip()
        content = (content or "").strip()
        if not qq or not content:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts(qq, content, source, created_at) VALUES(?,?,?,?)",
                (qq, content, source, time.time()),
            )
            self._commit()
            return int(cur.lastrowid or 0)

    def delete_user(self, qq: str) -> int:
        qq = str(qq or "").strip()
        if not qq:
            return 0
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE qq=?", (qq,))
            n = cur.rowcount or 0
            self._conn.execute("DELETE FROM facts WHERE qq=?", (qq,))
            self._conn.execute("DELETE FROM user_groups WHERE qq=?", (qq,))
            self._conn.execute("DELETE FROM users WHERE qq=?", (qq,))
            self._commit()
            return n

    def prune_old(self, retention_days: int = 180) -> int:
        """删除超过保留期的发言。"""
        if not retention_days or retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            n = cur.rowcount or 0
            self._commit()
            return n

    def maybe_prune(self, retention_days: int, every: int = 300) -> int:
        """每 N 次写入才做一次过期清理，避免每条消息都扫全表。"""
        with self._lock:
            if self._writes_since_prune < every:
                return 0
            self._writes_since_prune = 0
        return self.prune_old(retention_days)

    def migrate_clean_at_mentions(self) -> int:
        """一次性数据迁移：把旧数据里的 `@昵称(QQ号)` 还原成 `@昵称`。

        早期版本的 _clean_content 没处理 AstrBot 的 @ 格式，导致 QQ 号被写进
        正文、污染 FTS 检索。这里按 id 游标分批处理，可重复执行（幂等）。
        """
        pattern = re.compile(r"@([^\s()]*)\((\d{5,12})\)")
        fixed = 0
        last_id = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, content FROM messages WHERE id > ? AND content LIKE '%(%)%'"
                    " ORDER BY id LIMIT 200",
                    (last_id,),
                ).fetchall()
            if not rows:
                break
            batch: list[tuple[int, str, str]] = []
            for r in rows:
                last_id = r["id"]
                new = pattern.sub(
                    lambda m: f"@{m.group(1)}" if m.group(1) else "", r["content"]
                )
                new = re.sub(r"\s+", " ", new).strip()
                if new and new != r["content"]:
                    batch.append((new, segment_cjk(new), r["id"]))
            if batch:
                with self._lock:
                    self._conn.executemany(
                        "UPDATE messages SET content=?, seg=? WHERE id=?", batch
                    )
                    self._commit()
                fixed += len(batch)
        return fixed

    # ---------------------------------------------------------- 读取
    def get_user(self, qq: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE qq=?", (str(qq),)).fetchone()
        return dict(row) if row else None

    def get_groups(self, qq: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM user_groups WHERE qq=? ORDER BY msg_count DESC",
                (str(qq),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_nicknames(self, qq: str, limit: int = 8) -> list[tuple[str, int]]:
        """这个 QQ 用过哪些昵称（按出现次数）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT nickname, COUNT(*) AS n FROM messages"
                " WHERE qq=? AND nickname<>'' GROUP BY nickname ORDER BY n DESC LIMIT ?",
                (str(qq), limit),
            ).fetchall()
        return [(r["nickname"], r["n"]) for r in rows]

    # ---------------------------------------------------------- 好感度
    def get_affinity(self, qq: str) -> dict[str, Any]:
        """读取机器人对某人的好感度。没记录时返回默认 40（初始好感）。

        返回值：{'score': float, 'updated_at': float|None, 'reason': str}
        """
        qq = str(qq or "").strip()
        default = {"score": AFFINITY_DEFAULT, "updated_at": None, "reason": ""}
        if not qq:
            return default
        with self._lock:
            row = self._conn.execute(
                "SELECT score, updated_at, reason FROM affinity WHERE qq=?", (qq,)
            ).fetchone()
        if not row:
            return default
        return {
            "score": float(row["score"]),
            "updated_at": row["updated_at"],
            "reason": str(row["reason"] or ""),
        }

    def set_affinity(
        self, qq: str, score: float, reason: str = ""
    ) -> float:
        """直接设置好感度（会裁剪到 0-100）。返回落库后的值。"""
        qq = str(qq or "").strip()
        if not qq:
            return AFFINITY_DEFAULT
        val = max(AFFINITY_MIN, min(AFFINITY_MAX, float(score)))
        with self._lock:
            self._conn.execute(
                "INSERT INTO affinity(qq, score, updated_at, reason) VALUES(?,?,?,?)"
                " ON CONFLICT(qq) DO UPDATE SET"
                "   score=excluded.score,"
                "   updated_at=excluded.updated_at,"
                "   reason=excluded.reason",
                (qq, val, time.time(), str(reason or "")[:200]),
            )
            self._commit()
        return val

    def adjust_affinity(self, qq: str, delta: float, reason: str = "") -> float:
        """在当前好感度上做增减（会裁剪到 0-100）。返回落库后的值。"""
        qq = str(qq or "").strip()
        if not qq:
            return AFFINITY_DEFAULT
        cur = self.get_affinity(qq)["score"]
        return self.set_affinity(qq, cur + float(delta), reason)

    def affinity_top(self, limit: int = 20) -> list[dict[str, Any]]:
        """好感度最高的前 N 人（含昵称）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.qq, a.score, a.updated_at, a.reason, u.nickname"
                " FROM affinity a LEFT JOIN users u ON u.qq = a.qq"
                " ORDER BY a.score DESC, a.qq LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_facts(self, qq: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM facts WHERE qq=? ORDER BY id DESC LIMIT ?",
                (str(qq), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_messages(self, qq: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE qq=? ORDER BY id DESC LIMIT ?",
                (str(qq), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_messages_other_groups(
        self,
        qq: str,
        exclude_group_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """这个 QQ 在**其他群**里的最近发言（不含 exclude_group_id 所在群）。

        exclude_group_id 传空字符串（私聊）表示「不排除任何群」，返回全部群的
        最近发言——私聊里本来就没有「同群历史」，跨群记忆正是这时最有用。
        """
        qq = str(qq or "").strip()
        if not qq:
            return []
        limit = max(1, int(limit))
        gid = str(exclude_group_id or "")
        with self._lock:
            if gid:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE qq=?"
                    " AND COALESCE(group_id,'')<>? ORDER BY id DESC LIMIT ?",
                    (qq, gid, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE qq=? ORDER BY id DESC LIMIT ?",
                    (qq, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_messages_other_groups(self, qq: str, exclude_group_id: str) -> int:
        """这个 QQ 在**其他群**里的发言总数（用来判断「到底跨没跨群」）。"""
        qq = str(qq or "").strip()
        if not qq:
            return 0
        gid = str(exclude_group_id or "")
        with self._lock:
            try:
                if gid:
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE qq=?"
                        " AND COALESCE(group_id,'')<>?",
                        (qq, gid),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE qq=?", (qq,)
                    ).fetchone()
                return int(row[0] or 0) if row else 0
            except Exception:
                return 0

    def group_names(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, group_name FROM user_groups"
                " WHERE group_name<>'' GROUP BY group_id"
            ).fetchall()
        return {r["group_id"]: r["group_name"] for r in rows}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            def one(sql: str, params: tuple = ()):
                try:
                    r = self._conn.execute(sql, params).fetchone()
                    return int(r[0] or 0) if r else 0
                except Exception:
                    return 0

            return {
                "users": one("SELECT COUNT(*) FROM users"),
                "messages": one("SELECT COUNT(*) FROM messages"),
                "facts": one("SELECT COUNT(*) FROM facts"),
                "groups": one("SELECT COUNT(DISTINCT group_id) FROM messages"),
                "first_at": one("SELECT MIN(created_at) FROM messages"),
                "last_at": one("SELECT MAX(created_at) FROM messages"),
            }

    # ---------------------------------------------------------- 检索
    def _run_match(
        self,
        match_expr: str,
        limit: int,
        qq: str | None = None,
        exclude_group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.*, bm25(messages_fts) AS score FROM messages_fts"
            " JOIN messages m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [match_expr]
        if qq:
            sql += " AND m.qq = ?"
            params.append(str(qq))
        if exclude_group_id:
            sql += " AND COALESCE(m.group_id,'') <> ?"
            params.append(str(exclude_group_id))
        sql += " ORDER BY score LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def search(
        self,
        keyword: str,
        limit: int = 10,
        qq: str | None = None,
        exclude_group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """关键词检索，逐级放宽直到命中为止。

        精确优先：整串短语 → 所有字都要有 → 任一字命中。
        只要某一级有结果就返回，避免「搜麻辣烫却搜出酸辣粉」这种噪声。

        exclude_group_id 非空时排除该群的发言（跨群检索用）。
        """
        tokens = _fts_tokens(keyword)
        if not tokens:
            return []
        limit = max(1, int(limit))
        phrase = '"' + " ".join(tokens) + '"'
        levels = [phrase]
        if len(tokens) > 1:
            levels.append(" AND ".join(f'"{t}"' for t in tokens))
            levels.append(" OR ".join(f'"{t}"' for t in tokens))
        elif len(tokens[0]) > 1:
            # 英文/数字单词整体没命中时，退化为前缀匹配
            levels.append(f'"{tokens[0]}"*')

        for expr in levels:
            try:
                rows = self._run_match(
                    expr, limit, qq=qq, exclude_group_id=exclude_group_id
                )
            except sqlite3.OperationalError:
                continue
            if rows:
                return rows
        return []

    def search_other_groups(
        self,
        keyword: str,
        exclude_group_id: str,
        limit: int = 10,
        qq: str | None = None,
    ) -> list[dict[str, Any]]:
        """在**其他群**里按关键词检索这个 QQ 的发言。

        直接复用 search() 的分级匹配（短语 → AND → OR），只是多挂一个
        「群号 != exclude_group_id」的过滤条件，不重复实现分级逻辑。
        exclude_group_id 为空字符串时等价于 search(keyword, limit, qq)。
        """
        gid = str(exclude_group_id or "")
        return self.search(
            keyword, limit=limit, qq=qq, exclude_group_id=(gid or None)
        )

    def find_qq_by_nickname(self, name: str) -> list[str]:
        name = (name or "").strip()
        if not name:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT qq, COUNT(*) AS n FROM messages WHERE nickname=? GROUP BY qq"
                " ORDER BY n DESC LIMIT 5",
                (name,),
            ).fetchall()
        return [r["qq"] for r in rows]

    def find_qq_by_nickname_prefix(self, name: str) -> list[str]:
        """昵称前缀匹配：用户只打了昵称的一部分（或含空格被截断）。"""
        name = (name or "").strip()
        if len(name) < 2:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT qq, COUNT(*) AS n FROM messages WHERE nickname LIKE ?"
                " GROUP BY qq ORDER BY n DESC LIMIT 5",
                (name + "%",),
            ).fetchall()
        return [r["qq"] for r in rows]


# ---------------------------------------------------------------- 展示格式化

def fmt_time(ts: float | None) -> str:
    if not ts:
        return "?"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def group_label(group_id: str, names: dict[str, str] | None = None) -> str:
    gid = str(group_id or "")
    if not gid:
        return "私聊"
    name = (names or {}).get(gid, "")
    if name:
        return f"{name}({gid})"
    return f"群{gid}"


class ContextBuilder:
    """把数据库里的东西拼成一段给 LLM 看的记忆文本。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def build(
        self,
        qq: str,
        nickname: str,
        group_id: str,
        current_text: str,
        *,
        max_chars: int = 200,
        recent_limit: int = 5,
        related_limit: int = 3,
        facts_limit: int = 6,
        min_messages: int = 3,
    ) -> str:
        qq = str(qq or "").strip()
        if not qq:
            return ""
        user = self.store.get_user(qq)
        if not user:
            return ""
        total = int(user.get("msg_count") or 0)
        if total < int(min_messages or 0):
            return ""  # 话太少，不值得占用 token

        # 好感度：独立于跨群判断。即使这个人只在当前群说话（没有跨群记忆），
        # 「机器人对他什么态度」也必须有，否则行为会千人一面。
        affinity_line = ""
        try:
            aff = self.store.get_affinity(qq)
            tier_name, tier_desc = affinity_tier(aff["score"])
            affinity_line = f"你对他的好感：{aff['score']:.0f}/100（{tier_name}）→ {tier_desc}"
        except Exception:
            affinity_line = ""

        # 【核心】同群历史每轮已由群聊上下文喂给模型，插件只补充「跨群」信息。
        # 在「其他群」的发言不足 2 条 = 没有跨群价值。但好感度仍要单独注入。
        cur_gid = str(group_id or "")
        try:
            other_total = int(self.store.count_messages_other_groups(qq, cur_gid))
        except Exception:
            other_total = 0
        if other_total < 2:
            # 没有跨群内容也要给好感度，否则「服从度」对单群群友完全不生效
            if not affinity_line:
                return ""
            block = f"[关于这位群友]\nQQ号：{qq}\n{affinity_line}"
            return block[: max_chars or 200]

        names = self.store.group_names()
        lines: list[str] = [f"[关于这位群友的记忆]"]

        head = f"QQ号：{qq}"
        use_nick = nickname or user.get("nickname") or ""
        if use_nick:
            head += f"，当前昵称「{use_nick}」"
        lines.append(head)

        # 昵称演变（跨群换马甲也能对上）
        nicks = [n for (n, _c) in self.store.get_nicknames(qq, 6) if n]
        if use_nick and nicks:
            nicks = [n for n in nicks if n != use_nick]
        if nicks:
            lines.append("用过的昵称：" + "、".join(nicks[:6]))

        # 在哪些群出现过（只看其他群：当前群的历史已经由群聊上下文喂给模型了）
        groups = self.store.get_groups(qq)
        if cur_gid:
            groups = [g for g in groups if str(g.get("group_id") or "") != cur_gid]
        if groups:
            parts = []
            for g in groups[:5]:
                label = group_label(g["group_id"], names)
                parts.append(f"{label}（{g['msg_count']}条）")
            lines.append("活跃群：" + "；".join(parts))

        # 好感度放在画像之前：它直接决定「配不配合」，比性格描述更优先
        if affinity_line:
            lines.append(affinity_line)

        # 事实：画像（每日生成的长期总结）排最前，手动记的其次
        facts = self.store.get_facts(qq, facts_limit)
        profile_facts = [f for f in facts if str(f.get("source") or "") == "profile"]
        manual_facts = [f for f in facts if str(f.get("source") or "") != "profile"]
        if profile_facts:
            lines.append("【画像】")
            for f in profile_facts:
                lines.append(f"- {f['content']}")
        if manual_facts:
            lines.append("已知事实：")
            for f in manual_facts:
                lines.append(f"- {f['content']}")

        # 最近发言：只取**其他群**的（当前群 + 500 条历史已在 prompt 里）
        cur = (current_text or "").strip()
        recent: list[dict[str, Any]] = []
        if recent_limit:
            try:
                recent = [
                    m for m in self.store.recent_messages_other_groups(
                        qq, cur_gid, recent_limit + 2
                    )
                    if m.get("content", "").strip() and m.get("content", "").strip() != cur
                ][:recent_limit]
            except Exception:
                recent = []
        if recent:
            lines.append("在其他群里说过：")
            for m in reversed(recent):
                gl = group_label(m.get("group_id", ""), names)
                lines.append(f"- {gl} {fmt_time(m['created_at'])}：{m['content'][:80]}")

        # 与当前话题相关的旧发言：同样只查其他群
        related: list[dict[str, Any]] = []
        if current_text and related_limit:
            try:
                related = self.store.search_other_groups(
                    current_text, cur_gid, limit=related_limit + 2, qq=qq
                )
            except Exception:
                related = []
            recent_ids = {r["id"] for r in recent}
            related = [
                m for m in related
                if m["content"].strip() != cur and m["id"] not in recent_ids
            ][:related_limit]
        if related:
            kw = (current_text or "").strip()[:20]
            lines.append(f"和当前话题相关的旧发言（其他群，关键词：{kw}）：")
            for m in related:
                gl = group_label(m.get("group_id", ""), names)
                lines.append(f"- {gl} {fmt_time(m['created_at'])}：{m['content'][:80]}")

        if len(lines) <= 2:
            return ""

        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            # 按行截断，别把半句话切给模型
            out: list[str] = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > max_chars:
                    out.append("……（还有更多记忆，已省略）")
                    break
                out.append(line)
                used += len(line) + 1
            text = "\n".join(out)
        return text

    def profile_text(self, qq: str) -> str:
        """`/记忆 查` 用的完整档案。"""
        qq = str(qq or "").strip()
        user = self.store.get_user(qq)
        if not user:
            return f"还没记住 QQ {qq}，数据库里没有这个人。"
        names = self.store.group_names()
        groups = self.store.get_groups(qq)
        nicks = self.store.get_nicknames(qq, 10)
        facts = self.store.get_facts(qq, 20)
        recent = self.store.recent_messages(qq, 10)

        out = [f"【QQ {qq} 的记忆档案】"]
        out.append(f"当前昵称：{user.get('nickname') or '未知'}")
        out.append(f"累计发言：{int(user.get('msg_count') or 0)} 条")
        out.append(f"首次出现：{fmt_time(user.get('first_seen'))}　最近出现：{fmt_time(user.get('last_seen'))}")
        if nicks:
            out.append("用过的昵称：" + "、".join(f"{n}({c})" for n, c in nicks))
        if groups:
            out.append("活跃群：")
            for g in groups[:10]:
                out.append(
                    f"  - {group_label(g['group_id'], names)}：{g['msg_count']} 条，"
                    f"最近 {fmt_time(g['last_seen'])}"
                )
        if facts:
            out.append("已知事实：")
            for f in facts:
                out.append(f"  - {f['content']}")
        if recent:
            out.append("最近发言：")
            for m in reversed(recent):
                out.append(
                    f"  - [{group_label(m.get('group_id',''), names)} {fmt_time(m['created_at'])}] "
                    f"{m['content'][:100]}"
                )
        return "\n".join(out)


def search_text(store: MemoryStore, keyword: str, limit: int = 10) -> str:
    """`/记忆 搜` 用的检索结果。"""
    hits = store.search(keyword, limit=limit)
    if not hits:
        return f"没搜到跟「{keyword}」有关的发言。"
    names = store.group_names()
    out = [f"【搜「{keyword}」· {len(hits)} 条】"]
    for h in hits:
        nick = h.get("nickname") or "?"
        out.append(
            f"- {fmt_time(h['created_at'])} [{group_label(h.get('group_id',''), names)}] "
            f"{nick}({h['qq']})：{h['content'][:100]}"
        )
    return "\n".join(out)
