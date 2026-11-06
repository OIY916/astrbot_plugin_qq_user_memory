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

# ---- jieba：主题提取用（可选依赖，没有就退回 n-gram 兜底）
try:
    import jieba as _jieba  # noqa: F401

    try:
        import jieba.posseg as _jieba_posseg  # type: ignore
        _jieba.setLogLevel(60)
    except Exception:  # pragma: no cover
        _jieba_posseg = None
except Exception:  # pragma: no cover
    _jieba_posseg = None

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
-- locked=1 表示主人手动设定过，每日自动评估不得覆盖
CREATE TABLE IF NOT EXISTS affinity (
    qq          TEXT PRIMARY KEY,
    score       REAL NOT NULL DEFAULT 40,
    updated_at  REAL,
    reason      TEXT,
    locked      INTEGER NOT NULL DEFAULT 0
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

SCHEMA_VERSION = 4  # v4: affinity 增加 locked 列（手动锁定不自动覆盖）


# ====================================================================
# 好感度桥接（2026-11-04）
#
# 好感度的唯一真相源改成了 group_chat_plus 的双驱情绪系统
# （data/plugins/astrbot_plugin_group_chat_plus/data/mood_dual.db
#   的 mood_state_v2 表，affection 字段）。
#
# 为什么要换：
#   · qq_user_memory 的 affinity 是**单一数值**（只有 score）
#   · 情绪系统是**双驱模型**（affection 关系 + warmth 亲近 + irritation 烦躁）
#   · 两套并存会打架，所以统一到后者
#
# 本插件只保留「查看」能力（用户决定去掉锁定），
# 所有读写都转发到情绪库，调用方代码一行不用改。
# ====================================================================

_MOOD_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "astrbot_plugin_group_chat_plus", "data", "mood_dual.db",
)


def _mood_db() -> str:
    """定位情绪库（延迟查找，避免加载顺序问题）。"""
    cands = [
        _MOOD_DB_PATH,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "plugins", "astrbot_plugin_group_chat_plus",
            "data", "mood_dual.db"),
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return ""


