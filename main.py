# -*- coding: utf-8 -*-
"""跨群用户记忆插件（astrbot_plugin_qq_user_memory）

解决的问题：机器人换个群就不认识人了。
做法：以 **QQ 号** 为主键，记录「谁、在哪个群、说过什么」，
      跨群自动合并成同一个人的档案，并在每次回复前把这份记忆喂给模型。

技术路线（见 STATUS.md「零、当前任务」）：
- 不用 embedding（DeepSeek 没有 embedding API，且要省钱）
- 用 SQLite FTS5 全文检索，中文逐字切分（segment_cjk）
- 注入 **req.prompt**，不要动 req.system_prompt
  （group_chat_plus 的第三方插件兼容层会自动打标记合并，零冲突）

指令：
  /记忆 查 <QQ号或昵称>     看某个人的完整档案
  /记忆 搜 <关键词>          全文检索所有群里的发言
  /记忆 记住 <QQ号> <内容>   手动记一条事实
  /记忆 忘记 <QQ号>          删除某个人的全部记忆（管理员）
  /记忆 统计                 看库里有多少数据
  /记忆 最近                 看最近记录情况
  /记忆 更新画像 [QQ号]      立刻生成/刷新画像（管理员）

每日画像（方向 B，profiler.py）：
  每天 profile_hour 点（默认 3:00）把活跃用户的发言归纳成 2-3 句画像，
  写进 facts 表（source='profile'），注入时作为【画像】展示。
  固定用 deepseek-chat，不用当前会话的模型。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 不同版本 AstrBot 的导入路径略有差异，做兼容
    from astrbot.core.star.star_tools import StarTools
except Exception:  # pragma: no cover
    StarTools = None  # type: ignore

try:
    from .memory_store import (
        AFFINITY_DEFAULT,
        ContextBuilder,
        MemoryStore,
        affinity_tier,
        fmt_time,
        group_label,
        search_text,
    )
except ImportError:  # 直接当脚本/单文件测试时走绝对导入
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from memory_store import (  # type: ignore
        AFFINITY_DEFAULT,
        ContextBuilder,
        MemoryStore,
        affinity_tier,
        fmt_time,
        group_label,
        search_text,
    )

try:  # 画像模块（方向 B）
    from . import profiler as profiler_mod
except ImportError:  # 直接当脚本跑时走同目录导入
    try:
        import profiler as profiler_mod  # type: ignore
    except Exception:  # pragma: no cover - 画像模块缺失也不能让插件挂掉
        profiler_mod = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_qq_user_memory"

# 好感度的可见性由配置项 affinity_viewer_qq 决定（留空=所有人可查），
# 不要在代码里写死个人 QQ 号。
AFFINITY_VIEWER_QQ = ""


def strip_forbidden_names(text: str, names: list[str] | None = None) -> str:
    """把禁用称呼从文本里去掉（连同紧跟的「的/，/。」等）。

    names 由配置项 forbidden_names 提供，默认空 = 不过滤。
    例：names=["张三"] 时，「张三是个好人」→「是个好人」；
        「再说一次 张三」→「再说一次」
    """
    if not text or not names:
        return text
    out = text
    for name in names:
        if not name:
            continue
        out = re.sub(rf"{re.escape(name)}[的，,、。！!？?\s]*", "", out)
    return out

# 记录时跳过的内容：纯表情/纯图片占位符等
_SKIP_PATTERNS = (
    re.compile(r"^\[图片\]$"),
    re.compile(r"^\[表情[:：]?\d*\]$"),
    re.compile(r"^\[At[:：]?\d+\]$"),
    re.compile(r"^\[转发消息\]$"),
    re.compile(r"^[\[\(（【].*[\]\)）】]$"),  # 全是占位符
)

# 指令里常见的干扰词，检索前剔掉
_QUERY_NOISE = ("记忆", "一下", "帮我", "看看", "查询", "查查", "搜索", "搜一下", "吗", "呢", "的")


def _clean_content(text: str) -> str:
    """清洗一条消息文本，判断值不值得记。

    AstrBot 会把 @ 提取成 `@昵称(QQ号)` 塞进 message_str（见
    aiocqhttp_platform_adapter 的 at_parts 逻辑），这里还原成「@昵称」，
    否则 QQ 号会被当成正文关键词污染 FTS 检索。
    """
    if not text:
        return ""
    t = text.strip()
    if not t or len(t) > 1000:
        return ""
    # @昵称(QQ号) -> @昵称；@(QQ号) 这种没昵称的直接去掉
    t = re.sub(r"@([^\s()]*)\((\d{5,12})\)", lambda m: f"@{m.group(1)}" if m.group(1) else "", t)
    # 消息链占位符
    t = re.sub(r"\[At[:：]?\d+\]", "", t)
    t = re.sub(r"\[图片\]", "", t)
    t = re.sub(r"\[表情[:：]?\d*\]", "", t)
    t = re.sub(r"\[回复消息[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    for pat in _SKIP_PATTERNS:
        if pat.match(t):
            return ""
    # 太像指令的不记（/xxx 斜杠指令）
    if t.startswith("/") or t.startswith("／"):
        return ""
    return t[:1000]


def _extract_qq(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{5,12})(?!\d)", text or "")
    return m.group(1) if m else None


def _parse_hhmm(raw: Any) -> tuple[int | None, int]:
    """把 "HH:MM" / "2:05" / "02：30"（中文冒号）解析成 (时, 分)。

    解析失败返回 (None, 0)，由调用方决定回退策略。
    也接受纯整数（如 2 表示 02:00）。
    """
    if raw is None:
        return None, 0
    s = str(raw).strip().replace("：", ":")
    if not s:
        return None, 0
    # 纯数字（没有冒号）当作小时
    if ":" not in s:
        try:
            h = int(s)
        except ValueError:
            return None, 0
        if 0 <= h <= 23:
            return h, 0
        return None, 0
    parts = s.split(":")
    if len(parts) != 2:
        return None, 0
    try:
        h = int(parts[0].strip())
        m = int(parts[1].strip())
    except ValueError:
        return None, 0
    if not (0 <= h <= 23) or not (0 <= m <= 59):
        return None, 0
    return h, m


def _fmt_hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


@register(
    PLUGIN_NAME,
    "dafish",
    "跨群用户记忆 —— 记住每个 QQ 号是谁、说过什么，换个群也认得",
    "1.0.1",
)
class QQUserMemoryPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self.store: MemoryStore | None = None
        self.builder: ContextBuilder | None = None
        self._init_error: str | None = None
        # 每日画像定时任务（在 initialize() 里起，terminate() 里取消）
        self._profile_task: asyncio.Task | None = None
        self._last_profile_at: float = 0.0
        self._last_profile_result: dict[str, int] | None = None
        try:
            self._init_store()
        except Exception as e:  # 存储挂了也不能让整个插件初始化中断
            self._init_error = str(e)
            logger.error(f"[user_memory] 存储初始化失败: {e}", exc_info=True)

    # ------------------------------------------------------------ 基础
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _forbidden_names(self) -> list[str]:
        """从配置读禁用称呼列表（字符串用逗号/空格分隔也接受）。"""
        raw = self._cfg("forbidden_names", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        out: list[str] = []
        for item in raw or []:
            name = str(item or "").strip()
            if name and name not in out:
                out.append(name)
        return out

    def _data_dir(self) -> str:
        if StarTools is not None:
            try:
                return str(StarTools.get_data_dir(PLUGIN_NAME))
            except Exception:
                pass
        # 兜底：data/plugin_data/<插件名>
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(root, "plugin_data", PLUGIN_NAME)
        os.makedirs(d, exist_ok=True)
        return d

    def _init_store(self) -> None:
        db_name = str(self._cfg("db_file", "user_memory.db") or "user_memory.db")
        db_path = os.path.join(self._data_dir(), db_name)
        self.store = MemoryStore(db_path)
        self.builder = ContextBuilder(self.store)
        st = self.store.stats()
        logger.info(
            f"[user_memory] 记忆库就绪: {db_path} "
            f"({st['users']} 人 / {st['messages']} 条发言 / {st['facts']} 条事实)"
        )

    def _ready(self) -> bool:
        if self.store is not None and self.builder is not None:
            return True
        if self._init_error:
            try:  # 允许重试（比如首次启动时目录还没建好）
                self._init_store()
                return self.store is not None
            except Exception as e:
                logger.error(f"[user_memory] 存储重连失败: {e}")
        return False

    def _enabled(self, event: AstrMessageEvent | None = None) -> bool:
        if not self._cfg("enable", True):
            return False
        groups = self._cfg("enabled_groups", []) or []
        if groups and event is not None:
            gid = self._group_id(event)
            if gid and str(gid) not in {str(g) for g in groups}:
                return False
        return True

    # ------------------------------------------------------------ 事件取数
    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _group_name(event: AstrMessageEvent) -> str:
        try:
            g = getattr(getattr(event, "message_obj", None), "group", None)
            if g is None:
                return ""
            return str(getattr(g, "group_name", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _is_self(event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) == str(event.get_self_id())
        except Exception:
            return False

    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        try:
            return (event.message_str or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------ 每日画像
    async def _restart_profile_task(self):
        """取消现有定时任务并按当前配置重排（改时间后调用）。"""
        task = self._profile_task
        self._profile_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if profiler_mod is None or not self._cfg("enable_daily_profile", True):
            return
        self._profile_task = asyncio.create_task(self._daily_profile_loop())
        _h, _m = self._profile_time()
        logger.info(f"[user_memory] 画像定时任务已重排：每天 {_fmt_hhmm(_h, _m)}")

    async def initialize(self):
        """插件激活时起每日画像定时任务（纯 asyncio，不引入 apscheduler）。"""
        if profiler_mod is None:
            logger.warning("[user_memory] profiler 模块不可用，每日画像已禁用")
            return
        if not self._cfg("enable_daily_profile", True):
            logger.info("[user_memory] 每日画像已关闭（enable_daily_profile=false）")
            return
        if self._profile_task is not None and not self._profile_task.done():
            return
        self._profile_task = asyncio.create_task(self._daily_profile_loop())
        _h, _m = self._profile_time()
        logger.info(
            f"[user_memory] 每日画像定时任务已启动：每天 "
            f"{_fmt_hhmm(_h, _m)} 执行（模型 deepseek-chat）"
        )

    async def _daily_profile_loop(self):
        """sleep 到下一个 profile_time 指定时刻 → 跑一次 → 循环。"""
        try:
            while True:
                hh, mm = self._profile_time()
                wait = self._seconds_until(hh, mm)
                logger.info(
                    f"[user_memory] 下次画像将在 {wait / 3600:.1f} 小时后执行"
                    f"（每天 {hh:02d}:{mm:02d}）"
                )
                await asyncio.sleep(wait)
                try:
                    await self.run_profile_now()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[user_memory] 每日画像执行异常: {e}", exc_info=True)
                # 防止同一秒内再次触发（sleep 返回时刻恰好是目标时刻，时差 <1s）
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("[user_memory] 每日画像定时任务已取消")
            raise

    def _profile_time(self) -> tuple[int, int]:
        """读配置得到画像执行时刻 (时, 分)。

        优先用 profile_time（"HH:MM" 字符串，精确到分钟）；
        兼容旧配置 profile_hour（整数小时）。解析失败回退 00:00。
        """
        raw = self._cfg("profile_time", None)
        if raw not in (None, ""):
            hh, mm = _parse_hhmm(raw)
            if hh is not None:
                return hh, mm
            logger.warning(f"[user_memory] profile_time 格式无效: {raw!r}，回退 profile_hour")
        # 兼容老配置
        try:
            hour = int(self._cfg("profile_hour", 0))
        except Exception:
            hour = 0
        return min(23, max(0, hour)), 0

    @staticmethod
    def _seconds_until(hour: int, minute: int = 0) -> float:
        """距离下一个 hour:minute 还有多少秒（今天还没到就等今天）。"""
        now = time.localtime()
        target = time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, int(hour), int(minute), 0, 0, 0, -1)
        )
        if target <= time.time():
            target += 86400
        return max(60.0, target - time.time())

    async def run_profile_now(
        self,
        min_messages: int | None = None,
        max_users: int | None = None,
    ) -> dict[str, int]:
        """立刻跑一次批量画像，返回 {'ok','failed','users'}。"""
        if profiler_mod is None:
            logger.error("[user_memory] profiler 模块不可用，无法生成画像")
            return {"ok": 0, "failed": 0, "users": 0}
        if not self._ready():
            logger.error("[user_memory] 记忆库未就绪，跳过画像")
            return {"ok": 0, "failed": 0, "users": 0}
        res = await profiler_mod.run_daily_profiling(
            self.store,
            min_messages=int(
                min_messages
                if min_messages is not None
                else self._cfg("profile_min_messages", 10)
            ),
            max_users=int(
                max_users if max_users is not None else self._cfg("profile_max_users", 50)
            ),
            recent_n=int(self._cfg("profile_recent_n", 40)),
        )
        self._last_profile_at = time.time()
        self._last_profile_result = dict(res)
        return res

    # ------------------------------------------------------------ 1. 记录
    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def record_message(self, event: AstrMessageEvent):
        """记录每一条群消息。高优先级，先存下来再让别的插件处理。"""
        if not self._ready() or not self._enabled(event):
            return
        if self._is_self(event):
            return

        qq = ""
        try:
            qq = str(event.get_sender_id() or "")
        except Exception:
            return
        if not qq or not qq.isdigit():
            return

        content = _clean_content(self._text(event))
        if not content:
            return

        group_id = self._group_id(event)
        if not group_id and not self._cfg("record_private", True):
            return
        # 群聊白名单（只记录白名单群的发言）
        groups = self._cfg("enabled_groups", []) or []
        if groups and group_id and group_id not in {str(g) for g in groups}:
            return

        nickname = ""
        try:
            nickname = str(event.get_sender_name() or "")
        except Exception:
            pass

        try:
            ok = self.store.record_message(
                qq=qq,
                nickname=nickname,
                group_id=group_id,
                group_name=self._group_name(event),
                content=content,
                max_per_user=int(self._cfg("max_messages_per_user", 2000)),
            )
            if ok and self._cfg("retention_days", 180):
                n = self.store.maybe_prune(int(self._cfg("retention_days", 180)))
                if n:
                    logger.info(f"[user_memory] 清理过期发言 {n} 条")
        except Exception as e:
            logger.error(f"[user_memory] 记录发言失败: {e}", exc_info=True)

    # ------------------------------------------------------------ 2. 注入
    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req):
        """把当前发言人的跨群记忆注入 req.prompt。

        ⚠️ 必须写 req.prompt，不能写 req.system_prompt
        （group_chat_plus 的 SystemPromptRewriter 会重复）。
        """
        if not self._ready() or not self._enabled(event):
            return
        if not self._cfg("inject_memory", True):
            return
        if not self._cfg("inject_on_all", False):
            # 默认只在被 @ / 唤醒时注入，避免每条读空气消息都吃 token
            if not getattr(event, "is_at_or_wake_command", False):
                return

        qq = ""
        try:
            qq = str(event.get_sender_id() or "")
        except Exception:
            return
        if not qq:
            return

        nickname = ""
        try:
            nickname = str(event.get_sender_name() or "")
        except Exception:
            pass

        try:
            block = self.builder.build(
                qq=qq,
                nickname=nickname,
                group_id=self._group_id(event),
                current_text=self._text(event),
                max_chars=int(self._cfg("max_inject_chars", 700)),
                recent_limit=int(self._cfg("recent_limit", 5)),
                related_limit=int(self._cfg("related_limit", 3)),
                facts_limit=int(self._cfg("facts_limit", 6)),
                min_messages=int(self._cfg("min_messages_before_inject", 3)),
            )
        except Exception as e:
            logger.error(f"[user_memory] 构造记忆失败: {e}", exc_info=True)
            return

        if not block:
            return

        try:
            req.prompt = (req.prompt or "") + "\n\n" + block
            logger.info(
                f"[user_memory] 已注入记忆 QQ={qq} 长度={len(block)} "
                f"(会话 {self._group_id(event) or '私聊'})"
            )
        except Exception as e:
            logger.error(f"[user_memory] 注入 prompt 失败: {e}")

    # ------------------------------------------------------------ 3. 指令
    def _resolve_qq(self, token: str) -> str | None:
        """把手输的 QQ 号或昵称解析成 QQ 号。

        用户可能输入：`123456789`、`@小明`、`小明`、`小明(123456789)`。
        先按「确实是已记录的人」验证，验证不到再退回纯数字。
        """
        token = (token or "").strip()
        if not token:
            return None
        # 昵称(QQ号) / @昵称(QQ号) 形式优先取括号里的号
        m = re.search(r"\((\d{5,12})\)", token)
        if m and self.store.get_user(m.group(1)):
            return m.group(1)
        qq = _extract_qq(token)
        if qq and self.store.get_user(qq):
            return qq
        # 昵称反查（去掉 @ 和末尾的括号部分）
        bare = re.sub(r"\((\d{5,12})\)", "", token).strip().lstrip("@").strip()
        if bare:
            hits = self.store.find_qq_by_nickname(bare)
            if hits:
                return hits[0]
            # 昵称可能含空格导致只传进来第一个词，做一次前缀匹配
            hits = self.store.find_qq_by_nickname_prefix(bare)
            if hits:
                return hits[0]
        return qq

    @filter.command("记忆")
    async def memory_cmd(self, event: AstrMessageEvent, sub: str = "", rest: str = ""):
        """记忆指令入口。

        AstrBot 的唤醒前缀（/）在 WakingCheckStage 里已被剥离，所以注册时
        写 "记忆" 即可，不要再加 "/记忆" 别名（永远匹配不到）。
        """
        if not self._ready():
            yield event.plain_result(f"记忆库还没就绪：{self._init_error or '未知错误'}")
            return

        full = self._text(event)
        # 唤醒前缀已被剥离，但用户仍可能把「/记忆」原样写出来；统一剥掉
        args = re.sub(r"^[/／]?记忆\s*", "", full).strip()
        parts = args.split(None, 1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        # 指令里 QQ 号后面可能直接跟昵称，保持原样交给下游解析

        if sub in ("查", "查看", "查询", "who"):
            if not rest:
                yield event.plain_result("用法：/记忆 查 <QQ号或昵称>")
                return
            qq = self._resolve_qq(rest)
            if not qq:
                yield event.plain_result(f"没找到「{rest}」这个人。")
                return
            yield event.plain_result(self.builder.profile_text(qq))
            return

        if sub in ("搜", "搜索", "找", "search"):
            kw = rest
            for noise in _QUERY_NOISE:
                kw = kw.replace(noise, " ") if noise != "记忆" else kw
            kw = kw.strip() or rest
            if not kw:
                yield event.plain_result("用法：/记忆 搜 <关键词>")
                return
            yield event.plain_result(search_text(self.store, kw, limit=10))
            return

        if sub in ("记住", "记", "add"):
            qq = _extract_qq(rest)
            fact = re.sub(r"^\d{5,12}\s*", "", rest).strip()
            if not qq or not fact:
                yield event.plain_result("用法：/记忆 记住 <QQ号> <要记住的内容>")
                return
            fid = self.store.add_fact(qq, fact, source=f"manual:{self._sender_id(event)}")
            if fid:
                yield event.plain_result(f"已记下 QQ {qq} 的事实 #{fid}：{fact}")
            else:
                yield event.plain_result("没记下来，检查一下内容是否为空。")
            return

        if sub in ("忘记", "删除", "forget"):
            if not self._is_admin(event):
                yield event.plain_result("删除记忆需要管理员权限。")
                return
            qq = _extract_qq(rest)
            if not qq:
                yield event.plain_result("用法：/记忆 忘记 <QQ号>")
                return
            n = self.store.delete_user(qq)
            yield event.plain_result(f"已删除 QQ {qq} 的 {n} 条发言和全部事实。")
            return

        if sub in ("好感", "好感度", "affinity"):
            # 好感度可见性由配置项 affinity_viewer_qq 控制
            viewer = str(self._cfg("affinity_viewer_qq", "") or "").strip()
            # 留空 = 所有人可查；填了 QQ 号 = 只有该号可查
            if viewer and self._sender_id(event) != viewer:
                yield event.plain_result("这个指令不对你开放。")
                return
            # 支持设置：/记忆 好感 123456789 20   ← 锁定为 20，不被自动评估覆盖
            parts = rest.split()
            if len(parts) >= 2:
                target = _extract_qq(parts[0]) or parts[0]
                try:
                    val = float(parts[1])
                except ValueError:
                    yield event.plain_result("用法：/记忆 好感 <QQ号> [0-100]")
                    return
                sc = self.store.set_affinity(target, val, "主人手动设定", locked=True)
                yield event.plain_result(
                    f"已把 QQ {target} 的好感度锁定为 {sc:.0f}（自动评估不再改动）"
                )
                return
            target = _extract_qq(rest) if rest else ""
            if target:
                yield event.plain_result(self._affinity_detail_text(target))
            else:
                yield event.plain_result(self._affinity_rank_text())
            return

        if sub in ("解锁好感", "unlock"):
            viewer = str(self._cfg("affinity_viewer_qq", "") or "").strip()
            # 留空 = 所有人可查；填了 QQ 号 = 只有该号可查
            if viewer and self._sender_id(event) != viewer:
                yield event.plain_result("这个指令不对你开放。")
                return
            target = _extract_qq(rest) if rest else ""
            if not target:
                yield event.plain_result("用法：/记忆 解锁好感 <QQ号>")
                return
            ok = self.store.unlock_affinity(target)
            yield event.plain_result(
                f"已解除 QQ {target} 的好感锁定" if ok else f"QQ {target} 没有锁定记录"
            )
            return

        if sub in ("画像时间", "时间", "profile_time"):
            if not self._is_admin(event):
                yield event.plain_result("修改画像时间需要管理员权限。")
                return
            arg = (rest or "").strip()
            cur_h, cur_m = self._profile_time()
            if not arg:
                yield event.plain_result(
                    f"当前画像时间：每天 {_fmt_hhmm(cur_h, cur_m)}\n"
                    f"要修改请发：/记忆 画像时间 HH:MM（例如 /记忆 画像时间 02:30）"
                )
                return
            hh, mm = _parse_hhmm(arg)
            if hh is None:
                yield event.plain_result(
                    f"时间格式不对：{arg!r}\n请用 24 小时制 HH:MM，例如 00:00 / 02:30 / 23:45"
                )
                return
            # 写回运行时配置 + 持久化
            try:
                self.config["profile_time"] = _fmt_hhmm(hh, mm)
                save_ok = True
                try:
                    self.config.save_config()  # AstrBotConfig 支持持久化
                except Exception:
                    save_ok = False
                # 重排定时任务
                await self._restart_profile_task()
                tip = "" if save_ok else "\n⚠️ 配置未能写入磁盘，重启后会恢复旧值"
                yield event.plain_result(
                    f"✅ 画像时间已改为每天 {_fmt_hhmm(hh, mm)}，定时任务已重排{tip}"
                )
            except Exception as e:
                yield event.plain_result(f"修改失败：{e}")
            return

        if sub in ("更新画像", "画像", "profile"):
            if not self._is_admin(event):
                yield event.plain_result("更新画像需要管理员权限。")
                return
            if profiler_mod is None:
                yield event.plain_result("画像模块（profiler.py）不可用，无法生成。")
                return
            # 指定 QQ 就只跑一个人，否则批量
            target = ""
            if rest:
                target = self._resolve_qq(rest) or _extract_qq(rest) or ""
            yield event.plain_result(
                ("正在给 QQ " + target + " 生成画像") if target
                else "正在给活跃用户批量生成画像（deepseek-chat），稍等…"
            )
            try:
                if target:
                    cfg = profiler_mod.load_llm_config()
                    text, delta = await profiler_mod.build_profile_and_affinity(
                        self.store,
                        target,
                        api_key=cfg["api_key"],
                        api_base=cfg["api_base"],
                        model=cfg["model"],
                        recent_n=int(self._cfg("profile_recent_n", 40)),
                    )
                    profiler_mod.replace_profile_fact(self.store, target, text)
                    note = ""
                    if delta is not None and abs(delta) >= 0.5:
                        sc = self.store.adjust_affinity(target, delta, f"手动更新画像 {delta:+.0f}")
                        note = f"\n好感度 {delta:+.0f} → {sc:.0f}"
                    yield event.plain_result(f"【QQ {target} 画像已更新】\n{text}{note}")
                    return
                res = await self.run_profile_now()
                yield event.plain_result(
                    "【画像更新完成】\n"
                    f"成功：{res['ok']} 人\n"
                    f"失败：{res['failed']} 人\n"
                    f"处理：{res['users']} 人\n"
                    f"（结果写入 facts 表，source='profile'）"
                )
            except Exception as e:
                logger.error(f"[user_memory] 手动生成画像失败: {e}", exc_info=True)
                yield event.plain_result(f"画像生成失败：{e}")
            return

        if sub in ("统计", "状态", "stats"):
            st = self.store.stats()
            yield event.plain_result(
                "【记忆库统计】\n"
                f"人数：{st['users']}\n"
                f"发言：{st['messages']} 条\n"
                f"事实：{st['facts']} 条\n"
                f"群数：{st['groups']}\n"
                f"时间跨度：{fmt_time(st['first_at'])} ~ {fmt_time(st['last_at'])}"
            )
            return

        if sub in ("最近", "recent"):
            st = self.store.stats()
            names = self.store.group_names()
            yield event.plain_result(
                "【最近记录】\n"
                f"最新一条：{fmt_time(st['last_at'])}\n"
                f"共 {st['messages']} 条发言，来自 {st['users']} 个人、{st['groups']} 个群\n"
                f"已记录群：{('、'.join(group_label(g, names) for g in names) or '（无）')}"
            )
            return

        yield event.plain_result(
            "【跨群记忆】用法：\n"
            "/记忆 查 <QQ号或昵称> —— 看这个人的完整档案\n"
            "/记忆 搜 <关键词> —— 搜所有群里的发言\n"
            "/记忆 记住 <QQ号> <内容> —— 手动记一条事实\n"
            "/记忆 忘记 <QQ号> —— 删除某人的记忆（管理员）\n"
            "/记忆 更新画像 [QQ号] —— 立刻生成/刷新画像（管理员）\n"
            "/记忆 统计 —— 看库里有数据多少\n"
            "机器人会自动记住大家说过的话，换个群也认得。"
        )

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ------------------------------------------------------------ 好感度展示
    def _affinity_detail_text(self, qq: str) -> str:
        """单人好感度详情。"""
        qq = str(qq or "").strip()
        if not qq:
            return "用法：/记忆 好感 <QQ号>"
        try:
            aff = self.store.get_affinity(qq)
        except Exception as e:
            return f"读取好感度失败：{e}"
        user = self.store.get_user(qq) or {}
        tier, desc = affinity_tier(aff["score"])
        nicks = []
        try:
            nicks = [n for n, _c in self.store.get_nicknames(qq, 3) if n]
        except Exception:
            pass
        out = [
            f"【好感度】QQ {qq}",
            f"昵称：{(user.get('nickname') or (nicks[0] if nicks else '未知'))}",
            f"好感度：{aff['score']:.0f}/100（{tier}）",
            f"行为倾向：{desc}",
        ]
        if aff.get("reason"):
            out.append(f"最近调整：{aff['reason']}")
        if aff.get("updated_at"):
            out.append(f"更新时间：{fmt_time(aff['updated_at'])}")
        return "\n".join(out)

    def _affinity_rank_text(self) -> str:
        """好感度排行总览。"""
        try:
            rows = self.store.affinity_top(limit=15)
        except Exception as e:
            return f"读取好感度失败：{e}"
        if not rows:
            return (
                "还没有好感度记录。\n"
                "好感度由每天凌晨的画像任务自动评估，也可以 /记忆 更新画像 立刻触发。"
            )
        out = ["【好感度排行（前15）】"]
        for i, r in enumerate(rows, 1):
            nick = r.get("nickname") or "?"
            tier, _ = affinity_tier(r["score"])
            out.append(f"{i}. {nick}({r['qq']}) {r['score']:.0f} [{tier}]")
        # 顺便给出全局统计
        try:
            st = self.store.stats()
            out.append("")
            out.append(f"共 {st['users']} 人，初始好感 {AFFINITY_DEFAULT:.0f}")
        except Exception:
            pass
        return "\n".join(out)

    # ------------------------------------------------------------ 输出过滤
    @filter.on_decorating_result(priority=90)
    async def filter_forbidden_names(self, event: AstrMessageEvent):
        """把禁用称呼从回复里去掉（文字和语音都过这里）。

        场景：群友拿某人的真实姓名当外号起哄，当事人在群里不想被这么喊。
        仅靠人格提示不可靠——群友只要说「再说一句 XXX」，模型就会照着念。
        所以这里在输出层做硬拦截，名字列表由配置项 forbidden_names 提供。

        priority=90：要早于 rvc_tts_hook(priority=0) 执行，
        否则语音会先按未清洗的文本合成，文字清了语音还是脏的。
        """
        names = self._forbidden_names()
        if not names:
            return
        try:
            result = event.get_result()
        except Exception:
            return
        if not result or not getattr(result, "chain", None):
            return

        changed = False
        for comp in result.chain:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text:
                cleaned = strip_forbidden_names(text, names)
                if cleaned != text:
                    comp.text = cleaned.strip()
                    changed = True
        if changed:
            # 清完变空的消息直接置空，避免发一条空消息
            try:
                remains = [
                    c for c in result.chain
                    if getattr(c, "text", None) or not isinstance(getattr(c, "text", None), str)
                ]
                if not remains:
                    result.chain = []
            except Exception:
                pass
            logger.info(f"[user_memory] 已过滤禁用称呼，回复已清洗")

    # ------------------------------------------------------------ 生命周期
    async def terminate(self):
        # 先取消定时任务，避免关库之后协程还在写
        task = self._profile_task
        self._profile_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5.0)
            logger.info("[user_memory] 每日画像定时任务已停止")
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
