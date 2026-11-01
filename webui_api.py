# -*- coding: utf-8 -*-
"""跨群用户记忆 —— WebUI 后端 API

在 AstrBot WebUI 里提供只读浏览界面（配合 pages/app/ 前端）。

⚠️ 关键机制（2026-11-01 摸清）：
- 框架调用 handler 时**只传路径参数**（`view_handler(**path_values)`，
  见 `dashboard/api/plugins.py:260`），**不会传 query/body**。
- 所以参数必须自己从请求对象读：`from astrbot.api.web import request`，
  用 `request.query.get(...)` 取 query string、`await request.json()` 取 body。
- 另一条路是 Quart 兼容层（memory_diary 用的）：`from quart import request`，
  然后 `request.args.get()` / `await request.get_json()`。两条都能用，
  这里选 `astrbot.api.web` 的原生接口，少一层依赖。

只读设计：本模块**不碰**记忆注入逻辑，只暴露查询接口。
所有 handler 都包了异常兜底，单个接口挂掉不影响 QQ 聊天。
"""
from __future__ import annotations

import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 单独测试时没有 astrbot
    import logging

    logger = logging.getLogger(__name__)

try:
    from astrbot.api.web import request as web_request
except Exception:  # pragma: no cover
    web_request = None

# ⚠️ 路由前缀**不能**带插件名（2026-11-01 反复踩坑，最终实测确认）。
#
# 框架行为（dashboard/dist/assets/PluginViewPage-*.js 的 S() 函数）：
#   const S = endpoint => `/api/v1/plugins/extensions/${pluginName}/${encodedPath}`
#   —— pluginName 由框架自动填，插件端只管传 endpoint。
# 也就是最终 URL = /api/v1/plugins/extensions/<插件名>/<endpoint>。
#
# 而匹配端（dashboard/api/plugins.py:209-221）拿整个 plugin_path
# （即 "<插件名>/<endpoint>"）去与注册的 route 做 fullmatch。
# 所以注册时必须用 **带插件名** 的 route 才能匹配上：
#   注册 "/astrbot_plugin_qq_user_memory/qq_user_memory/stats"
#   请求 /api/v1/plugins/extensions/astrbot_plugin_qq_user_memory/qq_user_memory/stats
#   → plugin_path = "astrbot_plugin_qq_user_memory/qq_user_memory/stats"
#   → request_path = "/astrbot_plugin_qq_user_memory/qq_user_memory/stats" ✅ 匹配
#
# 但是！前端 apiGet 传的 endpoint 必须是**不带插件名**的
# （因为框架会自动加），于是：
#   endpoint = "/qq_user_memory/stats"
#   → 最终 URL = /api/v1/plugins/extensions/<插件名>/qq_user_memory/stats
#   → request_path 仍是 "/<插件名>/qq_user_memory/stats" ✅
#
# **结论：注册用带插件名的 route；前端 BASE 用不带插件名的路径。**
PLUGIN_DIR_NAME = "astrbot_plugin_qq_user_memory"
# 注册用（含插件名）
ROUTE_PREFIX = f"/{PLUGIN_DIR_NAME}/qq_user_memory"
# 前端用（不含插件名）
CLIENT_PREFIX = "/qq_user_memory"


def log_route_hint() -> None:
    logger.info(
        "[user_memory] WebUI 注册前缀=%s ；前端 BASE 应使用 %s",
        ROUTE_PREFIX,
        CLIENT_PREFIX,
    )


def _ts(v: Any) -> str:
    """时间戳 -> "2026-11-01 18:30"；无效返回空串。"""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _rel(v: Any) -> str:
    """相对时间：3分钟前 / 2小时前 / 5天前"""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    d = max(0, time.time() - t)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def _param(name: str, default: str = "") -> str:
    """从 query string 取参数（handler 拿不到 kwargs）。"""
    if web_request is None:
        return default
    try:
        v = web_request.query.get(name)
        return str(v) if v is not None else default
    except Exception:
        return default