def _mood_conn():
    """开一个情绪库连接（只读为主）。找不到返回 None。"""
    p = _mood_db()
    if not p:
        return None
    try:
        con = sqlite3.connect(p, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _mood_get_affection(qq: str) -> float | None:
    """从情绪库读 affection。没有记录返回 None。"""
    con = _mood_conn()
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT affection FROM mood_state_v2 "
            "WHERE user_id=? AND scene='group'", (str(qq),)
        ).fetchone()
        if row is None:
            return None
        return float(row["affection"])
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _mood_set_affection(qq: str, score: float, reason: str = "") -> float:
    """写 affection 到情绪库。没有记录就创建。"""
    con = _mood_conn()
    val = max(AFFINITY_MIN, min(AFFINITY_MAX, float(score)))
    if con is None:
        return val
    try:
        row = con.execute(
            "SELECT user_id FROM mood_state_v2 "
            "WHERE user_id=? AND scene='group'", (str(qq),)
        ).fetchone()
        if row:
            con.execute(
                "UPDATE mood_state_v2 SET affection=? "
                "WHERE user_id=? AND scene='group'", (val, str(qq)))
        else:
            warmth = max(0.0, min(100.0, 50.0 + (val - 50.0) * 0.4))
            con.execute(
                "INSERT INTO mood_state_v2 "
                "(user_id, scene, affection, warmth, irritation, "
                " base_warmth, base_irritation, turns, last_update) "
                "VALUES (?, 'group', ?, ?, 0, ?, 0, 0, ?)",
                (str(qq), val, warmth, warmth, time.time()))
        con.commit()
    except Exception as e:
        logger.warning(f"[user_memory] 写情绪库失败（本地兜底）: {e}")
    finally:
        try:
            con.close()
        except Exception:
            pass
    return val


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
        # v3 → v4：affinity 表补 locked 列
        if old_version < 4:
            try:
                cols = [
                    r[1]
                    for r in self._conn.execute("PRAGMA table_info(affinity)").fetchall()
                ]
                if cols and "locked" not in cols:
                    self._conn.execute(
                        "ALTER TABLE affinity ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"
                    )
                    self._conn.commit()
                    logger.info("[user_memory] affinity 表已补 locked 列")
            except Exception as e:
                logger.error(f"[user_memory] affinity 迁移失败: {e}")
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

    #: 主题提取用：出现在多少条消息里才算「他最近在聊这个」
    TOPIC_MIN_COUNT = 2
    #: 分词后只保留名词类词性（n 名词 / nz 专名 / nr 人名 / ns 地名 / nt 机构
    #  / ng 名词性语素 / vn 名动词 / an 名形词 / eng 英文 / l 习用语）
    _TOPIC_KEEP_FLAGS = ("n", "nz", "nr", "ns", "nt", "ng", "vn", "an", "eng", "l")
    #: 虚字（兜底的 n-gram 路径用：首/尾是这些字的组合一律丢）
    _TOPIC_STOP_CHARS = set(
        "的了是我你他她它这那有在不也就都一个们吗呢吧啊哦嗯哈和与跟把被让给对说"
        "要去来到会能要想看过还又很太更没别做上下前后里外多少大小好去后先再"
    )
    #: 虚词/泛词，分词后仍然要丢
    _TOPIC_STOP_WORDS = {
        "什么", "怎么", "为什么", "这个", "那个", "现在", "今天", "明天", "昨天",
        "感觉", "知道", "觉得", "没有", "不是", "还是", "但是", "然后", "所以",
        "因为", "如果", "真的", "有点", "一样", "自己", "东西", "时候", "问题",
        "地方", "意思", "可能", "应该", "需要", "已经", "一直", "这些", "那些",
        "其实", "反正", "群里", "上面", "下面", "里面", "外面", "之前", "之后",
        "起来", "出来", "过来", "回来", "一点", "两个", "三个", "不会", "不用",
        "别人", "咱们", "你们", "他们", "我们", "一个", "这么", "那么", "这样",
        "那样", "时间", "事情", "情况", "样子", "本质", "意思", "概念",
        # ---- 2026-11-05 补：实测捞出来的泛词，对「他在忙什么」零信息量 ----
        "名字", "表情", "差不多", "你好", "自动", "小时", "分钟", "刚才",
        "一下子", "哈哈哈", "笑死", "有点", "确实", "直接", "基本", "刚才",
    }

    @staticmethod
    def _segment_topics(text: str) -> list[str]:
        """把一句话切成名词语（jieba 不可用时退回 n-gram）。"""
        text = (text or "").strip()
        if not text:
            return []
        if _jieba_posseg is not None:
            out = []
            try:
                for w, flag in _jieba_posseg.cut(text):
                    w = w.strip()
                    if len(w) < 2:
                        continue
                    if flag not in MemoryStore._TOPIC_KEEP_FLAGS:
                        continue
                    if w in MemoryStore._TOPIC_STOP_WORDS:
                        continue
                    if re.fullmatch(r"[0-9]+", w):
                        continue
                    out.append(w.lower())
            except Exception:
                return []
            return out
        # ---- 兜底：无 jieba 时用 2-3 字 n-gram ----
        # ⚠️ 实测过：不做虚词过滤时捞出来全是「这个/直接/就是/不是」，
        # 质量等于没有。所以这里复用同一套停用词，并且丢掉首/尾是虚字的组合。
        cleaned = re.sub(r"[^一-鿿A-Za-z0-9]", " ", text)
        out = []
        for seg in cleaned.split():
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{1,}", seg):
                out.append(seg.lower())
                continue
            for n in (2, 3):
                for i in range(len(seg) - n + 1):
                    w = seg[i : i + n]
                    if w[0] in MemoryStore._TOPIC_STOP_CHARS:
                        continue
                    if w[-1] in MemoryStore._TOPIC_STOP_CHARS:
                        continue
                    if w in MemoryStore._TOPIC_STOP_WORDS:
                        continue
                    out.append(w)
        return out

    def recent_topics(
        self, qq: str, days: int = 7, scan: int = 250, top_k: int = 4
    ) -> list[str]:
        """这个人最近几天在聊什么（本地分词统计，不花 LLM 钱）。

        这是「会想」的关键原料：画像回答「他是什么人」（长期静态），
        这一行回答「他这两天在忙什么」（动态）。没有它，模型只能演「我认识你」；
        有了它，模型才能接「你那个 X 弄得怎么样了」。
        """
        qq = str(qq or "").strip()
        if not qq:
            return []
        since = time.time() - max(1, int(days)) * 86400
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT content FROM messages WHERE qq=? AND created_at>=?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (qq, since, max(20, int(scan))),
                ).fetchall()
            except Exception:
                return []

        counter: dict[str, int] = {}
        for r in rows:
            try:
                content = str(r["content"] or "")
            except Exception:
                content = str(r[0] or "")
            # 按「消息条数」计而不是词频：刷屏的人不能靠重复一个词占领主题
            for w in set(self._segment_topics(content)):
                counter[w] = counter.get(w, 0) + 1

        # 样本太少时主题不可信（38 条发言能捞出的只有「傻子」「kk」这种噪声，
        # 反而让模型以为他整天在骂人）。低于这个条数就不给「最近常聊」。
        if len(rows) < 40:
            return []

        cand = [(w, c) for w, c in counter.items() if c >= self.TOPIC_MIN_COUNT]
        cand.sort(key=lambda x: (-x[1], -len(x[0])))
        out: list[str] = []
        for w, _c in cand:
            # 主题词里也要挡脏话/骂人词（「傻子」「废物」当主题毫无意义）
            if _PROFANITY_ANY.search(w):
                continue
            # 已有更长的词包含它就不再单列（有「记忆插件」就不要「记忆」）
            if any(w != o and w in o for o in out):
                continue
            if any(o in w for o in out):
                continue
            out.append(w)
            if len(out) >= top_k:
                break
        return out

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
        """读取好感度 —— **数据源是 group_chat_plus 的情绪库**。

        2026-11-04：好感度模块已换成双驱情绪系统
        （mood_state_v2 表的 affection 字段）。
        返回值结构保持不变，调用方不用改。

        返回值：{'score': float, 'updated_at': float|None, 'reason': str, 'locked': bool}
        locked 恒为 False（用户决定去掉锁定功能）。
        """
        qq = str(qq or "").strip()
        default = {
            "score": AFFINITY_DEFAULT,
            "updated_at": None,
            "reason": "",
            "locked": False,
        }
        if not qq:
            return default

        # ---- 优先读情绪库 ----
        aff = _mood_get_affection(qq)
        if aff is not None:
            # reason 仍从本地表拿（情绪库里没有这个字段）
            reason = ""
            updated = None
            try:
                with self._lock:
                    row = self._conn.execute(
                        "SELECT reason, updated_at FROM affinity WHERE qq=?",
                        (qq,)).fetchone()
                if row:
                    reason = str(row["reason"] or "")
                    updated = row["updated_at"]
            except Exception:
                pass
            return {
                "score": float(aff),
                "updated_at": updated,
                "reason": reason,
                "locked": False,
            }

        # ---- 情绪库里没有 → 退回本地表（迁移前的老数据/从未互动的人）----
        with self._lock:
            row = self._conn.execute(
                "SELECT score, updated_at, reason, locked FROM affinity WHERE qq=?", (qq,)
            ).fetchone()
        if not row:
            return default
        keys = row.keys()
        return {
            "score": float(row["score"]),
            "updated_at": row["updated_at"],
            "reason": str(row["reason"] or ""),
            "locked": False,   # 锁定功能已废弃
        }

    def set_affinity(
        self, qq: str, score: float, reason: str = "", locked: bool = False
    ) -> float:
        """设置好感度 —— **写入 group_chat_plus 的情绪库**。

        2026-11-04：数据源换成双驱情绪系统。
        locked 参数保留但**已废弃**（用户决定去掉锁定功能）。

        reason 仍存本地表（情绪库没有这个字段），便于回溯是谁改的。
        """
        qq = str(qq or "").strip()
        if not qq:
            return AFFINITY_DEFAULT

        val = max(AFFINITY_MIN, min(AFFINITY_MAX, float(score)))

        # ---- 写情绪库（唯一真相源）----
        written = _mood_set_affection(qq, val, reason)

        # ---- reason 记在本地表（不参与读取判断）----
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO affinity(qq, score, updated_at, reason, locked)"
                    " VALUES(?,?,?,?,0)"
                    " ON CONFLICT(qq) DO UPDATE SET"
                    "   score=excluded.score,"
                    "   updated_at=excluded.updated_at,"
                    "   reason=excluded.reason",
                    (qq, val, time.time(), str(reason or "")[:200]),
                )
                self._commit()
        except Exception:
            pass

        return written

    def unlock_affinity(self, qq: str) -> bool:
        """解除锁定 —— **功能已废弃**（2026-11-04 好感度模块换成情绪系统）。

        保留方法是为了让老命令不报 AttributeError，但永远返回 False。
        """
        return False

    def adjust_affinity(self, qq: str, delta: float, reason: str = "") -> float:
        """在当前好感度上做增减（会裁剪到 0-100）。返回落库后的值。

        2026-11-04：读写都走情绪库。锁定功能已废弃，不再检查 locked。
        """
        qq = str(qq or "").strip()
        if not qq:
            return AFFINITY_DEFAULT
        cur = self.get_affinity(qq)
        return self.set_affinity(qq, cur["score"] + float(delta), reason)

    def affinity_top(self, limit: int = 20) -> list[dict[str, Any]]:
        """好感度最高的前 N 人（含昵称）—— **数据源是情绪库**。"""
        # ---- 优先从情绪库算 ----
        con = _mood_conn()
        if con is not None:
            try:
                rows = con.execute(
                    "SELECT user_id, affection FROM mood_state_v2 "
                    "WHERE scene='group' ORDER BY affection DESC LIMIT ?",
                    (int(limit),)).fetchall()
                ids = [r["user_id"] for r in rows]
                nicks = {}
                if ids:
                    try:
                        with self._lock:
                            ph = ",".join("?" * len(ids))
                            for r in self._conn.execute(
                                f"SELECT qq, nickname FROM users WHERE qq IN ({ph})",
                                ids,
                            ):
                                nicks[str(r["qq"])] = r["nickname"]
                    except Exception:
                        pass
                return [
                    {
                        "qq": str(r["user_id"]),
                        "score": float(r["affection"]),
                        "updated_at": None,
                        "reason": "",
                        "nickname": nicks.get(str(r["user_id"]), ""),
                    }
                    for r in rows
                ]
            except Exception:
                pass
            finally:
                try:
                    con.close()
                except Exception:
                    pass

        # ---- 兜底：本地表 ----
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


