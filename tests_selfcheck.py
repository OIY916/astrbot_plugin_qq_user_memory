# -*- coding: utf-8 -*-
"""存储层自测：不依赖 AstrBot，直接跑 memory_store.py

    E:\\qq-bot\\astrbot-venv\\Scripts\\python.exe tests_selfcheck.py

临时库默认建在**本插件目录下的 _selftest_tmp/**（E 盘），跑完自动删除，
不会在 C 盘 %TEMP% 留残留（本机 C 盘紧张）。
"""
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

from memory_store import (  # noqa: E402
    AFFINITY_DEFAULT,
    ContextBuilder,
    MemoryStore,
    affinity_tier,
    search_text,
    segment_cjk,
)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="umtest_")
    db = os.path.join(tmp, "test.db")
    store = MemoryStore(db)
    builder = ContextBuilder(store)

    print("[1] 中文切分")
    check("segment 中文逐字", segment_cjk("今天钓鱼") == "今 天 钓 鱼", segment_cjk("今天钓鱼"))
    check("segment 英文成词", segment_cjk("hello world") == "hello world", segment_cjk("hello world"))
    check("segment 混合", segment_cjk("i love 钓鱼2") == "i love 钓 鱼 2", segment_cjk("i love 钓鱼2"))

    print("[2] 写入 & 跨群聚合")
    now = time.time()
    data = [
        ("111", "小明", "g1", "一号群", "今天天气很好我想去钓鱼"),
        ("111", "小明", "g2", "二号群", "我养了一只猫叫小白"),
        ("111", "大明", "g3", "三号群", "周末去爬山了"),
        ("222", "小红", "g1", "一号群", "钓鱼真的很难"),
        ("222", "小红", "g2", "二号群", "hello world fishing trip"),
    ]
    for i, (qq, nick, gid, gname, txt) in enumerate(data):
        ok = store.record_message(qq, nick, gid, gname, txt, created_at=now + i)
        check(f"record {qq}/{gid}", ok)

    u = store.get_user("111")
    check("users 聚合", u and u["msg_count"] == 3, u)
    check("昵称变更汇总", [n for n, _ in store.get_nicknames("111")] == ["小明", "大明"],
          store.get_nicknames("111"))
    check("跨群列表", {g["group_id"] for g in store.get_groups("111")} == {"g1", "g2", "g3"},
          store.get_groups("111"))

    print("[3] FTS 检索")
    hits = store.search("钓鱼", limit=10)
    check("查中文子串 钓鱼", len(hits) >= 2, [h["content"] for h in hits])
    hits1 = store.search("钓鱼", limit=10, qq="111")
    check("按 QQ 过滤", len(hits1) == 1 and hits1[0]["qq"] == "111", hits1)
    hits_set = {h["content"] for h in hits1}
    check("按 QQ 过滤不含他人", "钓鱼真的很难" not in hits_set, hits_set)
    check("查英文词 fishing", any("fishing" in h["content"] for h in store.search("fishing")),
          store.search("fishing"))
    check("查英文 hello", any("hello" in h["content"] for h in store.search("hello")),
          store.search("hello"))
    check("不存在的词", store.search("量子力学") == [], store.search("量子力学"))

    print("[3b] 跨群读取（排除当前群）")
    rm = store.recent_messages_other_groups("111", "g3", 10)
    check("other_groups 排除当前群", {m["group_id"] for m in rm} == {"g1", "g2"},
          [(m["group_id"], m["content"]) for m in rm])
    rm_all = store.recent_messages_other_groups("111", "", 10)
    check("私聊（exclude='') 返回全部群", {m["group_id"] for m in rm_all} == {"g1", "g2", "g3"},
          rm_all)
    check("other_groups 尊重 limit", len(store.recent_messages_other_groups("111", "g3", 1)) == 1,
          store.recent_messages_other_groups("111", "g3", 1))
    check("count_messages_other_groups 正确",
          store.count_messages_other_groups("111", "g3") == 2,
          store.count_messages_other_groups("111", "g3"))
    check("count 私聊模式 = 全部", store.count_messages_other_groups("111", "") == 3,
          store.count_messages_other_groups("111", ""))
    sg = store.search_other_groups("钓鱼", "g3", 10, qq="111")
    check("search_other_groups 只返回其他群",
          len(sg) == 1 and sg[0]["group_id"] == "g1",
          [(m["group_id"], m["content"]) for m in sg])
    check("search_other_groups 排除当前群命中", store.search_other_groups("爬山", "g3", 10, qq="111") == [],
          store.search_other_groups("爬山", "g3", 10, qq="111"))
    check("search_other_groups 私聊模式不排除", len(store.search_other_groups("爬山", "", 10, qq="111")) == 1,
          store.search_other_groups("爬山", "", 10, qq="111"))
    check("search 旧签名仍可用", len(store.search("钓鱼", 10, qq="111")) == 1,
          store.search("钓鱼", 10, qq="111"))

    print("[4] 事实 & 删除")
    fid = store.add_fact("111", "小明养了一只叫小白的猫")
    check("add_fact", fid > 0, fid)
    check("get_facts", store.get_facts("111") and "小白" in store.get_facts("111")[0]["content"],
          store.get_facts("111"))
    n = store.delete_user("222")
    check("delete_user 返回条数", n == 2, n)
    check("删除后搜不到", all(h["qq"] != "222" for h in store.search("钓鱼", limit=20)),
          store.search("钓鱼", limit=20))
    check("删除后 FTS 一致", store.search("fishing") == [], store.search("fishing"))

    print("[5] 注入上下文（只注入跨群信息）")
    block = builder.build("111", "小明", "g3", "钓鱼好玩吗", max_chars=700)
    check("build 非空", bool(block), block)
    check("含 QQ 号", "111" in block, block)
    check("含昵称演变", "大明" in block or "小明" in block, block)
    check("含其他群", "g1" in block or "一号群" in block, block)
    check("含事实", "小白" in block, block)
    check("含相关旧发言", "钓鱼" in block, block)
    check("标题是「在其他群里说过」", "在其他群里说过：" in block, block)
    check("旧标题「最近说过」已删除", "最近说过：" not in block, block)
    # 当前群 g3 的发言（周末去爬山了）不该出现在注入里
    check("不含当前群内容", "爬山" not in block, block)
    check("max_chars 生效", len(block) <= 700 + 40, len(block))
    import inspect as _inspect
    _sig = _inspect.signature(ContextBuilder.build)
    check("默认 max_chars=200", _sig.parameters["max_chars"].default == 200,
          _sig.parameters["max_chars"].default)
    check("默认 max_chars 生效", len(builder.build("111", "小明", "g3", "钓鱼")) <= 240,
          len(builder.build("111", "小明", "g3", "钓鱼")))
    check("查无此人返回空", builder.build("999", "x", "g1", "hi") == "",
          builder.build("999", "x", "g1", "hi"))
    thin = builder.build("111", "小明", "g1", "", related_limit=0)
    check("无当前话题也能构造", bool(thin), thin)

    print("[5b] 单群用户：不注入跨群内容，但**要**注入好感度")
    # 只在 g1 说过话的用户：没有跨群价值，但好感度必须给
    # （实测 27 人里 24 人是单群用户，不给好感度就等于 89% 的人行为无差异）
    for txt in ["单群第一句", "单群第二句", "单群第三句"]:
        store.record_message("555", "单群用户", "g1", "一号群", txt)
    solo = builder.build("555", "单群用户", "g1", "随便聊聊")
    check("单群用户也拿到好感度", "好感" in solo, solo)
    check("单群用户不含跨群块标题", "[关于这位群友的记忆]" not in solo, solo)
    check("单群用户不泄漏具体发言", "单群第一句" not in solo, solo)
    # 跨 gA / gB 两个群：在 gB 里只应出现 gA 的内容
    store.record_message("666", "跨群", "gA", "A群", "我在A群说钓鱼装备")
    store.record_message("666", "跨群", "gA", "A群", "A群的人叫我老六")
    store.record_message("666", "跨群", "gB", "B群", "我在B群说钓鱼装备")
    block2 = builder.build("666", "跨群", "gB", "钓鱼", max_chars=1000)
    check("跨群用户有注入", bool(block2), block2)
    check("只含另一个群", "A群" in block2, block2)
    check("不含当前群内容", "B群" not in block2, block2)
    check("跨群最近发言条数正确", block2.count("- ") == 2, block2)

    print("[5c] 画像（source=profile）与手动事实分开展示")
    store.add_fact("666", "长期爱好：钓鱼、爬山", source="profile")
    store.add_fact("666", "手动记的：喜欢喝冰美式", source="manual")
    block3 = builder.build("666", "跨群", "gB", "", max_chars=2000)
    check("画像标题为【画像】", "【画像】" in block3, block3)
    check("画像内容出现", "长期爱好" in block3, block3)
    check("手动事实标题仍是「已知事实：」", "已知事实：" in block3, block3)
    check("画像排在手动事实前面",
          block3.index("【画像】") < block3.index("已知事实："), block3)
    check("画像行不在「已知事实：」下面",
          block3.index("长期爱好") < block3.index("已知事实："), block3)

    print("[6] 档案 & 搜索文本")
    prof = builder.profile_text("111")
    check("profile 含统计", "累计发言" in prof, prof[:200])
    check("profile 含事实", "小白" in prof, prof[:200])
    st = search_text(store, "钓鱼")
    check("search_text 有结果", "111" in st, st)
    check("search_text 空结果", "没搜到" in search_text(store, "量子力学"), search_text(store, "量子力学"))

    print("[7] 配额 & 保留期")
    for i in range(30):
        store.record_message("333", "路人", "g1", "一号群", f"刷屏第{i}句", max_per_user=10)
    check("max_per_user 生效", store.get_user("333")["msg_count"] == 30
          and len(store.recent_messages("333", 100)) == 10,
          (store.get_user("333")["msg_count"], len(store.recent_messages("333", 100))))
    old_db = os.path.join(tmp, "old.db")
    s2 = MemoryStore(old_db)
    s2.record_message("444", "老人", "g1", "一号群", "很久以前说的话",
                      created_at=time.time() - 400 * 86400)
    s2.record_message("444", "老人", "g1", "一号群", "刚刚说的话")
    pruned = s2.prune_old(180)
    check("prune_old 删除过期", pruned == 1, pruned)
    check("prune 后仍在的能搜到", bool(s2.search("刚刚")), s2.search("刚刚"))
    check("prune 后过期搜不到", s2.search("很久") == [], s2.search("很久"))
    s2.close()

    print("[8] 好感度")
    # 初始值
    aff = store.get_affinity("777_new")
    check("未记录时初始好感 40", aff["score"] == 40.0, aff)
    # 增减 + 裁剪
    store.set_affinity("777", 50)
    check("设置后读到 50", store.get_affinity("777")["score"] == 50.0)
    check("加减正常", store.adjust_affinity("777", 10) == 60.0)
    check("下限裁剪 0", store.adjust_affinity("777", -999) == 0.0)
    check("上限裁剪 100", store.adjust_affinity("777", 999) == 100.0)
    # 分档
    check("90 分档=非常亲近", affinity_tier(90)[0] == "非常亲近", affinity_tier(90))
    check("70 分档=很熟", affinity_tier(70)[0] == "很熟", affinity_tier(70))
    check("50 分档=一般熟", affinity_tier(50)[0] == "一般熟", affinity_tier(50))
    check("30 分档=不太熟", affinity_tier(30)[0] == "不太熟", affinity_tier(30))
    check("10 分档=没什么好感", affinity_tier(10)[0] == "没什么好感", affinity_tier(10))
    # 排行
    store.set_affinity("999", 95)
    top = store.affinity_top(3)
    check("排行榜按分数倒序", len(top) >= 2 and top[0]["score"] >= top[1]["score"], top)

    print("[9] 持久化 & 重开")
    st_before = store.stats()
    store.close()
    store2 = MemoryStore(db)
    st_after = store2.stats()
    check("重开后数据还在", st_before["messages"] == st_after["messages"], (st_before, st_after))
    check("重开后 FTS 可查", bool(store2.search("钓鱼")), store2.search("钓鱼"))

    store2.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
