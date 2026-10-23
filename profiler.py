# -*- coding: utf-8 -*-
"""每日画像生成模块（方向 B）

把每个人的发言归纳成 2-3 句「这个人是什么样的人 / 说话什么调性 / 怎么跟他相处」，
写进 facts 表（source='profile'），注入时作为【画像】展示。

硬性约束（不要改成别的模型）：
- 固定走 DeepSeek 官方接口：api_base=https://api.deepseek.com，model=deepseek-chat
- key 从 data/cmd_config.json 的 provider_sources 里 id=='deepseek' 的 key 读取
  （该字段可能是 list，取 [0]），不硬编码
- 只用标准库 urllib，不引入新依赖

设计要点：
- build_profile_for_user 只负责「取最近发言 → 拼提示词 → 调 LLM → 清洗输出」
- run_daily_profiling 只处理发言数够的活跃用户，单个人失败不影响整体
- 重复生成时先删旧的 source='profile' 记录再插入（手动 manual 事实保留）
- memory_store.py 没有「按 source 删事实 / 列出活跃用户」的公开接口，这里用
  store._conn + store._lock 直连（RLock，可重入），不改 storage 层

手动测试：

    E:\\qq-bot\\astrbot-venv\\Scripts\\python.exe profiler.py --db <path>
    E:\\qq-bot\\astrbot-venv\\Scripts\\python.exe profiler.py --db <path> --qq 123456789
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 单独跑测试脚本 / 命令行时没有 astrbot
    import logging

    logger = logging.getLogger("user_memory.profiler")

# ---------------------------------------------------------------- 常量

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

#: 该插件目录是 data/plugins/astrbot_plugin_qq_user_memory，
#: cmd_config.json 在 data/ 下，即上两级。
DEFAULT_CMD_CONFIG = os.path.abspath(
    os.path.join(PLUGIN_DIR, "..", "..", "cmd_config.json")
)

#: 画像固定用 deepseek-chat，**不允许**用 mimo / 其它模型兜底
PROFILE_MODEL = "deepseek-chat"
PROFILE_API_BASE = "https://api.deepseek.com"

PROFILE_SOURCE = "profile"
MAX_TOKENS = 900  # 需求要求 >= 600；留点余量给思考式输出
HTTP_TIMEOUT = 120.0

#: 画像正文长度上限（字符），防止模型啰嗦复述原文
PROFILE_MAX_CHARS = 320

_SYSTEM_PROMPT = (
    "你是一个群聊观察者。你只根据给定样本，概括某位群友「是个什么样的人」。"
    "你从不复述他说过的具体内容，只总结性格、说话调性和相处方式。"
)


# ---------------------------------------------------------------- 配置读取

def load_llm_config(cmd_config_path: str | None = None) -> dict[str, Any]:
    """从 cmd_config.json 读出 DeepSeek 的 key / api_base / model。

    返回 {'api_key': str, 'api_base': str, 'model': 'deepseek-chat'}。
    读不到 key 时抛 RuntimeError（由调用方记日志），绝不静默降级到别的模型。
    """
    path = cmd_config_path or os.environ.get("UM_CMD_CONFIG") or DEFAULT_CMD_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"找不到 AstrBot 配置：{path}")
    except Exception as e:
        raise RuntimeError(f"读取 AstrBot 配置失败：{path}（{e}）")

    sources = cfg.get("provider_sources") or []
    if not isinstance(sources, list):
        raise RuntimeError("cmd_config.json 的 provider_sources 不是列表")

    entry: dict[str, Any] | None = None
    for item in sources:
        if isinstance(item, dict) and str(item.get("id") or "") == "deepseek":
            entry = item
            break
    if entry is None:
        raise RuntimeError("cmd_config.json 里没有 id='deepseek' 的 provider_source")

    raw_key = entry.get("key")
    if isinstance(raw_key, (list, tuple)):
        raw_key = raw_key[0] if raw_key else ""
    api_key = str(raw_key or "").strip()
    if not api_key:
        raise RuntimeError("deepseek provider 的 key 为空，无法生成画像")

    api_base = str(entry.get("api_base") or PROFILE_API_BASE).strip().rstrip("/")
    if not api_base.startswith("http"):
        api_base = PROFILE_API_BASE

    # 模型写死，防止配置里被换成 mimo
    return {"api_key": api_key, "api_base": api_base, "model": PROFILE_MODEL}


def _resolve_key(api_key: str | None) -> tuple[str, str, str]:
    """允许调用方显式传 key（测试/命令行），否则从 cmd_config 读。"""
    if api_key:
        return (
            str(api_key),
            PROFILE_API_BASE,
            PROFILE_MODEL,
        )
    cfg = load_llm_config()
    return cfg["api_key"], cfg["api_base"], cfg["model"]


# ---------------------------------------------------------------- HTTP

def _post_chat_sync(
    payload: dict[str, Any],
    api_key: str,
    api_base: str,
    timeout: float = HTTP_TIMEOUT,
) -> str:
    """同步 POST /chat/completions，返回 assistant 文本。失败抛 RuntimeError。"""
    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_err = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}"
            # 4xx（除 429）重试没意义
            if 400 <= int(e.code) < 500 and int(e.code) != 429:
                raise RuntimeError(f"DeepSeek 接口报错 {last_err}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < 3:
            time.sleep(1.5 * attempt)
    else:
        raise RuntimeError(f"DeepSeek 接口连续失败：{last_err}")

    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError(f"DeepSeek 返回不是 JSON：{raw[:200]}")

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"DeepSeek 返回结构异常：{str(data)[:200]}")
    content = (content or "").strip()
    if not content:
        raise RuntimeError("DeepSeek 返回空内容")
    return content


async def _post_chat(
    payload: dict[str, Any],
    api_key: str,
    api_base: str,
    timeout: float = HTTP_TIMEOUT,
) -> str:
    """在线程里跑阻塞 IO，别卡住 AstrBot 的事件循环。"""
    return await asyncio.to_thread(_post_chat_sync, payload, api_key, api_base, timeout)


# ---------------------------------------------------------------- 提示词

_PROFILE_INSTRUCTION = """下面是群友「{nickname}」（QQ {qq}）最近在群里的 {n} 条发言样本，来自 {groups}。