# ---------------------------------------------------------------- 发言筛选
#
# 【2026-11-05 加】为什么需要这个：
# 注入块里原来原样铺「在其他群里说过：- 用 / - 对的 / - 啥」，
# 这类碎片对模型零信息量，却实打实吃掉 300 字的预算，
# 把真正有价值的「dsh默认上下文是1M」挤出去。
# 模型看不到有内容的记忆，自然只表现出「记得这个人」，不会「想起具体的事」。

#: 低于这个长度基本是「用 / 对的 / 啥 / 哈」这类废话
_MIN_SUBSTANTIVE_LEN = 6

#: 整条就是语气词/应答词，一律丢掉
_FILLER_RE = re.compile(
    r"^(?:"
    r"用|对的|是的|对|是|嗯|啊|哦|噢|哈|哈哈|哈哈哈|\+1|1|6|666|"
    r"好|好吧|好的|行|可以|没事|没有|不是|真的|确实|好的呢|"
    r"啥|什么|怎么|为什么|草|我靠|啊这|离谱|笑死|绷|乐|"
    r"ok|okay|yes|no|嗯嗯|啊啊|哦哦|好好好"
    r")[。！？~…\.\!\?]*$",
    re.IGNORECASE,
)

#: 纯表情/纯标点
_JUNK_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)