def _int_param(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_param(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


async def _json_body() -> dict:
    """取 JSON body；失败返回空 dict。"""
    if web_request is None:
        return {}
    try:
        data = await web_request.json(default={})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class WebApiMixin:
    """把 WebUI 的 HTTP handler 挂到插件的 context 上。

    使用方式（在插件 __init__ 里）：
        self._register_webui_apis()
    """

    # ---------------------------------------------------------- 注册
    def _register_webui_apis(self) -> None:
        routes = [
            ("stats", self._api_stats, ["GET"], "记忆库总览统计"),
            ("users", self._api_users, ["GET"], "用户列表（可按昵称/QQ 筛选）"),
            ("profile", self._api_profile, ["GET"], "单个用户的完整档案"),
            ("affinity", self._api_affinity, ["GET"], "好感度排行榜"),
            ("search", self._api_search, ["POST"], "全文检索发言"),
            ("groups", self._api_groups, ["GET"], "群组列表"),
        ]
        for name, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"{ROUTE_PREFIX}/{name}", handler, methods, desc
                )
                logger.info(f"[user_memory] 已注册 WebUI API {ROUTE_PREFIX}/{name}")
            except Exception as e:
                logger.error(f"[user_memory] 注册 WebUI API {name} 失败: {e}")

    # ---------------------------------------------------------- 工具
    @staticmethod
    def _json_ok(data: Any) -> dict:
        return {"ok": True, "data": data}

    @staticmethod
    def _json_err(msg: str) -> dict:
        return {"ok": False, "error": msg}

    def _need_store(self):
        """确保 store 可用，返回 (store, 错误响应)。"""
        try:
            if self.store is None and not self._ready():
                return None, self._json_err(
                    f"记忆库未就绪：{self._init_error or '未知错误'}"
                )
        except Exception:
            pass
        if self.store is None:
            return None, self._json_err("记忆库未就绪")
        return self.store, None

    # ---------------------------------------------------------- 接口
    async def _api_stats(self):
        """总览：人数、发言数、事实数、群数、时间跨度。"""
        store, err = self._need_store()
        if err:
            return err
        try:
            st = store.stats()
            return self._json_ok(
                {
                    "users": st.get("users", 0),
                    "messages": st.get("messages", 0),
                    "facts": st.get("facts", 0),
                    "groups": st.get("groups", 0),
                    "first_at": _ts(st.get("first_at")),
                    "last_at": _ts(st.get("last_at")),
                    "last_rel": _rel(st.get("last_at")),
                }
            )
        except Exception as e:
            logger.error(f"[user_memory] stats 失败: {e}", exc_info=True)
            return self._json_err(str(e))

    async def _api_users(self):
        """用户列表。query: q 筛选 / limit / sort(msg_count|last_seen|affinity)"""
        store, err = self._need_store()
        if err:
            return err
        try:
            q = _param("q").strip().lower()
            limit = _int_param("limit", 300, 1, 1000)
            sort = _param("sort", "msg_count")

            with store._lock:  # noqa: SLF001 - 同包内部使用
                rows = store._conn.execute(  # noqa: SLF001
                    "SELECT u.qq, u.nickname, u.first_seen, u.last_seen, u.msg_count,"
                    "       a.score AS affinity"
                    " FROM users u"
                    " LEFT JOIN affinity a ON a.qq = u.qq"
                    " ORDER BY u.msg_count DESC"
                    " LIMIT ?",
                    (limit * 3,),
                ).fetchall()

            items = []
            for r in rows:
                d = dict(r)
                qq = str(d.get("qq") or "")
                nick = str(d.get("nickname") or "")
                if q and (q not in qq.lower() and q not in nick.lower()):
                    continue
                items.append(
                    {
                        "qq": qq,
                        "nickname": nick,
                        "msg_count": int(d.get("msg_count") or 0),
                        "affinity": (
                            round(float(d["affinity"]), 1)
                            if d.get("affinity") is not None
                            else None
                        ),
                        "last_seen": _ts(d.get("last_seen")),
                        "last_rel": _rel(d.get("last_seen")),
                        "first_seen": _ts(d.get("first_seen")),
                    }
                )
                if len(items) >= limit:
                    break

            if sort == "last_seen":
                items.sort(key=lambda x: x["last_seen"], reverse=True)
            elif sort == "affinity":
                items.sort(
                    key=lambda x: (x["affinity"] is None, x["affinity"] or 0),
                    reverse=True,
                )
            return self._json_ok({"items": items, "total": len(items)})
        except Exception as e:
            logger.error(f"[user_memory] users 失败: {e}", exc_info=True)
            return self._json_err(str(e))

    async def _api_profile(self):
        """单人完整档案。query: qq"""
        store, err = self._need_store()
        if err:
            return err
        try:
            qq = _param("qq").strip()
            if not qq:
                return self._json_err("缺少 qq 参数")

            user = store.get_user(qq)
            if not user:
                return self._json_err(f"没记住 QQ {qq}")

            try:
                aff = store.get_affinity(qq) or {}
            except Exception:
                aff = {}

            facts_raw = store.get_facts(qq, 30) or []
            profile_facts = [
                {"content": f.get("content"), "source": f.get("source")}
                for f in facts_raw
                if str(f.get("source") or "") == "profile"
            ]
            manual_facts = [
                {"content": f.get("content"), "source": f.get("source")}
                for f in facts_raw
                if str(f.get("source") or "") != "profile"
            ]

            groups = []
            for g in store.get_groups(qq) or []:
                gid = str(g.get("group_id") or "")
                groups.append(
                    {
                        "group_id": gid,
                        "group_name": str(g.get("group_name") or ""),
                        "label": "私聊" if not gid else (g.get("group_name") or gid),
                        "msg_count": int(g.get("msg_count") or 0),
                        "last_seen": _ts(g.get("last_seen")),
                    }
                )
            groups.sort(key=lambda x: x["msg_count"], reverse=True)

            nicks = [
                {"name": n, "count": int(c)}
                for n, c in (store.get_nicknames(qq, 10) or [])
                if n
            ]

            recent = []
            for m in store.recent_messages(qq, 8) or []:
                gid = str(m.get("group_id") or "")
                recent.append(
                    {
                        "content": str(m.get("content") or "")[:300],
                        "group_id": gid,
                        "group_name": str(m.get("group_name") or ""),
                        "label": "私聊" if not gid else (m.get("group_name") or gid),
                        "at": _ts(m.get("created_at")),
                        "rel": _rel(m.get("created_at")),
                    }
                )

            return self._json_ok(
                {
                    "qq": qq,
                    "nickname": str(user.get("nickname") or ""),
                    "msg_count": int(user.get("msg_count") or 0),
                    "first_seen": _ts(user.get("first_seen")),
                    "last_seen": _ts(user.get("last_seen")),
                    "last_rel": _rel(user.get("last_seen")),
                    "affinity": {
                        "score": aff.get("score"),
                        "reason": str(aff.get("reason") or ""),
                        "locked": int(aff.get("locked") or 0),
                        "updated_at": _ts(aff.get("updated_at")),
                    },
                    "groups": groups,
                    "nicknames": nicks,
                    "profile_facts": profile_facts,
                    "manual_facts": manual_facts,
                    "recent": recent,
                }
            )
        except Exception as e:
            logger.error(f"[user_memory] profile 失败: {e}", exc_info=True)
            return self._json_err(str(e))

    async def _api_affinity(self):
        """好感度排行榜（现有 affinity_top 从没在群里暴露过）。"""
        store, err = self._need_store()
        if err:
            return err
        try:
            limit = _int_param("limit", 30, 1, 200)
            rows = store.affinity_top(limit) or []
            items = []
            for r in rows:
                qq = str(r.get("qq") or "")
                u = store.get_user(qq) or {}
                items.append(
                    {
                        "qq": qq,
                        "nickname": str(u.get("nickname") or r.get("nickname") or ""),
                        "score": round(float(r.get("score") or 0), 1),
                        "reason": str(r.get("reason") or ""),
                        "locked": int(r.get("locked") or 0),
                        "updated_at": _ts(r.get("updated_at")),
                        "msg_count": int(u.get("msg_count") or 0),
                    }
                )
            return self._json_ok({"items": items, "total": len(items)})
        except Exception as e:
            logger.error(f"[user_memory] affinity 失败: {e}", exc_info=True)
            return self._json_err(str(e))

    async def _api_search(self):
        """全文检索（FTS5）。body: {q, qq?, limit?}"""
        store, err = self._need_store()
        if err:
            return err
        try:
            body = await _json_body()
            kw = str(body.get("q") or body.get("keyword") or "").strip()
            if not kw:
                return self._json_err("缺少检索词")
            try:
                limit = int(body.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 100))
            qq = str(body.get("qq") or "").strip() or None
            rows = store.search(kw, limit=limit, qq=qq) or []
            names = store.group_names() or {}
            items = []
            for m in rows:
                gid = str(m.get("group_id") or "")
                gname = str(m.get("group_name") or names.get(gid) or "")
                items.append(
                    {
                        "qq": str(m.get("qq") or ""),
                        "nickname": str(m.get("nickname") or ""),
                        "content": str(m.get("content") or "")[:400],
                        "group_id": gid,
                        "group_name": gname,
                        "label": "私聊" if not gid else (gname or gid),
                        "at": _ts(m.get("created_at")),
                        "rel": _rel(m.get("created_at")),
                    }
                )
            return self._json_ok({"items": items, "total": len(items), "query": kw})
        except Exception as e:
            logger.error(f"[user_memory] search 失败: {e}", exc_info=True)
            return self._json_err(str(e))

    async def _api_groups(self):
        """群组列表（按发言数排序）。"""
        store, err = self._need_store()
        if err:
            return err
        try:
            with store._lock:  # noqa: SLF001
                rows = store._conn.execute(  # noqa: SLF001
                    "SELECT group_id, group_name, COUNT(*) AS n,"
                    "       COUNT(DISTINCT qq) AS people, MAX(created_at) AS last_at"
                    " FROM messages GROUP BY group_id ORDER BY n DESC"
                ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                gid = str(d.get("group_id") or "")
                items.append(
                    {
                        "group_id": gid,
                        "group_name": str(d.get("group_name") or ""),
                        "label": "私聊" if not gid else (d.get("group_name") or gid),
                        "messages": int(d.get("n") or 0),
                        "people": int(d.get("people") or 0),
                        "last_at": _ts(d.get("last_at")),
                        "last_rel": _rel(d.get("last_at")),
                    }
                )
            return self._json_ok({"items": items, "total": len(items)})
        except Exception as e:
            logger.error(f"[user_memory] groups 失败: {e}", exc_info=True)
            return self._json_err(str(e))
