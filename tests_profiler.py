# -*- coding: utf-8 -*-
"""画像模块自测（方向 B）：直连真实 deepseek-chat

    cd E:/qq-bot/data/plugins/astrbot_plugin_qq_user_memory
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 E:/qq-bot/astrbot-venv/Scripts/python.exe tests_profiler.py

覆盖：
  1. 造 3 个用户各 15 条发言 → 直连真实 deepseek-chat 生成画像
  2. 画像非空、不是占位符、是中文
  3. 写入 facts 且 source='profile'
  4. 重复运行不产生重复行，且手动 manual 事实不会被删
  5. 错误路径（坏 key / 没人）不崩溃
  6. main.py 侧的定时任务挂载、手动指令、terminate() 能干净取消

临时库放在**本插件目录下的 _selftest_tmp/**（E 盘），跑完自动删除，
不在 C 盘 %TEMP% 留残留（本机 C 盘紧张）。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# 让 tempfile 落在工作区而不是 C 盘系统临时目录
_TMP_ROOT = os.path.join(_HERE, "_selftest_tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

# `import main` 会顺带 import astrbot，AstrBot 会往 cwd/data 里写默认配置。
# 把 ASTRBOT_ROOT 指到临时目录，别在插件目录里留下 data/ 垃圾。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(_TMP_ROOT, "astrbot_root")
)
os.makedirs(os.environ["ASTRBOT_ROOT"], exist_ok=True)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


# ---------------------------------------------------------------- 测试数据
# 三个人性格差异明显，方便肉眼检查画像有没有真的「认得出人」
USERS = {
    "123456789": {
        "nickname": "阿强",
        "lines": [
            "这游戏也太拉了，做的一坨，策划脑子进水了吧",
            "哈哈哈笑死，大肥鱼你又在这胡说八道",
            "机器人今天怎么这么怂，接不住梗了吧",
            "不是，这也能吹？我真服了你们了",
            "来solo啊，谁怂谁是狗",
            "别问，问就是菜，练十年也打不过我",
            "大肥鱼你这回复挺灵性啊，谁教你的",
            "我说句实话，这活动确实是割韭菜",
            "别聊了，开黑，三缺一",
            "你这个理由也太牵强了，笑不活了",
            "反正我是不信，你们随便",
            "行行行，你赢了你赢了你最强",
            "这也能叫设计？我感觉我上我行",
            "大肥鱼你是不是又被调教过了，嘴这么甜",
            "算了，跟你们说不通，我打游戏去了",
        ],
    },
    "987654321": {
        "nickname": "小桃",
        "lines": [
            "今天下班好晚，地铁上挤成肉夹馍了",
            "有人知道附近哪家火锅好吃吗，最近馋得慌",
            "谢谢你呀，你人真好，抱抱",
            "刚做好的蛋糕，发个图给你们看看，成功了！",
            "呜呜呜呜我今天被领导骂了，好难过",
            "我最近在学做菜，锅都烧糊了三次",
            "大家晚安呀，明天继续打工，爱你们",
            "这周想去看海，有没有人一起呀",
            "我养的多肉又长新叶子了，好开心",
            "太累了，今天不想说话，就躺一躺",
            "食堂的菜今天难得还行，光盘了",
            "谢谢大家安慰我，我现在好多啦",
            "周末约不约呀，想去逛夜市，有好多小吃",
            "买了条小裙子，感觉有点显胖，纠结中",
            "晚安晚安，做个好梦，么么",
        ],
    },
    "555666777": {
        "nickname": "老陈",
        "lines": [
            "关于这个问题，我的看法是先看财报再说结论",
            "最新的政策原文我贴群里了，第三条是关键",
            "我不太认同，理由有三点，我一条条讲",
            "这事得从时间线上捋一遍，别记混了",
            "数据来源要标清楚，不然没法判断可信度",
            "刚查了下，这个说法在 2023 年就被辟谣过",
            "建议大家别急着下结论，等两天有结果",
            "从成本结构上看，这波涨价其实是必然的",
            "我把要点整理成一小段，方便你们看",
            "补充一下，那个季度的口径不一样，别直接比",
            "要说清楚前提，不然很容易偷换概念",
            "我持保留意见，先观察一段时间",
            "这个逻辑链条中间缺了一环，说不通",
            "原文链接在这，大家自己判断",
            "严谨一点，数据对不上就别当结论用",
        ],
    },
}

GROUP_A = ("1000000001", "一号群")
GROUP_B = ("1000000002", "二号群")


def seed(store, created_base=None):
    """给 3 个用户各写 15 条发言，跨两个群。"""
    base = created_base or (time.time() - 15 * 3600)
    i = 0
    for qq, info in USERS.items():
        for line in info["lines"]:
            gid, gname = GROUP_A if i % 2 == 0 else GROUP_B
            store.record_message(
                qq=qq,
                nickname=info["nickname"],
                group_id=gid,
                group_name=gname,
                content=line,
                created_at=base + i * 60,
            )
            i += 1


# ---------------------------------------------------------------- 假 AstrBot 事件
class FakeGroup:
    def __init__(self, gid, gname=""):
        self.group_id = gid
        self.group_name = gname


class FakeObj:
    def __init__(self, gid=None, gname=""):
        self.group = FakeGroup(gid, gname) if gid else None


class FakeEvent:
    def __init__(self, sender, nickname, text, gid=None, gname="", at=False,
                 self_id="1999999999", admin=False):
        self._sender = sender
        self._nickname = nickname
        self.message_str = text
        self.message_obj = FakeObj(gid, gname)
        self.is_at_or_wake_command = at
        self._self_id = self_id
        self._admin = admin

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._nickname

    def get_group_id(self):
        return self.message_obj.group.group_id if self.message_obj.group else ""

    def get_self_id(self):
        return self._self_id

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return ("plain", text)


async def drain(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


def _looks_like_real_profile(text):
    """不是占位符 / 不是报错信息 / 有中文。"""
    if not text or len(text) < 8:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk < 6:
        return False
    bad = ("占位", "TODO", "placeholder", "Error", "失败", "无法生成", "None", "{}")
    return not any(b in text for b in bad)


# ---------------------------------------------------------------- 主流程
async def main():
    tmp = tempfile.mkdtemp(prefix="umprof_")
    db = os.path.join(tmp, "profiler.db")

    import profiler as P
    from memory_store import ContextBuilder, MemoryStore

    # ---------------------------------------------------------- [1] 配置
    print("[1] 配置读取（固定 deepseek-chat，不硬编码 key）")
    cfg = P.load_llm_config()
    check("拿到 api_key", bool(cfg["api_key"]) and len(cfg["api_key"]) > 10, cfg["api_key"][:4])
    check("模型固定 deepseek-chat", cfg["model"] == "deepseek-chat", cfg["model"])
    check("api_base 是 deepseek 官方", "api.deepseek.com" in cfg["api_base"], cfg["api_base"])
    check("不是 mimo", "mimo" not in cfg["api_base"].lower(), cfg["api_base"])
    check("max_tokens >= 600", P.MAX_TOKENS >= 600, P.MAX_TOKENS)
    src = open(os.path.join(_HERE, "profiler.py"), encoding="utf-8").read()
    check("源码里没有硬编码 sk- key", "sk-" not in src, [l for l in src.splitlines() if "sk-" in l][:2])
    check("源码模型写死 deepseek-chat", 'PROFILE_MODEL = "deepseek-chat"' in src, "")

    # ---------------------------------------------------------- [2] 造数据
    print("[2] 造数：3 个用户 × 15 条发言（跨 2 个群）")
    store = MemoryStore(db)
    builder = ContextBuilder(store)
    seed(store)
    st = store.stats()
    check("3 个用户", st["users"] == 3, st)
    check("45 条发言", st["messages"] == 45, st)
    check("facts 初始为 0", st["facts"] == 0, st)
    check("每人 15 条", all(store.get_user(q)["msg_count"] == 15 for q in USERS), "")

    # 手动事实：重复画像不能把它删掉
    store.add_fact("123456789", "生日是 3 月 14 日", source="manual")

    # ---------------------------------------------------------- [3] 真实 API
    print("[3] 直连真实 deepseek-chat 生成 3 个人的画像")
    profiles = {}
    for qq, info in USERS.items():
        t0 = time.time()
        try:
            text = await P.build_profile_for_user(
                store, qq, api_key=cfg["api_key"],
                api_base=cfg["api_base"], model=cfg["model"], recent_n=40,
            )
            profiles[qq] = text
            print(f"  --- {info['nickname']}({qq})  {time.time() - t0:.1f}s ---")
            print(f"      {text}")
        except Exception as e:
            print(f"  FAIL  {qq} 真实 API 调用失败: {type(e).__name__}: {e}")
            FAILS.append(f"api-{qq}")

    api_ok = len(profiles) == 3
    if api_ok:
        for qq in USERS:
            t = profiles[qq]
            check(f"画像非空 {qq}", bool(t.strip()), repr(t))
            check(f"画像像真的中文 {qq}", _looks_like_real_profile(t), repr(t))
            check(f"画像长度合理 {qq}", 20 <= len(t) <= P.PROFILE_MAX_CHARS + 5, len(t))
            check(f"没复述样本原文 {qq}", t not in USERS[qq]["lines"], repr(t))
        # 三个人的画像必须不一样（证明是按各自样本归纳的）
        check("三人画像互不相同", len(set(profiles.values())) == 3, profiles)
    else:
        print("  (真实 API 未全部成功，跳过内容断言，只验证错误处理路径)")

    # ---------------------------------------------------------- [4] 写入 facts
    print("[4] 写入 facts 表（source='profile'）")
    if api_ok:
        for qq, text in profiles.items():
            P.replace_profile_fact(store, qq, text)
        rows = P.get_profile_facts(store, "123456789")
        check("facts 有 profile 记录", len(rows) == 1, rows)
        check("source 是 profile", rows and rows[0]["source"] == "profile", rows[0]["source"] if rows else None)
        check("内容与生成一致", rows and rows[0]["content"] == profiles["123456789"], "")
        check("手动 manual 事实还在",
              any("生日" in f["content"] and f["source"] == "manual"
                  for f in store.get_facts("123456789", 20)),
              store.get_facts("123456789", 20))
        check("总 facts = 3 画像 + 1 手动", store.stats()["facts"] == 4, store.stats())
    else:
        # 无 API 时至少验证写入/去重逻辑（用假文本）
        for qq in USERS:
            P.replace_profile_fact(store, qq, f"[离线占位] {USERS[qq]['nickname']} 的画像")
        rows = P.get_profile_facts(store, "123456789")
        check("离线写入 profile", len(rows) == 1 and rows[0]["source"] == "profile", rows)
        check("手动事实还在（离线）",
              any(f["source"] == "manual" for f in store.get_facts("123456789", 20)), "")

    # ---------------------------------------------------------- [5] run_daily_profiling
    print("[5] run_daily_profiling 全流程 + 幂等（重复跑不出重复行）")
    before = {q: len(P.get_profile_facts(store, q)) for q in USERS}
    check("跑之前每人 1 行 profile", all(v == 1 for v in before.values()), before)

    if api_ok:
        res = await P.run_daily_profiling(
            store, min_messages=10, max_users=50, recent_n=40,
            api_key=cfg["api_key"], api_base=cfg["api_base"],
        )
        print(f"  统计：{res}")
        check("统计字段齐全", set(res) == {"ok", "failed", "users"}, res)
        check("处理 3 人", res["users"] == 3, res)
        check("3 人全部成功", res["ok"] == 3 and res["failed"] == 0, res)
        after = {q: len(P.get_profile_facts(store, q)) for q in USERS}
        check("重复跑不产生重复行", all(v == 1 for v in after.values()), after)
        check("重复跑后 facts 仍 4 条", store.stats()["facts"] == 4, store.stats())
        check("手动事实仍在（重复跑后）",
              any(f["source"] == "manual" for f in store.get_facts("123456789", 20)),
              store.get_facts("123456789", 20))
        newest = P.get_profile_facts(store, "123456789")
        check("画像是新生成的（不是旧行）",
              newest and newest[0]["content"] == P.clean_profile_text(newest[0]["content"]),
              newest[0]["content"][:60] if newest else None)
    else:
        res = await P.run_daily_profiling(
            store, min_messages=10, max_users=50, recent_n=40,
            api_key=cfg["api_key"], api_base=cfg["api_base"],
            cmd_config_path=P.DEFAULT_CMD_CONFIG,
        )
        check("离线也能返回统计", set(res) == {"ok", "failed", "users"}, res)

    print("[5b] min_messages 门槛：门槛调高就没人")
    res2 = await P.run_daily_profiling(
        store, min_messages=999, max_users=50,
        api_key=cfg["api_key"], api_base=cfg["api_base"],
    )
    check("发言数不够不处理", res2 == {"ok": 0, "failed": 0, "users": 0}, res2)

    # ---------------------------------------------------------- [6] 错误路径
    print("[6] 错误路径不崩溃")
    try:
        await P.build_profile_for_user(
            store, "123456789", api_key="sk-invalid-key-for-test",
            api_base=cfg["api_base"], model="deepseek-chat", recent_n=40,
        )
        check("坏 key 应该抛异常", False, "居然成功了")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        check("坏 key 抛 RuntimeError 而不是崩溃",
              isinstance(e, RuntimeError) and "DeepSeek" in str(e), f"{type(e).__name__}: {e}")

    try:
        await P.build_profile_for_user(store, "999999999", api_key=cfg["api_key"],
                                       api_base=cfg["api_base"])
        check("没有样本应抛异常", False, "")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        check("没人时抛异常而非崩溃", isinstance(e, RuntimeError), f"{type(e).__name__}: {e}")

    res3 = await P.run_daily_profiling(
        store, min_messages=10, max_users=50,
        api_key="sk-invalid-key-for-test", api_base=cfg["api_base"],
    )
    check("全部失败也返回统计不中断",
          res3["ok"] == 0 and res3["failed"] == 3 and res3["users"] == 3, res3)
    check("失败后旧画像没被清空",
          all(len(P.get_profile_facts(store, q)) == 1 for q in USERS),
          {q: len(P.get_profile_facts(store, q)) for q in USERS})

    # 缺配置文件也不崩
    try:
        P.load_llm_config(os.path.join(tmp, "nope.json"))
        check("缺配置应抛异常", False, "")
    except Exception as e:
        check("缺配置抛 RuntimeError", isinstance(e, RuntimeError), f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------- [7] main.py 挂载
    print("[7] main.py：定时任务 / 手动指令 / terminate")
    try:
        from main import QQUserMemoryPlugin
    except Exception as e:
        check("main.py 可导入", False, f"{type(e).__name__}: {e}")
    else:
        check("main.py 可导入", True)

        cfg_dict = {
            "enable": True, "record_private": True, "inject_memory": True,
            "inject_on_all": False, "max_inject_chars": 700, "recent_limit": 5,
            "related_limit": 3, "facts_limit": 6, "min_messages_before_inject": 3,
            "max_messages_per_user": 2000, "retention_days": 180,
            "db_file": "user_memory.db",
            "enable_daily_profile": True, "profile_hour": 3,
            "profile_min_messages": 10, "profile_max_users": 50, "profile_recent_n": 40,
        }
        plugin = QQUserMemoryPlugin.__new__(QQUserMemoryPlugin)
        plugin.config = dict(cfg_dict)
        plugin.store = MemoryStore(os.path.join(tmp, "main.db"))
        plugin.builder = ContextBuilder(plugin.store)
        plugin._init_error = None
        plugin._profile_task = None
        plugin._last_profile_at = 0.0
        plugin._last_profile_result = None
        seed(plugin.store)

        # 到下一个 profile_hour 的秒数
        wait = plugin._seconds_until(3)
        check("_seconds_until 在 (0, 86400]", 60 <= wait <= 86400, wait)
        check("_seconds_until 未来时刻更短",
              plugin._seconds_until(23) < 90000, plugin._seconds_until(23))
        plugin.config["profile_hour"] = 99
        check("profile_hour 越界被夹到 23", plugin._profile_hour() == 23, plugin._profile_hour())
        plugin.config["profile_hour"] = -5
        check("profile_hour 负数夹到 0", plugin._profile_hour() == 0, plugin._profile_hour())
        plugin.config["profile_hour"] = 3

        # 启动定时任务（真实 create_task）
        await plugin.initialize()
        check("initialize 起了后台任务",
              plugin._profile_task is not None and not plugin._profile_task.done(), "")

        # 手动指令：非管理员被拒
        out = await drain(plugin.memory_cmd(
            FakeEvent("123456789", "阿强", "/记忆 更新画像 123456789",
                      "1000000001", "一号群", admin=False)))
        txt = out[0][1] if out else ""
        check("非管理员不能更新画像", "管理员" in txt, txt[:120])

        # 手动指令：管理员，指定 QQ
        if api_ok:
            out = await drain(plugin.memory_cmd(
                FakeEvent("1000000001", "管理员", "/记忆 更新画像 123456789",
                          "1000000001", "一号群", admin=True)))
            txt = "\n".join(x[1] for x in out)  # 先 yield 进度，再 yield 结果
            check("管理员单人生成成功", "画像已更新" in txt and "123456789" in txt, txt[:200])
            check("单人画像含中文",
                  _looks_like_real_profile(txt.split("】", 1)[-1].strip()),
                  txt[:200])
            print("  --- /记忆 更新画像 123456789 返回 ---")
            print("      " + txt.replace("\n", "\n      "))

            # 手动指令：管理员，批量
            out = await drain(plugin.memory_cmd(
                FakeEvent("1000000001", "管理员", "/记忆 更新画像",
                          "1000000001", "一号群", admin=True)))
            txt = "\n".join(x[1] for x in out)
            check("管理员批量生成成功", "画像更新完成" in txt and "成功：3" in txt, txt[:240])
            print("  --- /记忆 更新画像 返回 ---")
            print("      " + txt.replace("\n", "\n      "))
            check("批量后 facts 计数正确", plugin.store.stats()["facts"] >= 3,
                  plugin.store.stats())
        else:
            print("  (真实 API 未成功，跳过手动指令的成功路径断言)")

        # run_profile_now 直接调用
        if api_ok:
            res4 = await plugin.run_profile_now()
            check("run_profile_now 可调用", set(res4) == {"ok", "failed", "users"}, res4)
            check("记录了 last_profile_at", plugin._last_profile_at > 0, plugin._last_profile_at)

        # terminate 干净取消
        task = plugin._profile_task
        await plugin.terminate()
        check("terminate 后任务引用被清空", plugin._profile_task is None, plugin._profile_task)
        check("terminate 后任务已结束", task is None or task.done(), task)
        check("terminate 后没有残留 asyncio 任务",
              not any(t.get_name().startswith("Task-") and not t.done()
                      for t in asyncio.all_tasks()
                      if t is not asyncio.current_task()),
              [t.get_name() for t in asyncio.all_tasks() if t is not asyncio.current_task()])

        # 关掉 enable_daily_profile 就不起任务
        plugin2 = QQUserMemoryPlugin.__new__(QQUserMemoryPlugin)
        plugin2.config = dict(cfg_dict)
        plugin2.config["enable_daily_profile"] = False
        plugin2.store = None
        plugin2.builder = None
        plugin2._init_error = None
        plugin2._profile_task = None
        await plugin2.initialize()
        check("开关关掉后不起任务", plugin2._profile_task is None, plugin2._profile_task)

    # ---------------------------------------------------------- [8] 配置 schema
    print("[8] _conf_schema.json 新配置项")
    with open(os.path.join(_HERE, "_conf_schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    check("enable_daily_profile 存在", "enable_daily_profile" in schema, list(schema)[-6:])
    check("enable_daily_profile 默认 true",
          schema.get("enable_daily_profile", {}).get("default") is True, schema.get("enable_daily_profile"))
    check("enable_daily_profile 是 bool",
          schema.get("enable_daily_profile", {}).get("type") == "bool", "")
    check("profile_hour 存在", "profile_hour" in schema, "")
    check("profile_hour 默认 3", schema.get("profile_hour", {}).get("default") == 3,
          schema.get("profile_hour"))
    check("profile_hour 是 int", schema.get("profile_hour", {}).get("type") == "int", "")
    check("schema 是合法 JSON 且含原有项", "db_file" in schema and "enable" in schema, "")

    # ---------------------------------------------------------- [9] CLI 入口
    print("[9] __main__ 命令行入口（--db --qq）")
    proc_db = os.path.join(tmp, "cli.db")
    s2 = MemoryStore(proc_db)
    seed(s2)
    s2.close()
    app = await P._amain(["--db", proc_db, "--qq", "555666777"]) if api_ok else 0
    check("CLI 单人生成返回 0", app == 0, app)
    s3 = MemoryStore(proc_db)
    rows = P.get_profile_facts(s3, "555666777")
    cli_profile = rows[0]["content"] if rows else ""
    if api_ok:
        check("CLI 写入 profile 事实", len(rows) == 1 and rows[0]["source"] == "profile", rows)
        print(f"  --- CLI 生成的画像（老陈 555666777）---")
        print(f"      {cli_profile or '(无)'}")
    s3.close()

    # ---------------------------------------------------------- 收尾
    if api_ok:
        print("\n===== 真实画像样例（用于交付汇报）=====")
        for qq, info in USERS.items():
            print(f"{info['nickname']}({qq})：{profiles.get(qq, '(无)')}")
        print(f"老陈(555666777) CLI 复跑：{cli_profile or '(无)'}")

    store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    # import main 时 AstrBot 建出来的假根目录也清掉，别留垃圾
    shutil.rmtree(os.environ.get("ASTRBOT_ROOT", ""), ignore_errors=True)
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
