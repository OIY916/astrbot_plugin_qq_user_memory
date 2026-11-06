# -*- coding: utf-8 -*-
"""情绪系统对接层 —— 让「跨群用户记忆」插件自己拥有好感度与情绪值。

2026-11-04 用户决定：把情绪系统（好感度 + 情绪值）从
group_chat_plus **整体搬进**本插件，最终只留一个插件。

搬过来的东西：
  · mood_core.py      双驱模型（warmth 亲近 × irritation 烦躁 + affection）
  · mood_analyzer.py  关键词优先 / LLM 兜底 / 并行异步分析
  · affinity_view.py  好感度档位与文本格式化
  · mood_dual.db      情绪库（mood_state_v2 复合主键 (user_id, scene)）

为什么搬进这里而不是留在 gcp：
  · 用户要的是「在跨群用户记忆里看到人、好感度、情绪」——一个面板
  · 本插件本来就是**按 QQ 号聚合**的（users 表主键是 QQ），
    好感度和情绪天生是「这个人」的属性，不是「这个群」的属性
  · 本插件有 on_llm_request 钩子，注入可以自己完成，不需要跨插件调用
  · 本插件是用户自己的仓库，可以发布到插件商店（gcp 是第三方插件）

设计：本模块只提供 EmotionHub 这个门面类，把三件套的繁琐初始化
收进一处。main.py 只管调用，不关心内部细节。

⚠️ 群聊和私聊是**两份独立状态**（mood_state_v2 的复合主键），
   scene 必须是 "group" 或 "private"，否则会读到 group。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 单独测试
    import logging

    logger = logging.getLogger("user_memory.emotion")

# ---- 两个导入风格都兼容（包内 / 单文件）----
try:
    from .mood_core import (
        MoodStore, advance, SCENE_GROUP, SCENE_PRIVATE, SCENES,
    )
    from .mood_analyzer import MoodAnalyzer
    from .affinity_view import affinity_tier
except ImportError:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mood_core import (  # type: ignore
        MoodStore, advance, SCENE_GROUP, SCENE_PRIVATE, SCENES,
    )
    from mood_analyzer import MoodAnalyzer  # type: ignore
    from affinity_view import affinity_tier  # type: ignore

PLUGIN_TAG = "[情绪]"


# ====================================================================
# 场景判断
#
# ⚠️⚠️ 这里踩过一个非常隐蔽的坑（2026-11-04 用户实测发现）：
#   `event.get_message_type()` 返回 **MessageType**
#   （astrbot.core.platform.message_type，值是 "FriendMessage"），
#   装饰器用的 **EventMessageType**（astrbot.api.event.filter，Flag，
#   成员是 PRIVATE_MESSAGE）是**另一个枚举类**。
#   拿它们直接比较永远 False → 全部误判成 group。
#   所以**必须**用 MessageType，再加字符串兜底防版本变化。
# ====================================================================

def scene_of(event: Any) -> str:
    """判断这条消息属于 group 还是 private。四层兜底。"""
    # ---- 第一层：正确的枚举 ----
    try:
        from astrbot.core.platform.message_type import (  # type: ignore
            MessageType as _MT,
        )
        mt = event.get_message_type()
        if mt == _MT.FRIEND_MESSAGE:
            return SCENE_PRIVATE
        if mt == _MT.GROUP_MESSAGE:
            return SCENE_GROUP
    except Exception:  # noqa: BLE001
        pass

    # ---- 第二层：按字符串值比 ----
    try:
        mt = event.get_message_type()
        raw = getattr(mt, "value", mt)
        low = str(raw).lower()
        if "friend" in low or "private" in low:
            return SCENE_PRIVATE
        if "group" in low:
            return SCENE_GROUP
    except Exception:  # noqa: BLE001
        pass

    # ---- 第三层：从 unified_msg_origin 猜 ----
    # UMO 形如 default:FriendMessage:2252572250
    try:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if "FriendMessage" in umo or "PrivateMessage" in umo:
            return SCENE_PRIVATE
        if "GroupMessage" in umo:
            return SCENE_GROUP
    except Exception:  # noqa: BLE001
        pass

    # ---- 第四层：session.message_type ----
    try:
        sess = getattr(event, "session", None)
        st = str(getattr(sess, "message_type", "") or "").lower()
        if "friend" in st or "private" in st:
            return SCENE_PRIVATE
        if "group" in st:
            return SCENE_GROUP
    except Exception:  # noqa: BLE001
        pass

    return SCENE_GROUP


class EmotionHub:
    """好感度 + 情绪值的门面。

    一个插件实例持有一个 EmotionHub；内部持有 MoodStore 和 MoodAnalyzer。
    所有方法都做了异常兜底 —— 情绪系统挂了绝不能影响记忆和聊天。
    """

    def __init__(
        self,
        db_path: str,
        *,
        context: Any = None,
        enable_llm: bool = True,
        max_llm_per_hour: int = 60,
        persona_summary: str = "",
    ):
        self.db_path = db_path
        self.context = context
        self._last_umo = ""

        self.store: Optional[MoodStore] = None
        self.analyzer: Optional[MoodAnalyzer] = None
        self.ready = False
        self.init_error = ""

        try:
            self.store = MoodStore(db_path)
            self.analyzer = MoodAnalyzer(
                self.store,
                llm_callback=self._llm_callback,
                persona_summary=persona_summary,
                enable_llm=enable_llm,
                max_llm_per_hour=max_llm_per_hour,
            )
            self.ready = True
        except Exception as e:  # noqa: BLE001
            self.init_error = str(e)
            self.store = None
            self.analyzer = None
            logger.error(f"{PLUGIN_TAG} 初始化失败: {e}", exc_info=True)

    # ------------------------------------------------------------ LLM
    async def _llm_callback(self, prompt: str) -> Optional[str]:
        """情绪分析专用 LLM 回调（独立请求，不碰主对话上下文）。

        关键：**不发 contexts**，只有一句 system + 一条 user。
        所以它有自己的缓存前缀，不会污染主对话的 KV Cache。
        """
        if self.context is None:
            return None
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                self._last_umo or ""
            )
        except Exception:  # noqa: BLE001
            provider_id = ""
        if not provider_id:
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                contexts=[],
                system_prompt="你是情绪数值调节器，只输出 JSON，不要任何解释。",
            )
            return getattr(resp, "completion_text", None)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} LLM 回调失败: {e}")
            return None

    # ------------------------------------------------------------ 取数
    def get_state(self, qq: str, scene: str = SCENE_GROUP):
        """取某人某场景的情绪状态；失败返回 None。"""
        if not self.ready or self.store is None:
            return None
        try:
            return self.store.load(str(qq), scene)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} 读状态失败: {e}")
            return None

    def get_hint(self, qq: str, scene: str = SCENE_GROUP) -> str:
        """给主对话用的情绪提示（同步、零成本、不等待）。"""
        if not self.ready or self.analyzer is None:
            return ""
        try:
            return self.analyzer.get_hint(str(qq), scene)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} 取提示失败: {e}")
            return ""

    def spawn_analysis(
        self,
        qq: str,
        text: str,
        *,
        scene: str = SCENE_GROUP,
        history_hint: str = "",
        umo: str = "",
    ) -> None:
        """后台异步分析（第 N 轮的结果第 N+1 轮用，绝不阻塞回复）。"""
        if not self.ready or self.analyzer is None:
            return
        if umo:
            self._last_umo = umo
        try:
            self.analyzer.spawn_analysis(
                str(qq), text, scene=scene, history_hint=history_hint
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} 启动分析失败: {e}")

    # ------------------------------------------------------------ 好感度
    def affinity_rows(self, limit: int = 10, scene: Optional[str] = None) -> List[Dict[str, Any]]:
        """好感度排行（按 affection 降序）。"""
        if not self.ready or self.store is None:
            return []
        try:
            states = self.store.all_states(scene)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} 排行读取失败: {e}")
            return []
        states.sort(key=lambda s: -s.affection)
        return [
            {
                "qq": st.user_id,
                "score": st.affection,
                "warmth": st.warmth,
                "irritation": st.irritation,
                "label": st.label(),
                "turns": st.turns,
                "scene": st.scene,
            }
            for st in states[:limit]
        ]

    def set_affection(self, qq: str, value: float,
                      scene: str = SCENE_GROUP) -> bool:
        """手动设定好感度（基线一起走，不然双驱模型会把她拉回 50）。"""
        if not self.ready or self.store is None:
            return False
        try:
            val = max(0.0, min(100.0, float(value)))
            st = self.store.load(str(qq), scene)
            st.affection = val
            st.base_warmth = max(0.0, min(100.0, 50.0 + (val - 50.0) * 0.4))
            st.last_update = time.time()
            self.store.save(st)
            self.store.record_history(st, reason="manual_affinity")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{PLUGIN_TAG} 设定好感度失败: {e}")
            return False

    def tier_of(self, score: float) -> str:
        """好感度档位名。"""
        try:
            name, _ = affinity_tier(score)
            return name
        except Exception:  # noqa: BLE001
            return ""

    def all_scene_states(self, qq: str) -> Dict[str, Any]:
        """取某人两个场景的状态（面板用）。"""
        out: Dict[str, Any] = {}
        for sc in SCENES:
            st = self.get_state(qq, sc)
            if st is not None:
                out[sc] = st
        return out

    def history(self, qq: str, scene: str = SCENE_GROUP,
                limit: int = 50) -> List[Dict[str, Any]]:
        """情绪变化曲线数据。"""
        if not self.ready or self.store is None:
            return []
        try:
            return self.store.history(str(qq), scene, limit=limit)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{PLUGIN_TAG} 读历史失败: {e}")
            return []

    def close(self) -> None:
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
