# -*- coding: utf-8 -*-
"""插件级集成自测：用假的 AstrMessageEvent 驱动真实插件逻辑。

    E:\\qq-bot\\astrbot-venv\\Scripts\\python.exe tests_integration.py

不需要启动 AstrBot / NapCat，验证三件事：
  1. record_message 能把不同群、不同昵称的发言归到同一个 QQ 号
  2. inject_memory 会往 req.prompt 里加记忆块（且不碰 system_prompt）
  3. /记忆 指令能正确返回档案

临时库默认建在**本插件目录下的 _selftest_tmp/**（E 盘），跑完自动删除，
不会在 C 盘 %TEMP% 留残留（本机 C 盘紧张）。
"""
import asyncio
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

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


class FakeGroup:
    def __init__(self, gid, gname=""):
        self.group_id = gid
        self.group_name = gname


class FakeObj:
    def __init__(self, gid=None, gname=""):
        self.group = FakeGroup(gid, gname) if gid else None


class FakeEvent:
    """只实现插件用到的那几个方法。"""

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


class FakeReq:
    def __init__(self):
        self.prompt = "用户的原始提问"
        self.system_prompt = "系统提示词"
        self.contexts = []


async def drain(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


async def main():
    tmp = tempfile.mkdtemp(prefix="uminteg_")
    os.environ["UM_TEST_DIR"] = tmp

    from memory_store import MemoryStore  # noqa: F401  确认可导入
    from main import QQUserMemoryPlugin

    # 用假的 Star.__init__ 路径：直接构造插件，然后替换存储目录
    class Cfg(dict):
        pass

    cfg = Cfg({
        "enable": True,
        "record_private": True,
        "inject_memory": True,
        "inject_on_all": False,
        "max_inject_chars": 700,
        "recent_limit": 5,
        "related_limit": 3,
        "facts_limit": 6,
        "min_messages_before_inject": 3,
        "max_messages_per_user": 2000,
        "retention_days": 180,
        "db_file": "user_memory.db",
    })

    plugin = QQUserMemoryPlugin.__new__(QQUserMemoryPlugin)
    plugin.config = cfg
    plugin.store = None
    plugin.builder = None
    plugin._init_error = None
    # 直接把库建在临时目录，绕开 StarTools（没有跑在 AstrBot 里）
    from memory_store import ContextBuilder, MemoryStore as MS
    plugin.store = MS(os.path.join(tmp, "user_memory.db"))
    plugin.builder = ContextBuilder(plugin.store)

    print("[1] 记录：同一人跨 3 个群")
    msgs = [
        ("123456789", "小明", "1000000001", "一号群", "今天天气很好我想去钓鱼"),
        ("123456789", "小明", "1000000002", "二号群", "我养了一只猫叫小白"),
        ("123456789", "大明", "1000000003", "三号群", "周末去爬山了"),
        ("987654321", "小红", "1000000001", "一号群", "钓鱼真的很难"),
        ("555666777", "路人", "1000000001", "一号群", "大家好"),
    ]
    for qq, nick, gid, gname, text in msgs:
        await plugin.record_message(FakeEvent(qq, nick, text, gid, gname, self_id="1999999999"))

    u = plugin.store.get_user("123456789")
    check("3 条发言全部记下", u and u["msg_count"] == 3, u)
    check("跨 3 个群可见", len(plugin.store.get_groups("123456789")) == 3, plugin.store.get_groups("123456789"))

    print("[2] 过滤：不该记的内容")
    before = plugin.store.stats()["messages"]
    for text in ["[图片]", "[表情:12]", "[At:123456]", "/唱 晴天", "   "]:
        await plugin.record_message(FakeEvent("123456789", "小明", text, "1000000001", "一号群"))
    check("占位符/指令不入库", plugin.store.stats()["messages"] == before,
          plugin.store.stats()["messages"])

    print("[2b] @提及清洗（AstrBot 的 @昵称(QQ号) 格式）")
    await plugin.record_message(FakeEvent(
        "444555666", "阿强", "@小红(987654321) 晚上吃啥", "1000000001", "一号群"))
    last = plugin.store.recent_messages("444555666", 1)[0]["content"]
    check("@昵称(QQ号) 还原成 @昵称", last == "@小红 晚上吃啥", repr(last))
    hits = plugin.store.search("987654321", limit=5)
    check("QQ 号不再污染检索", hits == [], [h["content"] for h in hits])

    print("[3] 不记录机器人自己")
    await plugin.record_message(
        FakeEvent("1999999999", "大肥鱼", "我是机器人说的话", "1000000001", "一号群", self_id="1999999999"))
    check("机器人自己不入库", plugin.store.get_user("1999999999") is None)

    print("[4] 注入：@bot 时带跨群记忆")
    req = FakeReq()
    ev = FakeEvent("123456789", "小明", "你还记得我喜欢钓鱼吗", "1000000003", "三号群", at=True)
    await plugin.inject_memory(ev, req)
    check("prompt 增加", len(req.prompt) > len("用户的原始提问"), len(req.prompt))
    check("prompt 含记忆块", "[关于这位群友的记忆]" in req.prompt, req.prompt[:200])
    check("prompt 含 QQ 号", "123456789" in req.prompt, req.prompt[:300])
    check("prompt 含跨群信息", "1000000001" in req.prompt or "一号群" in req.prompt, req.prompt[:400])
    check("prompt 含昵称演变", "大明" in req.prompt, req.prompt[:400])
    check("标题是「在其他群里说过」", "在其他群里说过：" in req.prompt, req.prompt[:600])
    check("旧标题「最近说过」不再出现", "最近说过：" not in req.prompt, req.prompt[:600])
    check("不含当前群（三号群）的内容", "爬山" not in req.prompt, req.prompt[:600])
    check("system_prompt 未被动", req.system_prompt == "系统提示词", req.system_prompt)

    print("[4b] 注入：单群用户不注入")
    # 路人 只在一个群里说过话；即使话够多，在当前群也没跨群价值
    for i in range(4):
        await plugin.record_message(FakeEvent(
            "555666777", "路人", f"单群发言第{i}句", "1000000001", "一号群"))
    req_solo = FakeReq()
    await plugin.inject_memory(FakeEvent(
        "555666777", "路人", "你还记得我吗", "1000000001", "一号群", at=True), req_solo)
    check("单群用户不注入", "[关于这位群友的记忆]" not in req_solo.prompt, req_solo.prompt)
    # 换个群就有跨群内容了（一号群 → 二号群）
    req_cross = FakeReq()
    await plugin.inject_memory(FakeEvent(
        "555666777", "路人", "你还记得我吗", "1000000002", "二号群", at=True), req_cross)
    check("换群后有注入", "[关于这位群友的记忆]" in req_cross.prompt, req_cross.prompt[:400])
    check("换群后只含一号群内容", "一号群" in req_cross.prompt or "1000000001" in req_cross.prompt,
          req_cross.prompt[:400])
    check("换群后不含当前群（二号群）", "1000000002" not in req_cross.prompt, req_cross.prompt[:600])

    print("[4c] 注入：profile 事实标【画像】，手动事实标「已知事实：」")
    plugin.store.add_fact("123456789", "长期画像：喜欢钓鱼和猫", source="profile")
    plugin.store.add_fact("123456789", "手动记的：爱喝冰美式", source="manual")
    req_prof = FakeReq()
    await plugin.inject_memory(FakeEvent(
        "123456789", "小明", "钓鱼吗", "1000000003", "三号群", at=True), req_prof)
    check("含【画像】", "【画像】" in req_prof.prompt, req_prof.prompt[:600])
    check("含已知事实", "已知事实：" in req_prof.prompt, req_prof.prompt[:600])
    check("画像在已知事实之前",
          req_prof.prompt.index("【画像】") < req_prof.prompt.index("已知事实："),
          req_prof.prompt[:600])
    check("画像内容出现", "长期画像：喜欢钓鱼和猫" in req_prof.prompt, req_prof.prompt[:600])
    check("手动事实出现在已知事实段",
          req_prof.prompt.index("长期画像") < req_prof.prompt.index("已知事实：")
          and req_prof.prompt.index("爱喝冰美式") > req_prof.prompt.index("已知事实："),
          req_prof.prompt[:600])

    print("[5] 注入：非 @ 不注入")
    req2 = FakeReq()
    await plugin.inject_memory(
        FakeEvent("123456789", "小明", "自言自语", "1000000003", "三号群", at=False), req2)
    check("非 @ 不注入", req2.prompt == "用户的原始提问", req2.prompt)

    print("[6] 注入：陌生人 / 话太少")
    req3 = FakeReq()
    await plugin.inject_memory(
        FakeEvent("777888999", "陌生", "第一次说话", "1000000001", "一号群", at=True), req3)
    check("陌生人无记忆块", "[关于这位群友的记忆]" not in req3.prompt, req3.prompt)
    req4 = FakeReq()
    await plugin.inject_memory(
        FakeEvent("555666777", "路人", "我又来了", "1000000001", "一号群", at=True), req4)
    check("话太少不注入（<3 条）", "[关于这位群友的记忆]" not in req4.prompt, req4.prompt)

    print("[7] 指令：/记忆 查")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 查 123456789", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("查得到档案", "记忆档案" in txt, txt[:200])
    check("档案含跨群", "一号群" in txt or "1000000001" in txt, txt[:400])
    check("档案含发言数", "累计发言：3" in txt, txt[:300])

    print("[8] 指令：/记忆 查 昵称")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 查 大明", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("按昵称能反查到 QQ", "123456789" in txt, txt[:200])

    print("[8b] 指令：不带斜杠 + @昵称(QQ号) 形式")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "记忆 查 @大明(123456789)", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("无斜杠也能解析", "记忆档案" in txt, txt[:200])

    print("[9] 指令：/记忆 搜")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 搜 钓鱼", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("搜得到", "钓鱼" in txt and "123456789" in txt, txt[:300])
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 搜 量子力学", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("搜不到时给明确提示", "没搜到" in txt, txt[:200])

    print("[10] 指令：/记忆 记住 + 忘记")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 记住 123456789 喜欢喝冰美式", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("事实已记录", "已记下" in txt, txt[:200])
    check("事实入库", any("冰美式" in f["content"] for f in plugin.store.get_facts("123456789")),
          plugin.store.get_facts("123456789"))

    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 忘记 123456789", "1000000003", "三号群", admin=False)))
    txt = out[0][1] if out else ""
    check("非管理员不能删", "管理员" in txt, txt[:200])

    out = await drain(plugin.memory_cmd(
        FakeEvent("1000000001", "管理员", "/记忆 忘记 987654321", "1000000003", "三号群", admin=True)))
    txt = out[0][1] if out else ""
    check("管理员可删", "已删除" in txt, txt[:200])
    check("删除后查不到", plugin.store.get_user("987654321") is None)

    print("[11] 指令：/记忆 统计 与 帮助")
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆 统计", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("统计可用", "记忆库统计" in txt and "人数" in txt, txt[:200])
    out = await drain(plugin.memory_cmd(
        FakeEvent("123456789", "小明", "/记忆", "1000000003", "三号群")))
    txt = out[0][1] if out else ""
    check("帮助可用", "用法" in txt and "/记忆 查" in txt, txt[:200])

    print("[12] 群白名单")
    cfg["enabled_groups"] = ["1000000001"]
    check("白名单内 = 生效", plugin._enabled(FakeEvent("10000", "a", "x", "1000000001", "g")))
    check("白名单外 = 不生效", not plugin._enabled(FakeEvent("10000", "a", "x", "999999", "g")))

    plugin.store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