#: 纯骂人/纯情绪宣泄。这类句子有长度、有"内容"，但对「认识一个人」零价值，
#: 而且在注入块里出现会带偏模型的语气（实测：注入「我有傻逼恐惧症」之后
#: 回复明显变冲）。只拦「整句都是脏话」的，骂人里带具体信息的照留。
_PROFANITY = (
    "傻逼|傻比|傻b|傻福|sb|妈的|他妈的|tmd|nmsl|cnm|草泥马|尼玛|你妈|去死|"
    "滚你|贱人|废物|狗东西|智障|脑残|弱智|神经病|有病吧|闭嘴|"
    "fuck|shit|bitch|damn"
)
_PROFANITY_RE = re.compile(r"^(?:" + _PROFANITY + r")", re.IGNORECASE)
_PROFANITY_ANY = re.compile(_PROFANITY, re.IGNORECASE)


def is_substantive(content: str) -> bool:
    """这条发言值不值得占注入预算。"""
    t = (content or "").strip()
    if not t:
        return False
    if _JUNK_ONLY_RE.match(t):
        return False
    if _FILLER_RE.match(t):
        return False
    # 纯 @ 提及（@某人 / @某人 空格）没有任何内容
    if re.fullmatch(r"(?:@\S+\s*)+", t):
        return False
    # 中文按字算，英文/数字按 2 字算（"1M" 有点信息量但不大）
    if len(t) < _MIN_SUBSTANTIVE_LEN:
        return False
    # 脏话清洗后剩不下东西 → 整句就是情绪宣泄
    if _PROFANITY_ANY.search(t):
        stripped = _PROFANITY_ANY.sub("", t)
        stripped = re.sub(r"[\s\W_]+", "", stripped)
        if len(stripped) < 6:
            return False
    return True