请输出两段内容，中间用单独一行 ==== 分隔，不要任何多余文字。

【第一段：画像】
要求：
1. 用 2-3 句中文概括这个人，总长不超过 120 字。
2. 必须覆盖三件事：这个人是什么样的人 / 说话什么调性 / 别人该怎么跟他相处。
3. 禁止复述发言里的具体内容（不要提他说过的事、地点、物品、原话），只写性格与相处方式。
4. 不要用「他经常说」「根据发言」这类叙述腔，直接下判断、给结论。
5. 不要标题、不要编号、不要 Markdown、不要引号。

画像示例风格：
说话冲、爱抬杠、喜欢调侃机器人身上的梗，属熟面孔，玩笑可以开着接，认真话题上别跟他硬碰。

【第二段：好感度调整】
假设你是群里的机器人"大肥鱼"，判断和这个人相处下来，你对他的好感会怎么变。
只输出一个整数（正数=好感增加，负数=好感减少，0=不变），范围 -20 ~ +20。
判断依据：
- 他经常夸你、谢你、找你玩 → 加分
- 他经常骂你、羞辱你、无视你 → 减分
- 他聊天让人舒服、有趣 → 加分
- 他只顾自说自话、从不理你 → 轻微减分
只输出数字本身。

输出格式（严格遵守，两段之间是单独一行 ====）：
第一段画像文字
====
好感度调整数字

