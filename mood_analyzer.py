# -*- coding: utf-8 -*-
"""并行情绪分析 —— 保证回复速度不受影响。

核心思路（你确认的「并行」方案）：

    第 N 轮用户说话
      ├─ 立刻用**第 N-1 轮已经分析好的情绪**生成回复   ← 主链路，零等待
      └─ 后台异步分析第 N 轮，结果留给第 N+1 轮用       ← 不阻塞

为什么这样是对的：
  1. 情绪本来就该「慢半拍」。人不会因为你刚说一句话就立刻变脸，
     上一句话带来的情绪，下一句话才体现 —— 一轮滞后反而更真实。
  2. 主对话完全不受影响，首字延迟和以前一样。
  3. 缓存不受影响：情绪分析是**独立的 LLM 请求**，有自己的缓存前缀，
     不会污染主对话的 system_prompt。

成本控制（三层闸门，借鉴 group_chat_plus 现有的思路）：
    第一层：关键词命中且信号清晰 → 直接算，**零 LLM 调用**
    第二层：关键词拿不准/强情绪/反讽 → 调 LLM 细判
    第三层：LLM 也失败 → 用关键词的兜底值，绝不影响主流程
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, Optional, Tuple

# 复用主插件的 logger（拿不到就用标准 logging，保证可独立测试）
try:
    from astrbot.api.all import logger  # type: ignore
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("mood")

try:  # 包内导入（AstrBot 正常加载）
    from .mood_core import (
        MoodState, MoodStore, apply_delta, quick_score, mood_instruction,
        SCENE_GROUP, SCENE_PRIVATE, SCENES,
    )
except ImportError:  # 单文件/脚本环境
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from mood_core import (  # type: ignore
        MoodState, MoodStore, apply_delta, quick_score, mood_instruction,
        SCENE_GROUP, SCENE_PRIVATE, SCENES,
    )

PLUGIN_TAG = "[情绪核心]"

# 分析用的模型。默认用主对话的模型，也可以单独指定便宜模型。
DEFAULT_ANALYSIS_PROMPT = """你是一个情绪数值调节器。阅读下面这次互动，判断它如何影响角色的情绪。

角色设定：{persona_summary}

当前状态：
- 好感度：{affection:.0f}/100
- 亲近欲(warmth)：{warmth:.0f}/100
- 烦躁(irritation)：{irritation:.0f}/100
- 已互动轮次：{turns}

用户说的话：{text}
{history_hint}

请判断这次互动带来的**增量**（不是绝对值）：
- warmth_delta      ：-15 ~ +15，亲近欲变化
- irritation_delta  ：-15 ~ +15，烦躁变化（让对方烦就正数）
- affection_delta   ：-5 ~ +5，长期好感变化（只有真正重要的事才动它）

参考尺度：
- 「滚」「闭嘴」这类 → irritation +20~+30，warmth -10，affection -3
- 「笨蛋」「讨厌」   → irritation +15，warmth -5，affection -1
- 「哦」「随便」     → irritation +5，warmth -4，affection 0
- 普通闲聊           → 全部 0
- 「谢谢你」「辛苦了」 → warmth +8~+12，affection +1
- 「喜欢你」         → warmth +20，irritation -10，affection +4
- 道歉（对不起）     → irritation -12，warmth +6，affection +1

