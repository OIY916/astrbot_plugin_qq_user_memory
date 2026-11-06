# -*- coding: utf-8 -*-
"""情绪核心 —— 好感度（关系）+ 情绪值（心情）双轨模型。

设计借鉴（对比了 7 个插件的结论）：
  · 双驱模型（astrbot_plugin_affection）
      不存单值「好感 75」，存两个连续量：
        warmth     亲近欲（喜欢你 / 想靠近）
        irritation 排斥欲（烦 / 想怼）
      用双线性插值得到混合标签「亲近(70%)/烦躁(30%)」，
      而不是非黑即白的「开心/生气」。
  · 基线 / 当前分离（同上）
        base_*    长期印象（认识越久越难改）
        current_* 此刻情绪（会衰减回基线）
  · 确定性推进（astrbot_plugin_LivelyState）
      不依赖 LLM 主动「记得更新」，按时间自动推进/衰减。
      LLM 只负责「这一次互动造成多少偏移」。
  · 缓存友好注入（同上）
      提示词只追加到最后一条 user 消息，绝不碰 system_prompt。

本模块只负责**算数与持久化**，不碰 AstrBot / 不发网络请求，
方便单测和以后复用（QQ / 网页通话都能用）。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# ====================================================================
# 一、情绪标签表（双线性插值用）
#
# 横轴 warmth（0~100），纵轴 irritation（0~100），
# 档位 0/25/50/75/100，落在档位之间就插值。
# ====================================================================

BRACKETS = (0.0, 25.0, 50.0, 75.0, 100.0)

# (warmth 档, irritation 档) -> 标签
#
# ⚠️ 这张表的**行是亲近欲、列是烦躁**，越往下越冷淡、越往右越烦躁。
#    设计原则：被骂的时候应该明显生气，而不是"小声嘀咕"。
#    （v1 把 (25,25) 写成"别扭"、把 (25,50) 写成"不爽"，
#     导致"滚"只算出"小声嘀咕"，太温柔 —— 已按实测修正。）
MOOD_TABLE: Dict[Tuple[float, float], str] = {
    # 低亲近（好感不高时的底色）
    (0.0,   0.0):   "冷淡",
    (0.0,   25.0):  "不耐烦",
    (0.0,   50.0):  "生气",
    (0.0,   75.0):  "很生气",
    (0.0,   100.0): "炸毛",
    (25.0,  0.0):   "别扭",
    (25.0,  25.0):  "不爽",
    (25.0,  50.0):  "生气",
    (25.0,  75.0):  "暴躁",
    (25.0,  100.0): "气炸",
    # 中亲近（关系还行，但架不住烦）
    (50.0,  0.0):   "温和",
    (50.0,  25.0):  "有点意见",
    (50.0,  50.0):  "赌气",
    (50.0,  75.0):  "委屈又生气",
    (50.0,  100.0): "崩溃",
    (75.0,  0.0):   "开心",
    (75.0,  25.0):  "撒娇",
    (75.0,  50.0):  "假装生气",
    (75.0,  75.0):  "闹脾气",
    (75.0,  100.0): "又气又难过",
    # 高亲近（很熟，闹归闹）
    (100.0, 0.0):   "很黏人",
    (100.0, 25.0):  "嘴硬心软",
    (100.0, 50.0):  "赌气但还是在乎",
    (100.0, 75.0):  "受委屈",
    (100.0, 100.0): "心碎",
}

# 好感度分档（决定用哪一行标签倾向）
def affection_level(affection: float) -> int:
    """好感度 -> 0/25/50/75/100 五档。"""
    if affection < 12.5:
        return 0
    if affection < 37.5:
        return 25
    if affection < 62.5:
        return 50
    if affection < 87.5:
        return 75
    return 100


def _find_bracket(value: float) -> Tuple[float, float, float, float]:
    """找到 value 在 BRACKETS 里的上下档位和权重。

    返回 (下档, 上档, 下档权重, 上档权重)。恰好落在档位上时权重 1/0。
    """
    v = max(0.0, min(100.0, float(value)))
    for i, b in enumerate(BRACKETS):
        if v <= b:
            if i == 0 or v == b:
                return (b, b, 1.0, 0.0)
            lower = BRACKETS[i - 1]
            span = b - lower
            w_upper = (v - lower) / span if span else 0.0
            return (lower, b, 1.0 - w_upper, w_upper)
    return (BRACKETS[-1], BRACKETS[-1], 1.0, 0.0)


def blended_mood(warmth: float, irritation: float, affection: float) -> str:
    """双线性插值出混合情绪标签。

    返回形如「亲近(70%)/烦躁(30%)」或单一标签。
    """
    w_lower, w_upper, wlw, wuw = _find_bracket(warmth)
    i_lower, i_upper, ilw, iuw = _find_bracket(irritation)

    # 好感度低时整体更冷淡：把 warmth 往低压一档
    lvl = affection_level(affection)
    if lvl <= 25:
        w_lower, w_upper = min(w_lower, w_lower), min(w_upper, w_lower + 25.0)
        wlw, wuw = 1.0 if w_lower == w_upper else 0.5, 0.0 if w_lower == w_upper else 0.5

    corners = [
        (MOOD_TABLE.get((w_lower, i_lower), ""), wlw * ilw),
        (MOOD_TABLE.get((w_lower, i_upper), ""), wlw * iuw),
        (MOOD_TABLE.get((w_upper, i_lower), ""), wuw * ilw),
        (MOOD_TABLE.get((w_upper, i_upper), ""), wuw * iuw),
    ]
    # 同标签合并权重
    merged: Dict[str, float] = {}
    for label, weight in corners:
        if label and weight > 1e-9:
            merged[label] = merged.get(label, 0.0) + weight

    if not merged:
        return "平静"
    items = sorted(merged.items(), key=lambda kv: -kv[1])
    if len(items) == 1:
        return items[0][0]

    # 两个标签差距太小就只报主要那个（避免「开心(52%)/温和(48%)」这种噪声）
    top_label, top_w = items[0]
    second_label, second_w = items[1]
    total = top_w + second_w
    if total <= 0:
        return top_label
    pct = int(round(top_w / total * 100))
    if pct >= 85:
        return top_label
    return f"{top_label}({pct}%)/{second_label}({100 - pct}%)"

# ====================================================================
# 二、情绪 -> 行为指令（给模型的「怎么做」，不是「是什么」）
#
# 借鉴 group_chat_plus 现有的做法：真人不会默念「我要表现生气」，
# 而是直接生气。所以给模型的是具体行为指引。
# ====================================================================

MOOD_BEHAVIOR: Dict[str, str] = {
    "冷淡":       "现在没什么兴致，回短一点，别热情过头",
    "不耐烦":     "现在有点烦，话别说多，可以敷衍",
    "生气":       "现在有点不爽，回短一点、可以直接回呛，别顺着对方说",
    "很生气":     "现在很气，可以直接怼，不用给面子，但别真的伤人",
    "炸毛":       "现在炸了，语气冲、句子短，说完就想撂挑子",
    "平静":       "现在心情平常，正常说话就行",
    "别扭":       "现在有点别扭，嘴上可能不饶人，但别真的冷掉",
    "不爽":       "现在不太高兴，话少一点，带点阴阳也行",
    "暴躁":       "现在很燥，容易不耐烦，句子可以短促",
    "气炸":       "现在气到极点，可以直接呛人或者干脆说不想聊",
    "温和":       "现在心情平和，语气柔和一点",
    "小声嘀咕":   "现在有点小情绪，嘴上小声抱怨但不至于翻脸",
    "赌气":       "现在在赌气，嘴上不承认但明显在闹小脾气",
    "委屈又生气": "现在又委屈又气，可以带点哭腔但还在硬撑",
    "崩溃":       "现在快崩溃了，情绪外放，可以说得乱一点",
    "开心":       "现在心情不错，语气轻快，能接梗、能开玩笑",
    "撒娇":       "现在想撒娇，语气软一点、可以拖长音",
    "假装生气":   "现在假装生气，嘴上凶但其实是闹着玩",
    "闹脾气":     "现在在闹小脾气，可以被哄，但不会马上好",
    "又气又难过": "现在又气又难过，说话会带点哽咽或者停顿",
    "很黏人":     "现在特别黏，想被理、想被哄，话可以多一点",
    "嘴硬心软":   "现在嘴硬心软，表面说不要、其实很在乎",
    "赌气但还是在乎": "现在嘴上是赌气，但只要对方哄一下就软",
    "受委屈":     "现在觉得被冷落或者被误解了，语气低一点，别说漂亮话",
    "心碎":       "现在很难过，话说得少、情绪低，不装开心",
}


def mood_instruction(label: str) -> str:
    """把混合标签转成行为指令。

    混合标签形如「赌气(60%)/小声嘀咕(40%)」——
    取权重**最高**的那个（这里 60% 是赌气），而不是斜杠后面的。
    """
    if not label:
        return ""
    # 「赌气(60%)/小声嘀咕(40%)」 -> 先切斜杠，再按百分比排序
    parts = [p.strip() for p in label.split("/") if p.strip()]
    best_name, best_pct = "", -1.0
    for p in parts:
        name = p.split("(")[0].strip()
        pct = -1.0
        if "(" in p and "%" in p:
            try:
                pct = float(p.split("(")[1].split("%")[0])
            except (IndexError, ValueError):
                pct = -1.0
        # 没有百分比的是单一标签，权重视为最高
        if pct < 0:
            pct = 101.0
        if pct > best_pct:
            best_name, best_pct = name, pct

    behavior = MOOD_BEHAVIOR.get(best_name, "")
    if behavior:
        return (
            f"[当前状态：{label}。{behavior}。"
            "这是你此刻真实的情绪，自然表达出来即可，不要提到这个提示本身]"
        )
    return f"[当前状态：{label}（在你的人格基调上自然体现）]"


# ====================================================================
# 三、情绪状态数据结构
# ====================================================================

# 场景常量
SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
SCENES = (SCENE_GROUP, SCENE_PRIVATE)


@dataclass
class MoodState:
    """一个人在某场景下的情绪状态。

    ⚠️ 同一个人的群聊 / 私聊是**两份独立状态**：
       群里和大家一起 vs 私下跟你单独聊，她的状态本来就该不一样。
    """

    user_id: str = ""
    scene: str = SCENE_GROUP      # "group" | "private"

    # ---- 好感度（关系，慢变量）----
    affection: float = 50.0        # 0~100，长期关系

    # ---- 情绪双驱（快变量）----
    warmth: float = 50.0           # 亲近欲 0~100
    irritation: float = 0.0        # 排斥欲 0~100

    # ---- 基线（长期印象，极难改变）----
    base_warmth: float = 50.0
    base_irritation: float = 0.0

    # ---- 记账 ----
    turns: int = 0                 # 互动轮次（影响基线可塑程度）
    last_update: float = field(default_factory=time.time)

    def label(self) -> str:
        return blended_mood(self.warmth, self.irritation, self.affection)

    def instruction(self) -> str:
        return mood_instruction(self.label())


# ====================================================================
# 四、确定性推进（借鉴 LivelyState：不靠 LLM 也会自己变）
#
# 核心：情绪会随时间衰减回基线。
# 开心会变淡、生气会消气 —— 这不是「忘记」，是真实心理。
# ====================================================================

# 衰减半衰期（秒）：不同情绪衰减速度不一样
DECAY_HALFLIFE = {
    "irritation": 900.0,    # 烦躁：15 分钟消一半（气消得快）
    "warmth": 5400.0,       # 亲近：90 分钟回一半（好心情留得久一点）
}

# 好感度几乎不衰减，只在长期不互动时轻微回落
AFFECTION_IDLE_DECAY_HALFLIFE = 7 * 24 * 3600.0   # 7 天


def _decay(current: float, base: float, halflife: float, elapsed: float) -> float:
    """指数衰减：current 向 base 靠拢。"""
    if halflife <= 0 or elapsed <= 0:
        return current
    factor = math.pow(0.5, elapsed / halflife)
    return base + (current - base) * factor


def advance(state: MoodState, now: Optional[float] = None) -> MoodState:
    """按真实时间推进状态（惰性求值：只在被读时才算）。"""
    now = now or time.time()
    elapsed = max(0.0, now - state.last_update)
    if elapsed < 1.0:
        return state

    # 情绪衰减回基线
    state.warmth = _decay(state.warmth, state.base_warmth,
                          DECAY_HALFLIFE["warmth"], elapsed)
    state.irritation = _decay(state.irritation, state.base_irritation,
                              DECAY_HALFLIFE["irritation"], elapsed)    # 好感度长期不动才回落（避免「一天没聊就掉好感」的廉价感）
    if elapsed > 6 * 3600:
        state.affection = _decay(state.affection, 50.0,
                                 AFFECTION_IDLE_DECAY_HALFLIFE, elapsed)

    state.last_update = now
    return state


# ====================================================================
# 五、应用一次互动带来的偏移
#
# 借鉴 affection 的 clamp：基线随轮次越来越难改。
# ====================================================================

def apply_delta(
    state: MoodState,
    warmth_delta: float = 0.0,
    irritation_delta: float = 0.0,
    affection_delta: float = 0.0,
    now: Optional[float] = None,
) -> MoodState:
    """应用一次情绪偏移，并夹在合法范围里。"""
    now = now or time.time()
    advance(state, now)

    state.turns += 1

    # 当前值：直接加，夹在 0~100
    state.warmth = max(0.0, min(100.0, state.warmth + warmth_delta))
    state.irritation = max(0.0, min(100.0, state.irritation + irritation_delta))
    state.affection = max(0.0, min(100.0, state.affection + affection_delta))

    # 基线：随轮次越来越难改（初印象形成后基本定型）
    # turns <= 10 时基线可塑（±1.5），之后极小（±0.2）
    if state.turns <= 10:
        base_scale = 1.0
    elif state.turns <= 30:
        base_scale = 0.4
    else:
        base_scale = 0.12
    state.base_warmth = max(
        0.0, min(100.0,
                 state.base_warmth + warmth_delta * 0.15 * base_scale))
    state.base_irritation = max(
        0.0, min(100.0,
                 state.base_irritation + irritation_delta * 0.15 * base_scale))

    state.last_update = now
    return state


# ====================================================================
# 六、关键词快速判分（零成本，覆盖大部分日常）
#
# 只有关键词都命中不了、或者疑似强情绪时，才值得调 LLM。
# ====================================================================

# (关键词, warmth_delta, irritation_delta, affection_delta)
#
# ⚠️ 参数按真实体感调过：一句「滚」必须真的把她惹毛，
#    不能只到「有点意见」的程度。所以强负面直接给 irritation +35。
KEYWORD_RULES: List[Tuple[Tuple[str, ...], float, float, float]] = [
    # 强负面 —— 会真的伤到关系
    (("滚", "闭嘴", "傻逼", "恶心", "烦死了", "去死"), -18.0, +38.0, -4.0),
    # 骂人但没那么重
    (("笨蛋", "蠢", "傻", "讨厌", "烦人", "垃圾"), -8.0, +22.0, -1.5),
    # 敷衍 / 冷淡
    (("哦", "嗯", "随便", "无所谓", "懒得"), -5.0, +10.0, -0.6),
    # 强正面
    (("喜欢你", "爱你", "想你", "最好了", "离不开"), +25.0, -15.0, +4.5),
    # 夸奖
    (("厉害", "好棒", "聪明", "可爱", "好看", "优秀"), +14.0, -8.0, +1.8),
    # 关心 / 陪伴
    (("辛苦了", "加油", "抱抱", "别累", "早点睡", "注意身体"), +12.0, -5.0, +1.4),
    # 道歉
    (("对不起", "抱歉", "我错了", "别生气"), +8.0, -18.0, +1.2),
    # 撒娇 / 亲近
    (("想见", "陪", "在吗", "人呢", "回我"), +9.0, 0.0, +0.9),
    # 玩笑 / 调侃（会让她假生气）
    (("哈哈哈", "笑死", "搞笑", "逗你", "嘿嘿"), +7.0, +4.0, +0.7),
    # 被无视
    (("已读不回", "不理我", "好久没", "消失"), +2.0, +12.0, -1.8),
]

# 需要 LLM 细判的信号（关键词得分冲突 / 强情绪 / 反讽）
LLM_HINT_WORDS = ("但是", "虽然", "其实", "呵呵", "呵", "就这", "?", "？")


def quick_score(text: str) -> Optional[Tuple[float, float, float]]:
    """关键词打分。返回 None 表示「拿不准，值得调 LLM」。

    返回 (warmth_delta, irritation_delta, affection_delta)
    """
    if not text:
        return None
    t = text.strip()
    if not t:
        return None

    w = i = a = 0.0
    hit = False
    for kws, dw, di, da in KEYWORD_RULES:
        for kw in kws:
            if kw in t:
                w += dw
                i += di
                a += da
                hit = True
                break  # 同一组只算一次，避免「哈哈哈」叠三倍

    if not hit:
        return None

    # 信号冲突（同时有正有负）→ 让 LLM 细判
    if w > 0 and i > 5:
        return None
    # 有反讽/转折类词 → 让 LLM 细判
    for hw in LLM_HINT_WORDS:
        if hw in t:
            return None

    return (w, i, a)


# ====================================================================
# 七、持久化（SQLite，按 user_id 一人一行）
# ====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS mood_state_v2 (
    user_id         TEXT NOT NULL,
    scene           TEXT NOT NULL DEFAULT 'group',
    affection       REAL NOT NULL DEFAULT 50.0,
    warmth          REAL NOT NULL DEFAULT 50.0,
    irritation      REAL NOT NULL DEFAULT 0.0,
    base_warmth     REAL NOT NULL DEFAULT 50.0,
    base_irritation REAL NOT NULL DEFAULT 0.0,
    turns           INTEGER NOT NULL DEFAULT 0,
    last_update     REAL NOT NULL,
    PRIMARY KEY (user_id, scene)
);

-- 情绪变化历史（给曲线图用，带场景）
CREATE TABLE IF NOT EXISTS mood_history_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    scene       TEXT NOT NULL DEFAULT 'group',
    ts          REAL NOT NULL,
    affection   REAL NOT NULL,
    warmth      REAL NOT NULL,
    irritation  REAL NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mood_hist_v2
    ON mood_history_v2(user_id, scene, ts);

CREATE INDEX IF NOT EXISTS idx_mood_state_v2_aff
    ON mood_state_v2(scene, affection DESC);
"""