发言样本：
{samples}"""


def _build_messages(
    store: Any,
    qq: str,
    recent_n: int = 40,
) -> tuple[list[dict[str, str]], int]:
    """把最近发言拼成 chat messages。返回 (messages, 样本条数)。"""
    qq = str(qq or "").strip()
    messages = store.recent_messages(qq, int(recent_n)) or []
    # recent_messages 是倒序（最新在前），拼提示词时按时间正序更自然
    messages = [m for m in messages if str(m.get("content") or "").strip()]
    messages.reverse()

    user = store.get_user(qq) or {}
    nickname = str(user.get("nickname") or "").strip()
    if not nickname:
        nicks = store.get_nicknames(qq, 1) if hasattr(store, "get_nicknames") else []
        if nicks:
            nickname = str(nicks[0][0] or "").strip()
    nickname = nickname or "（未知昵称）"

    group_ids: list[str] = []
    with contextlib.suppress(Exception):
        for g in store.get_groups(qq) or []:
            gid = str(g.get("group_id") or "")
            if gid and gid not in group_ids:
                group_ids.append(gid)
    groups = f"{len(group_ids)} 个群" if group_ids else "群聊"

    samples = "\n".join(
        f"- {str(m.get('content') or '').strip()[:160]}" for m in messages
    )
    prompt = _PROFILE_INSTRUCTION.format(
        nickname=nickname,
        qq=qq,
        n=len(messages),
        groups=groups,
        samples=samples,
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ], len(messages)


def clean_profile_text(text: str) -> str:
    """清洗模型输出：去掉标题/编号/引号/多余换行，压成一段中文。"""
    t = (text or "").strip()
    if not t:
        return ""
    # 去掉 ``` 代码块围栏
    t = re.sub(r"```[a-zA-Z]*", "", t).replace("```", "")
    lines: list[str] = []
    for raw in t.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去掉「画像：」「【画像】」「总结：」「1.」「- 」这类前缀
        line = re.sub(r"^[【\[]?\s*(画像|人物画像|总结|概括|结论)\s*[】\]]?\s*[:：]\s*", "", line)
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        line = re.sub(r"^\d+\s*[.、)）:：]\s*", "", line)
        line = line.strip().strip("\"“”'‘’").strip()
        if line:
            lines.append(line)
    out = "".join(lines) if len(lines) > 1 else (lines[0] if lines else "")
    out = re.sub(r"\s+", " ", out).strip()
    out = out.strip("\"“”'‘’").strip()
    if len(out) > PROFILE_MAX_CHARS:
        out = out[:PROFILE_MAX_CHARS].rstrip("，,；;、 ") + "…"
    return out


# ---------------------------------------------------------------- 对外接口

def parse_profile_and_affinity(raw: str) -> tuple[str, float | None]:
    """把「画像 ==== 好感度」的原始输出拆开。

    返回 (画像文本, 好感度调整值或 None)。解析失败时好感度为 None，不影响画像。
    """
    text = (raw or "").strip()
    if not text:
        return "", None

    # 优先按 ==== 分隔
    parts = re.split(r"^\s*={3,}\s*$", text, maxsplit=1, flags=re.MULTILINE)
    profile_raw = parts[0]
    delta_raw = parts[1] if len(parts) > 1 else ""

    delta: float | None = None
    if delta_raw.strip():
        # 从可能是"好感度调整：+8"的描述里抽第一个整数
        m = re.search(r"[-+]?\d+(?:\.\d+)?", delta_raw)
        if m:
            try:
                delta = max(-20.0, min(20.0, float(m.group(0))))
            except ValueError:
                delta = None
    else:
        # 没写分隔符时，尝试从末尾找独立的数字行
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", lines[-1]):
            try:
                delta = max(-20.0, min(20.0, float(lines[-1])))
                profile_raw = "\n".join(lines[:-1])
            except ValueError:
                delta = None

    profile = clean_profile_text(profile_raw)
    return profile, delta


async def build_profile_for_user(
    store: Any,
    qq: str,
    api_key: str,
    api_base: str = PROFILE_API_BASE,
    model: str = PROFILE_MODEL,
    recent_n: int = 40,
) -> str:
    """只生成画像文本（兼容旧调用）。"""
    profile, _delta = await build_profile_and_affinity(
        store, qq, api_key, api_base, model, recent_n
    )
    return profile


async def build_profile_and_affinity(
    store: Any,
    qq: str,
    api_key: str,
    api_base: str = PROFILE_API_BASE,
    model: str = PROFILE_MODEL,
    recent_n: int = 40,
) -> tuple[str, float | None]:
    """一次 LLM 调用同时生成画像和好感度调整值。

    返回 (画像文本, 好感度调整值)。好感度为 None 表示这次没解析出来。
    失败（网络/接口/空输出）时抛 RuntimeError，由 run_daily_profiling 兜住。
    """
    qq = str(qq or "").strip()
    if not qq:
        raise RuntimeError("QQ 号为空")
    if not api_key:
        raise RuntimeError("api_key 为空")

    chat_messages, n = _build_messages(store, qq, recent_n=recent_n)
    if n == 0:
        raise RuntimeError(f"QQ {qq} 没有可用的发言样本")

    # 模型固定 deepseek-chat（即使调用方传了别的也不采纳）
    payload = {
        "model": PROFILE_MODEL,
        "messages": chat_messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
        "stream": False,
    }
    raw = await _post_chat(payload, api_key, api_base or PROFILE_API_BASE)
    profile, delta = parse_profile_and_affinity(raw)
    if not profile:
        raise RuntimeError(f"QQ {qq} 画像清洗后为空（原始：{raw[:80]}）")
    return profile, delta


# ---------------------------------------------------------------- 存储读写
# memory_store.py 没有对应公开接口（那边由另一个任务负责），这里直连 _conn。

def _store_conn(store: Any) -> Any:
    return getattr(store, "_conn", None)


def _store_lock(store: Any):
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def list_active_users(store: Any, min_messages: int = 10, max_users: int = 50) -> list[str]:
    """按发言数倒序列出活跃用户 QQ（>= min_messages）。"""
    conn = _store_conn(store)
    min_messages = max(1, int(min_messages or 1))
    max_users = max(1, int(max_users or 1))
    if conn is None:  # 理论上不会发生，兜底返回空
        return []
    sql = (
        "SELECT qq, COUNT(*) AS n FROM messages GROUP BY qq"
        " HAVING COUNT(*) >= ? ORDER BY n DESC LIMIT ?"
    )
    with _store_lock(store):
        try:
            rows = conn.execute(sql, (min_messages, max_users)).fetchall()
        except Exception as e:
            logger.error(f"[user_memory] 查询活跃用户失败: {e}")
            return []
    out: list[str] = []
    for r in rows:
        try:
            out.append(str(r["qq"]))
        except Exception:
            try:
                out.append(str(r[0]))
            except Exception:
                continue
    return out


def replace_profile_fact(store: Any, qq: str, content: str) -> None:
    """先删该 QQ 旧的 source='profile' 事实，再插入新的。

    只删 source 精确等于 'profile' 的行，手动事实（manual / manual:<qq>）保留。
    """
    qq = str(qq or "").strip()
    content = (content or "").strip()
    if not qq or not content:
        return
    conn = _store_conn(store)
    if conn is None:
        # 兜底走公开接口（会留旧行，但总比丢掉画像好）
        store.add_fact(qq, content, source=PROFILE_SOURCE)
        return
    with _store_lock(store):
        conn.execute("DELETE FROM facts WHERE qq=? AND source=?", (qq, PROFILE_SOURCE))
        conn.execute(
            "INSERT INTO facts(qq, content, source, created_at) VALUES(?,?,?,?)",
            (qq, content, PROFILE_SOURCE, time.time()),
        )
        conn.commit()


def get_profile_facts(store: Any, qq: str) -> list[dict[str, Any]]:
    """读某个 QQ 的 source='profile' 事实（测试 / 排查用）。"""
    conn = _store_conn(store)
    if conn is None:
        return [f for f in (store.get_facts(qq, 50) or []) if f.get("source") == PROFILE_SOURCE]
    with _store_lock(store):
        rows = conn.execute(
            "SELECT * FROM facts WHERE qq=? AND source=? ORDER BY id DESC",
            (str(qq), PROFILE_SOURCE),
        ).fetchall()
    return [dict(r) for r in rows]


async def profile_and_store(
    store: Any,
    qq: str,
    recent_n: int = 40,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
) -> str:
    """生成并写入单个 QQ 的画像，返回画像文本。失败抛异常。"""
    key, base, _model = _resolve_key(api_key)
    text = await build_profile_for_user(
        store,
        qq,
        api_key=key,
        api_base=api_base or base,
        model=model or PROFILE_MODEL,
        recent_n=recent_n,
    )
    replace_profile_fact(store, qq, text)
    return text


async def run_daily_profiling(
    store: Any,
    min_messages: int = 10,
    max_users: int = 50,
    recent_n: int = 40,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    cmd_config_path: str | None = None,
) -> dict[str, int]:
    """每日批量画像。返回 {'ok': n, 'failed': m, 'users': k}。

    - 只处理发言数 >= min_messages 的活跃用户，最多 max_users 人
    - 单个用户失败只记日志，不影响其他人
    - 成功者先删旧的 source='profile' 再插新行（手动事实保留）
    """
    started = time.time()
    if api_key:
        key, base = str(api_key), (api_base or PROFILE_API_BASE)
    else:
        cfg = load_llm_config(cmd_config_path)
        key, base = cfg["api_key"], cfg["api_base"]

    users = list_active_users(store, min_messages=min_messages, max_users=max_users)
    ok = failed = 0
    affinity_updates = 0
    for idx, qq in enumerate(users, 1):
        try:
            text, delta = await build_profile_and_affinity(
                store,
                qq,
                api_key=key,
                api_base=base,
                model=PROFILE_MODEL,
                recent_n=recent_n,
            )
            replace_profile_fact(store, qq, text)

            # 好感度：在现有值上增减（裁剪到 0-100）
            affinity_note = ""
            if delta is not None and abs(delta) >= 0.5:
                try:
                    new_score = store.adjust_affinity(
                        qq, delta, f"每日画像评估 {delta:+.0f}"
                    )
                    affinity_updates += 1
                    affinity_note = f" 好感 {delta:+.0f}→{new_score:.0f}"
                except Exception as e:
                    logger.warning(f"[user_memory] 好感度写入失败 {qq}: {e}")

            ok += 1
            logger.info(
                f"[user_memory] 画像已更新 {qq}（{idx}/{len(users)}）："
                f"{text[:50]}{affinity_note}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failed += 1
            logger.error(f"[user_memory] 画像生成失败 {qq}（{idx}/{len(users)}）: {e}")

    elapsed = round(time.time() - started, 1)
    logger.info(
        f"[user_memory] 每日画像完成：成功 {ok} / 失败 {failed} / 共 {len(users)} 人，"
        f"好感度更新 {affinity_updates} 人，耗时 {elapsed}s"
    )
    return {"ok": ok, "failed": failed, "users": len(users), "affinity": affinity_updates}


# ---------------------------------------------------------------- 命令行

async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨群用户记忆 —— 手动生成画像")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--qq", default="", help="只给这个 QQ 生成（不填=批量）")
    parser.add_argument("--min-messages", type=int, default=10, help="最少发言数（默认 10）")
    parser.add_argument("--max-users", type=int, default=50, help="最多处理几人（默认 50）")
    parser.add_argument("--recent-n", type=int, default=40, help="每人取样几条发言（默认 40）")
    args = parser.parse_args(argv)

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from memory_store import MemoryStore  # noqa: E402

    store = MemoryStore(args.db)
    try:
        cfg = load_llm_config()
        print(f"模型：{cfg['model']}　接口：{cfg['api_base']}　key：{cfg['api_key'][:6]}…")
        if args.qq:
            text = await profile_and_store(store, args.qq, recent_n=args.recent_n)
            print(f"\n【QQ {args.qq} 画像】\n{text}")
            return 0
        res = await run_daily_profiling(
            store,
            min_messages=args.min_messages,
            max_users=args.max_users,
            recent_n=args.recent_n,
        )
        print(f"\n结果：成功 {res['ok']} / 失败 {res['failed']} / 共 {res['users']} 人")
        return 0 if res["failed"] == 0 else 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