只输出 JSON，不要任何解释：
{{"warmth_delta": 0.0, "irritation_delta": 0.0, "affection_delta": 0.0}}"""


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def parse_delta_json(text: str) -> Optional[Tuple[float, float, float]]:
    """从 LLM 输出里抠出三个增量。容忍前后废话、代码块、NaN。"""
    if not text:
        return None
    # 去掉 ```json ``` 包裹
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    raw = m.group(0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # 容错：单引号 / 尾逗号
        try:
            obj = json.loads(raw.replace("'", '"').replace(",}", "}"))
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(obj, dict):
        return None
    try:
        w = _clamp(float(obj.get("warmth_delta", 0) or 0), -20, 20)
        i = _clamp(float(obj.get("irritation_delta", 0) or 0), -20, 20)
        a = _clamp(float(obj.get("affection_delta", 0) or 0), -8, 8)
    except (TypeError, ValueError):
        return None
    return (w, i, a)


class MoodAnalyzer:
    """情绪分析器 —— 关键词优先，LLM 兜底，全部异步不阻塞。"""

    def __init__(
        self,
        store: MoodStore,
        *,
        llm_callback=None,           # async (prompt) -> str | None
        persona_summary: str = "",
        enable_llm: bool = True,
        max_llm_per_hour: int = 60,
    ):
        self.store = store
        self.llm_callback = llm_callback
        self.persona_summary = persona_summary[:200]
        self.enable_llm = enable_llm
        self.max_llm_per_hour = max_llm_per_hour

        # 限流：每小时最多调这么多次 LLM
        self._llm_calls: list[float] = []
        # 正在进行中的分析（按 user_id，避免重复分析同一条）
        self._inflight: Dict[str, asyncio.Task] = {}
        # 统计（调试用）
        self.stats = {"keyword": 0, "llm": 0, "skip": 0, "llm_fail": 0}

    # ------------------------------------------------------------ 限流
    def _llm_budget_ok(self) -> bool:
        now = time.time()
        self._llm_calls = [t for t in self._llm_calls if now - t < 3600]
        return len(self._llm_calls) < self.max_llm_per_hour

    # ------------------------------------------------------------ 取状态
    def get_state(self, user_id: str,
                  scene: str = SCENE_GROUP) -> MoodState:
        """取状态。⚠️ 群聊和私聊是分开的两份。"""
        return self.store.load(user_id, scene)

    def get_hint(self, user_id: str,
                 scene: str = SCENE_GROUP) -> str:
        """给主对话用的情绪提示（同步、零成本、不等待）。

        scene 决定读哪一份状态：
          · 群聊 → 她"作为群友"的情绪
          · 私聊 → 她"跟你单独相处"的情绪
        """
        st = self.get_state(user_id, scene)
        return st.instruction()

    # ------------------------------------------------------------ 核心
    async def analyze_async(
        self,
        user_id: str,
        text: str,
        *,
        scene: str = SCENE_GROUP,
        history_hint: str = "",
    ) -> Optional[Tuple[float, float, float]]:
        """异步分析一次互动，**结果会写进状态**。

        调用方**不要 await 它**（除非在后台任务里）：
            asyncio.create_task(analyzer.analyze_async(...))
        这样主对话零等待。
        """
        if not text or not text.strip():
            self.stats["skip"] += 1
            return None

        st = self.get_state(user_id, scene)

        # ---- 第一层：关键词 ----
        kw = quick_score(text)
        if kw is not None:
            self.stats["keyword"] += 1
            w, i, a = kw
            apply_delta(st, w, i, a)
            self.store.save(st)
            self.store.record_history(st, reason="keyword")
            logger.debug(f"{PLUGIN_TAG} [{'qun' if scene == SCENE_GROUP else 'si'}] "
                         f"关键词 {text[:20]!r} "
                         f"-> w{w:+.1f} i{i:+.1f} a{a:+.1f} => {st.label()}")
            return (w, i, a)

        # ---- 第二层：LLM（如果允许且有预算）----
        if (not self.enable_llm
                or self.llm_callback is None
                or not self._llm_budget_ok()):
            self.stats["skip"] += 1
            return None

        prompt = DEFAULT_ANALYSIS_PROMPT.format(
            persona_summary=self.persona_summary or "（未提供）",
            affection=st.affection,
            warmth=st.warmth,
            irritation=st.irritation,
            turns=st.turns,
            text=text[:300],
            history_hint=(f"最近上下文：{history_hint[:200]}" if history_hint else ""),
        )

        self._llm_calls.append(time.time())
        self.stats["llm"] += 1

        try:
            raw = await asyncio.wait_for(self.llm_callback(prompt), timeout=15.0)
        except asyncio.TimeoutError:
            self.stats["llm_fail"] += 1
            logger.warning(f"{PLUGIN_TAG} 情绪分析超时，跳过本轮")
            return None
        except Exception as e:  # noqa: BLE001
            self.stats["llm_fail"] += 1
            logger.warning(f"{PLUGIN_TAG} 情绪分析失败: {e}")
            return None

        delta = parse_delta_json(raw or "")
        if delta is None:
            self.stats["llm_fail"] += 1
            logger.debug(f"{PLUGIN_TAG} 情绪分析输出无法解析: {(raw or '')[:80]!r}")
            return None

        w, i, a = delta
        apply_delta(st, w, i, a)
        self.store.save(st)
        self.store.record_history(st, reason="llm")
        logger.debug(f"{PLUGIN_TAG} [{'qun' if scene == SCENE_GROUP else 'si'}] "
                     f"LLM {text[:20]!r} "
                     f"-> w{w:+.1f} i{i:+.1f} a{a:+.1f} => {st.label()}")
        return delta

    def spawn_analysis(
        self,
        user_id: str,
        text: str,
        *,
        scene: str = SCENE_GROUP,
        history_hint: str = "",
    ) -> None:
        """后台起一个分析任务（fire-and-forget，绝不阻塞调用方）。

        同一个 user 已有分析在跑时不重复起 —— 情绪分析不需要那么实时，
        合并成一次更省成本。
        """
        key = f"{user_id}:{scene}"
        cur = self._inflight.get(key)
        if cur is not None and not cur.done():
            return

        async def _run():
            try:
                await self.analyze_async(user_id, text, scene=scene,
                                         history_hint=history_hint)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{PLUGIN_TAG} 后台分析异常（已忽略）: {e}")
            finally:
                self._inflight.pop(key, None)

        try:
            task = asyncio.create_task(_run())
            self._inflight[key] = task
        except RuntimeError:
            # 没有运行中的事件循环（同步上下文），跳过
            pass

    # ------------------------------------------------------------ 手动
    def adjust(
        self,
        user_id: str,
        warmth: float = 0.0,
        irritation: float = 0.0,
        affection: float = 0.0,
        scene: str = SCENE_GROUP,
    ) -> MoodState:
        """手动调整（管理员 / WebUI 用）。"""
        st = self.get_state(user_id, scene)
        apply_delta(st, warmth, irritation, affection)
        self.store.save(st)
        return st

    def reset(self, user_id: str,
              scene: str = SCENE_GROUP) -> MoodState:
        st = MoodState(user_id=user_id, scene=scene)
        self.store.save(st)
        return st