# 迁移用：老表（单主键 user_id）→ 新表（复合主键）
MIGRATE_SQL = """
INSERT OR IGNORE INTO mood_state_v2
    (user_id, scene, affection, warmth, irritation,
     base_warmth, base_irritation, turns, last_update)
SELECT ?, 'group', affection, warmth, irritation,
       base_warmth, base_irritation, turns, last_update
FROM {old_table};
"""


class MoodStore:
    """情绪状态的 SQLite 存储。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with sqlite3.connect(db_path) as con:
            for stmt in SCHEMA.strip().split(";"):
                s = stmt.strip()
                if s:
                    con.execute(s)
            con.commit()
            self._migrate_legacy(con)

    @staticmethod
    def _migrate_legacy(con) -> None:
        """把老的单主键表迁移到新的复合主键表。

        老数据全部归到 scene='group'（现存记录都是群聊产生的）。
        迁移完把老表改名备份，不删 —— 万一要回滚还能用。
        """
        try:
            tables = {
                r[0] for r in
                con.execute("SELECT name FROM sqlite_master "
                            "WHERE type='table'").fetchall()
            }
        except Exception:  # noqa: BLE001
            return

        if "mood_state" in tables:
            try:
                con.execute(
                    MIGRATE_SQL.format(old_table="mood_state").replace(
                        "SELECT ?, 'group'", "SELECT user_id, 'group'")
                )
                con.execute("ALTER TABLE mood_state RENAME TO mood_state_legacy")
                print("[mood] 已迁移 mood_state -> mood_state_v2（统一归为 group）")
            except Exception as e:  # noqa: BLE001
                print(f"[mood] 迁移 mood_state 失败（可能已迁移过）: {e}")

        if "mood_history" in tables:
            try:
                con.execute(
                    "INSERT OR IGNORE INTO mood_history_v2 "
                    "(user_id, scene, ts, affection, warmth, irritation, "
                    " label, reason) "
                    "SELECT user_id, 'group', ts, affection, warmth, "
                    "irritation, label, reason FROM mood_history"
                )
                con.execute(
                    "ALTER TABLE mood_history RENAME TO mood_history_legacy")
                print("[mood] 已迁移 mood_history -> mood_history_v2")
            except Exception as e:  # noqa: BLE001
                print(f"[mood] 迁移 mood_history 失败（可能已迁移过）: {e}")

    def load(self, user_id: str, scene: str = SCENE_GROUP) -> MoodState:
        scene = scene if scene in SCENES else SCENE_GROUP
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM mood_state_v2 WHERE user_id = ? AND scene = ?",
                (user_id, scene),
            ).fetchone()
        if not row:
            return MoodState(user_id=user_id, scene=scene)
        st = MoodState(
            user_id=user_id,
            scene=scene,
            affection=row["affection"],
            warmth=row["warmth"],
            irritation=row["irritation"],
            base_warmth=row["base_warmth"],
            base_irritation=row["base_irritation"],
            turns=row["turns"],
            last_update=row["last_update"],
        )
        return advance(st)

    def save(self, state: MoodState) -> None:
        scene = state.scene if state.scene in SCENES else SCENE_GROUP
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """INSERT INTO mood_state_v2
                   (user_id, scene, affection, warmth, irritation,
                    base_warmth, base_irritation, turns, last_update)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, scene) DO UPDATE SET
                     affection=excluded.affection,
                     warmth=excluded.warmth,
                     irritation=excluded.irritation,
                     base_warmth=excluded.base_warmth,
                     base_irritation=excluded.base_irritation,
                     turns=excluded.turns,
                     last_update=excluded.last_update""",
                (state.user_id, scene, state.affection, state.warmth,
                 state.irritation, state.base_warmth,
                 state.base_irritation, state.turns, state.last_update),
            )

    # ---------------------------------------------------- 历史记录
    def record_history(self, state: MoodState, reason: str = "") -> None:
        """记一条情绪快照（曲线图用）。

        只在**情绪真的变了**的时候记，否则时间轴会被灌满重复点。
        """
        try:
            label = state.label()
            scene = state.scene if state.scene in SCENES else SCENE_GROUP
            with sqlite3.connect(self.db_path) as con:
                # 和上一条比，三个值都没明显变化就不记
                last = con.execute(
                    "SELECT affection, warmth, irritation "
                    "FROM mood_history_v2 "
                    "WHERE user_id = ? AND scene = ? ORDER BY ts DESC LIMIT 1",
                    (state.user_id, scene),
                ).fetchone()
                if last is not None:
                    if (abs(last[0] - state.affection) < 0.5
                            and abs(last[1] - state.warmth) < 0.5
                            and abs(last[2] - state.irritation) < 0.5):
                        return
                con.execute(
                    "INSERT INTO mood_history_v2 "
                    "(user_id, scene, ts, affection, warmth, irritation, "
                    " label, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (state.user_id, scene, state.last_update, state.affection,
                     state.warmth, state.irritation, label, reason or ""),
                )
        except Exception as e:  # noqa: BLE001
            # 历史记录失败绝不影响主流程
            try:
                from astrbot.api.all import logger as _lg  # type: ignore
            except Exception:  # noqa: BLE001
                import logging
                _lg = logging.getLogger("mood")
            _lg.debug(f"mood history 写入失败: {e}")

    def history(self, user_id: str, scene: str = SCENE_GROUP,
                limit: int = 100,
                since: Optional[float] = None) -> List[Dict[str, Any]]:
        """读某用户某场景的历史快照，按时间**正序**（画图方便）。"""
        scene = scene if scene in SCENES else SCENE_GROUP
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            if since is not None:
                rows = con.execute(
                    "SELECT * FROM mood_history_v2 "
                    "WHERE user_id = ? AND scene = ? AND ts >= ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, scene, since, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM mood_history_v2 "
                    "WHERE user_id = ? AND scene = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, scene, limit),
                ).fetchall()
        out = []
        for r in rows:
            out.append({
                "ts": r["ts"],
                "affection": round(r["affection"], 1),
                "warmth": round(r["warmth"], 1),
                "irritation": round(r["irritation"], 1),
                "label": r["label"] or "",
                "reason": r["reason"] or "",
            })
        out.reverse()   # 转成正序
        return out

    def prune_history(self, keep_days: int = 30) -> int:
        """清掉太旧的历史（防数据库无限长）。返回删除行数。"""
        cutoff = time.time() - keep_days * 86400
        try:
            with sqlite3.connect(self.db_path) as con:
                cur = con.execute(
                    "DELETE FROM mood_history_v2 WHERE ts < ?", (cutoff,))
                return cur.rowcount or 0
        except Exception:  # noqa: BLE001
            return 0

    def all_states(self, scene: Optional[str] = None) -> List[MoodState]:
        """读所有状态。scene=None 表示两个场景都读。"""
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            if scene and scene in SCENES:
                rows = con.execute(
                    "SELECT * FROM mood_state_v2 WHERE scene = ?", (scene,)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM mood_state_v2").fetchall()
        out = []
        for row in rows:
            st = MoodState(
                user_id=row["user_id"],
                scene=row["scene"] if "scene" in row.keys() else SCENE_GROUP,
                affection=row["affection"],
                warmth=row["warmth"],
                irritation=row["irritation"],
                base_warmth=row["base_warmth"],
                base_irritation=row["base_irritation"],
                turns=row["turns"],
                last_update=row["last_update"],
            )
            out.append(advance(st))
        return out
