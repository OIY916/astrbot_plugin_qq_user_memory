# -*- coding: utf-8 -*-
"""好感度展示层 —— 从 qq_user_memory 搬过来的。

2026-11-04 用户要求：把好感度模块整个搬到 group_chat_plus，
不再由 qq_user_memory 维护（那边只保留记忆功能）。

本模块只负责**格式化和命令**，数据来自 utils/mood_core.py 的双驱情绪系统
（mood_state_v2.affection 是唯一真相源）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# ====================================================================
# 好感度档位（从 qq_user_memory/memory_store.py 原样搬过来）
# ====================================================================

AFFINITY_MIN = 0.0
AFFINITY_MAX = 100.0
AFFINITY_DEFAULT = 50.0   # 双驱模型的默认值是 50（原来是 40）

# (阈值, 档位名, 行为描述)
AFFINITY_TIERS: List[Tuple[float, str, str]] = [
    (95.0, "死心塌地", "你说什么她都听，哪怕明显在胡闹也最多嘴上嘟囔两句"),
    (85.0, "很喜欢", "会主动关心你，愿意迁就，被冷落会明显不高兴"),
    (70.0, "有好感", "愿意接你的话茬，偶尔主动搭话，但还端着"),
    (55.0, "还行", "正常聊天，不冷不热，不会特意照顾你的情绪"),
    (40.0, "无感", "公事公办，你说话她就回，懒得延伸"),
    (25.0, "有点烦", "回复变短、变敷衍，能不接就不接"),
    (10.0, "很烦", "几乎不主动理你，回复冷硬或者干脆不回"),
    (0.0, "反感", "见你就烦，可能会直接呛人"),
]


def affinity_tier(score: float) -> Tuple[str, str]:
    """好感度分数 → (档位名, 行为描述)。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = AFFINITY_DEFAULT
    for threshold, name, desc in AFFINITY_TIERS:
        if s >= threshold:
            return name, desc
    return AFFINITY_TIERS[-1][1], AFFINITY_TIERS[-1][2]


# ====================================================================
# 文本格式化（从 qq_user_memory/main.py 搬过来，去掉锁定相关）
# ====================================================================

def affinity_detail_text(
    qq: str,
    score: float,
    reason: str = "",
    *,
    warmth: float | None = None,
    irritation: float | None = None,
    label: str = "",
    turns: int = 0,
) -> str:
    """单人好感度详情。

    比原来的版本多了双驱信息（亲近/烦躁/情绪标签），
    因为现在数据源是双驱模型，这些维度是现成的。
    """
    tier, desc = affinity_tier(score)
    bar = _bar(score)
    lines = [
        f"【好感度】QQ {qq}",
        "",
        f"好感度：{score:.0f}/100（{tier}）",
        f"{bar}",
        f"→ {desc}",
    ]
    if warmth is not None and irritation is not None:
        lines.append("")
        lines.append(f"亲近欲：{warmth:.0f}/100  {_bar(warmth)}")
        lines.append(f"烦躁：  {irritation:.0f}/100  {_bar(irritation)}")
        if label:
            lines.append(f"当前情绪：{label}")
    if turns:
        lines.append(f"互动轮次：{turns}")
    if reason:
        lines.append(f"最近变动：{reason}")
    return "\n".join(lines)


def affinity_rank_text(rows: List[Dict[str, Any]], limit: int = 10) -> str:
    """好感度排行榜文本。"""
    if not rows:
        return "还没有任何好感度记录。"
    lines = [f"【好感度排行】前 {min(len(rows), limit)} 名", ""]
    for i, r in enumerate(rows[:limit], 1):
        tier, _ = affinity_tier(r.get("score", 0))
        nick = str(r.get("nickname") or "").strip()
        name = f"{nick}（{r['qq']}）" if nick else str(r["qq"])
        lines.append(f"{i}. {name}  {r.get('score', 0):.0f}  [{tier}]")
    return "\n".join(lines)


def _bar(score: float, width: int = 20) -> str:
    """画一个 ASCII 进度条。"""
    try:
        s = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        s = 0.0
    filled = int(round(s / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


# ====================================================================
# 从双驱情绪状态构造好感度视图
# ====================================================================

def build_affinity_view(state) -> Dict[str, Any]:
    """把 MoodState 转成好感度视图（给命令和 WebUI 用）。"""
    try:
        from .mood_core import affection_level
    except ImportError:
        from mood_core import affection_level  # type: ignore

    return {
        "qq": state.user_id,
        "score": round(state.affection, 1),
        "warmth": round(state.warmth, 1),
        "irritation": round(state.irritation, 1),
        "label": state.label(),
        "turns": state.turns,
        "tier": affinity_tier(state.affection)[0],
        "tier_desc": affinity_tier(state.affection)[1],
        "level": affection_level(state.affection),
    }