def rank_messages(
    messages: list[dict[str, Any]],
    topics: list[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """从候选发言里挑「最值得记住的」几条，而不是「最近的」几条。

    为什么不能只取最近：
    「在其他群里说过」原来越是取最近 3 条，越容易全是「@xxx」「哈哈」
    「这儿有个傻福骂人」这种，模型读了等于没读。
    这里按「有信息量」打分：长度适中、含专名/英文/数字、命中他的近期主题 加分；
    然后按时间正序返回（模型看到的是「他这些天在聊这些」）。

    打分权重是实测调出来的，别随手改。
    """
    topics = [t.lower() for t in (topics or []) if t]
    scored: list[tuple[float, dict[str, Any]]] = []
    for idx, m in enumerate(messages):
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        score = 0.0
        n = len(content)
        # 1) 长度：6-60 字最像「一句完整的话」；超过 80 字多半是转贴
        if 6 <= n <= 60:
            score += 1.0
        elif 60 < n <= 120:
            score += 0.5
        # 2) 含英文/数字/URL = 具体信息（dsh、1M、b23、github 链接…）
        if re.search(r"[A-Za-z]{2,}|https?://|\d", content):
            score += 0.8
        # 3) 命中近期主题 = 真正反映他在关心什么
        low = content.lower()
        hit = sum(1 for t in topics if t and t in low)
        score += min(hit, 3) * 1.2
        # 4) 越新越好（但只占小权重，别让它盖过信息量）
        score += max(0.0, 0.6 - idx * 0.05)
        scored.append((score, m))

    scored.sort(key=lambda x: -x[0])
    picked = [m for _s, m in scored[: max(1, int(limit))]]
    picked.sort(key=lambda m: float(m.get("created_at") or 0))
    return picked


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
        min_messages: int = 20,
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
            # 没有跨群内容，但「好感度 + 画像」仍必须有。
            # ⚠️ 2026-11-01 修复：原来这条路径只返回好感度一行，导致
            # **只在一个群活跃的群友永远拿不到画像**（实测 4/10 用户中招），
            # 而画像恰恰是最能体现「认识这个人」的东西。跨不跨群不该影响
            # 要不要认识他——跨群只影响「要不要补别的群的发言」。
            short_lines: list[str] = ["[关于这位群友]"]
            short_lines.append(
                "（这些是你自己记得的事，不是别人塞给你的资料。"
                "他问你最近在干嘛、你还记不记得、猜他在折腾什么——"
                "就照你记得的答，别装不知道、别反问回去、别要他提醒。"
                "其余时候聊到了顺口提一句，别整段复述、别说“根据记忆”。）"
            )
            if affinity_line:
                short_lines.append(affinity_line)
            # 单群用户同样需要「他最近在聊什么」——否则只能演出「我认识你」
            try:
                _topics = self.store.recent_topics(qq, days=7, top_k=4)
            except Exception:
                _topics = []
            if _topics:
                short_lines.append("最近常聊：" + "、".join(_topics))
            facts = self.store.get_facts(qq, facts_limit)
            profile_facts = [
                f for f in facts if str(f.get("source") or "") == "profile"
            ]
            if profile_facts:
                short_lines.append("【画像】")
                for f in profile_facts:
                    short_lines.append(f"- {f['content']}")
            head = f"QQ号：{qq}"
            use_nick = nickname or user.get("nickname") or ""
            if use_nick:
                head += f"，当前昵称「{use_nick}」"
            short_lines.append(head)
            if not affinity_line and not profile_facts:
                return ""
            text = "\n".join(short_lines)
            if max_chars and len(text) > max_chars:
                out: list[str] = []
                used = 0
                for line in short_lines:
                    if used + len(line) + 1 > max_chars:
                        out.append("……（还有更多记忆，已省略）")
                        break
                    out.append(line)
                    used += len(line) + 1
                text = "\n".join(out)
            return text

        names = self.store.group_names()
        lines: list[str] = [f"[关于这位群友的记忆]"]
        # 【2026-11-05 加】使用方式必须写在**开头**，否则一被截断就没了，
        # 模型就退回「照着档案念」。这句是「会想」和「会背」的分界线。
        lines.append(
            "（这些是你自己记得的事，不是别人塞给你的资料。"
            "他问你最近在干嘛、你还记不记得、猜他在折腾什么——"
            "就照你记得的答，别装不知道、别反问回去、别要他提醒。"
            "其余时候聊到了顺口提一句，别整段复述、别说“根据记忆”。）"
        )

        # ⚠️ 顺序决定截断后「谁先被保住」（2026-11-01 重排）。
        # 实测：12 个活跃用户里 9 个被 300 字符截断，而「活跃群全列表」一项
        # 就吃掉 140 字符（47%），导致【画像】经常是空的（连号主自己都中招）。
        # 现在的顺序是「价值高的先放」：好感度 → 画像 → 事实 → 跨群发言 → 头部 → 群数。
        # 头部（QQ号/昵称）信息量低但模型要靠它对上人，所以排在画像之后、
        # 跨群发言之前；「活跃群」压成一句「还在 N 个其他群活跃」，不再列全名单。

        # 1) 好感度：直接决定「配不配合」，比性格描述更优先
        if affinity_line:
            lines.append(affinity_line)

        # 1.5) 【近期关注】——「会想」的核心。
        # 画像只回答「他是什么人」（长期、静态），这一行回答「他这几天在忙什么」（动态）。
        # 没有这一行，模型只能演「我认识你」，有了才能接「你那个 X 弄得怎么样了」。
        try:
            topics = self.store.recent_topics(qq, days=7, top_k=4)
        except Exception:
            topics = []
        if topics:
            lines.append("最近常聊：" + "、".join(topics))

        # 2) 事实：画像（每日生成的长期总结）排最前，手动记的其次
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

        # 3) 头部（QQ号 + 当前昵称）：让模型能把画像对上具体的人
        head = f"QQ号：{qq}"
        use_nick = nickname or user.get("nickname") or ""
        if use_nick:
            head += f"，当前昵称「{use_nick}」"
        lines.append(head)

        # 4) 昵称演变（跨群换马甲也能对上）——只留最近 3 个，原 6 个太占地方
        nicks = [n for (n, _c) in self.store.get_nicknames(qq, 6) if n]
        if use_nick and nicks:
            nicks = [n for n in nicks if n != use_nick]
        if nicks:
            lines.append("用过的昵称：" + "、".join(nicks[:3]))

        # 最近发言：只取**其他群**的（当前群 + 500 条历史已在 prompt 里）
        # ⚠️ 2026-11-05：多取 4 倍再筛掉废话，保证「值得看的那条」不会被
        # 「用 / 对的 / 啥」挤掉（原来直接取 3 条，经常全是碎片）。
        cur = (current_text or "").strip()
        recent: list[dict[str, Any]] = []
        if recent_limit:
            try:
                pool = self.store.recent_messages_other_groups(
                    qq, cur_gid, max(recent_limit * 5, 20)
                )
                recent = rank_messages(
                    [
                        m for m in pool
                        if m.get("content", "").strip()
                        and m.get("content", "").strip() != cur
                        and is_substantive(m.get("content", ""))
                    ],
                    topics,
                    limit=recent_limit,
                )
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
                    current_text, cur_gid, limit=(related_limit + 2) * 3, qq=qq
                )
            except Exception:
                related = []
            recent_ids = {r["id"] for r in recent}
            related = [
                m for m in related
                if m["content"].strip() != cur
                and m["id"] not in recent_ids
                and is_substantive(m["content"])
            ][:related_limit]
        if related:
            kw = (current_text or "").strip()[:20]
            lines.append(f"和当前话题相关的旧发言（其他群，关键词：{kw}）：")
            for m in related:
                gl = group_label(m.get("group_id", ""), names)
                lines.append(f"- {gl} {fmt_time(m['created_at'])}：{m['content'][:80]}")

        # 5) 活跃群：压成一句话，不再列 5 个群名 + 条数（实测这一项曾吃掉 47% 预算）
        groups = self.store.get_groups(qq)
        if cur_gid:
            groups = [g for g in groups if str(g.get("group_id") or "") != cur_gid]
        if groups:
            lines.append(f"还在 {len(groups)} 个其他群活跃")

        if len(lines) <= 2:
            return ""

        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            # 按行截断，别把半句话切给模型
            out: list[str] = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > max_chars:
                    # ⚠️ 2026-11-05：不能把「以冒号结尾的小标题」留在末尾当孤儿。
                    # 原来会产出「和当前话题相关的旧发言（关键词：xxx）：」+ 省略号，
                    # 模型看到「有个标题但没内容」会以为记忆残缺，反而更不敢用。
                    while out and out[-1].rstrip().endswith(("：", ":")):
                        used -= len(out.pop()) + 1
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
