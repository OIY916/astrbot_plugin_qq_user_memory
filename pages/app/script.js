(async function () {
  "use strict";

  // ⚠️ 关键：框架把 bridge-sdk.js 注入在 </body> 之前，也就是**本文件之后**
  // （见 plugin_page_service.py:611-616：`replace("</body>", bridge_tag + "</body>")`）。
  // 所以脚本执行到这里时 window.AstrBotPluginPage 往往还是 undefined。
  // 必须轮询等它出现，否则会报「AstrBot 插件页面 SDK 不可用」。
  async function waitForBridge(timeoutMs = 8000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const b = window.AstrBotPluginPage || window.AstrBotPluginView;
      if (b && typeof b.apiGet === "function") return b;
      await new Promise((r) => setTimeout(r, 50));
    }
    return null;
  }

  const api = await waitForBridge();
  if (!api) {
    // 把原因写进页面，别只留个空白
    const box = document.getElementById("stats");
    if (box) {
      box.innerHTML =
        '<div class="err">加载失败：等不到 AstrBot 插件页面 SDK（bridge-sdk.js 没加载）。' +
        "请从 Dashboard 的插件详情页打开这个页面，直接访问 URL 是不行的。</div>";
    }
    return;
  }
  if (typeof api.ready === "function") {
    try {
      await api.ready();
    } catch (e) {
      // ready() 超时也不硬失败，后面 apiGet 仍可能可用
      console.warn("bridge ready() 失败:", e);
    }
  }

  // ⚠️ 前端 BASE **不带插件名**：框架的 bridge 会自动拼
  //   /api/v1/plugins/extensions/<插件名>/<endpoint>
  // 后端注册的 route 则是带插件名的（"/astrbot_plugin_qq_user_memory/qq_user_memory/stats"），
  // 两边最终都归一化成同一路径。实测：端点带插件名会 404「未找到该路由」。
  const BASE = "/qq_user_memory";

  const state = {
    users: [],
    filter: "",
    sort: "msg_count",
    currentQq: "",
  };

  const el = {};
  [
    "refreshBtn", "stats", "userFilter", "sortSelect", "userList",
    "emptyHint", "profilePane", "profileName", "profileMeta", "profileAffinity",
    "profileFacts", "manualFacts", "profileGroups", "profileNicks", "profileRecent",
    "affinityPane", "affinityList", "searchPane", "searchInput", "searchBtn",
    "searchStatus", "searchResults", "groupsPane", "groupsList",
    "tabUsers", "tabAffinity", "tabGroups", "tabSearch",
    "viewAffinityBtn", "backToUsersBtn", "backFromAffinityBtn", "backFromGroupsBtn",
  ].forEach((id) => { el[id] = document.getElementById(id); });

  // ---------------------------------------------------------- 基础
  async function get(endpoint, params) {
    if (!api || typeof api.apiGet !== "function") {
      throw new Error("AstrBot 插件页面 SDK 不可用");
    }
    return await api.apiGet(endpoint, params || {});
  }

  async function post(endpoint, body) {
    if (!api || typeof api.apiPost !== "function") {
      throw new Error("AstrBot 插件页面 SDK 不可用");
    }
    return await api.apiPost(endpoint, body || {});
  }

  function unwrap(res) {
    // 后端统一返回 {ok, data} 或 {ok:false, error}
    if (!res) throw new Error("空响应");
    if (res.ok === false) throw new Error(res.error || "请求失败");
    if (res.ok === true) return res.data;
    // 兼容 SDK 直接返回 data 的情况
    return res;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtNum(n) {
    n = Number(n || 0);
    if (n >= 10000) return (n / 10000).toFixed(1) + " 万";
    return String(n);
  }

  function affinityClass(score) {
    if (score == null) return "aff-none";
    if (score >= 80) return "aff-high";
    if (score >= 50) return "aff-mid";
    if (score >= 30) return "aff-low";
    return "aff-cold";
  }

  function affinityLabel(score) {
    if (score == null) return "未评估";
    if (score >= 80) return "非常亲近";
    if (score >= 50) return "一般熟";
    if (score >= 30) return "不太熟";
    return "冷淡";
  }

  // ---------------------------------------------------------- 统计条
  async function loadStats() {
    try {
      const d = unwrap(await get(BASE + "/stats"));
      el.stats.innerHTML = `
        <div class="stat"><span class="v">${fmtNum(d.users)}</span><span class="k">记住的人</span></div>
        <div class="stat"><span class="v">${fmtNum(d.messages)}</span><span class="k">条发言</span></div>
        <div class="stat"><span class="v">${fmtNum(d.facts)}</span><span class="k">条事实/画像</span></div>
        <div class="stat"><span class="v">${fmtNum(d.groups)}</span><span class="k">个会话</span></div>
        <div class="stat wide"><span class="v small">${esc(d.last_rel || "—")}</span><span class="k">最近记录</span></div>
      `;
    } catch (e) {
      el.stats.innerHTML = `<div class="err">统计加载失败：${esc(e.message)}</div>`;
    }
  }

  // ---------------------------------------------------------- 用户列表
  function renderUsers() {
    const q = state.filter.trim().toLowerCase();
    const list = state.users.filter((u) => {
      if (!q) return true;
      return (
        String(u.qq).toLowerCase().includes(q) ||
        String(u.nickname || "").toLowerCase().includes(q)
      );
    });

    if (!list.length) {
      el.userList.innerHTML = `<div class="empty small">没有匹配的用户</div>`;
      return;
    }

    el.userList.innerHTML = list
      .map((u) => {
        const nick = u.nickname || "（无昵称）";
        const active = u.qq === state.currentQq ? " active" : "";
        const aff = u.affinity;
        return `
          <button class="user-item${active}" data-qq="${esc(u.qq)}" type="button">
            <span class="user-main">
              <span class="user-name">${esc(nick)}</span>
              <span class="user-sub">${esc(u.qq)} · ${fmtNum(u.msg_count)} 条 · ${esc(u.last_rel || "")}</span>
            </span>
            <span class="badge ${affinityClass(aff)}">${aff == null ? "—" : Math.round(aff)}</span>
          </button>`;
      })
      .join("");
  }

  async function loadUsers() {
    try {
      const d = unwrap(await get(BASE + "/users", { limit: 500, sort: state.sort }));
      state.users = d.items || [];
      renderUsers();
    } catch (e) {
      el.userList.innerHTML = `<div class="err">用户列表加载失败：${esc(e.message)}</div>`;
    }
  }

  // ---------------------------------------------------------- 单人档案
  function showPane(which) {
    ["profilePane", "affinityPane", "searchPane", "groupsPane"].forEach((k) => {
      el[k].classList.add("hide");
    });
    el.emptyHint.classList.add("hide");
    if (which) el[which].classList.remove("hide");

    // tab 高亮
    el.tabUsers.classList.toggle("active", which === null || which === "profilePane");
    el.tabAffinity.classList.toggle("active", which === "affinityPane");
    el.tabGroups.classList.toggle("active", which === "groupsPane");
    el.tabSearch.classList.toggle("active", which === "searchPane");
  }

  async function loadProfile(qq) {
    state.currentQq = qq;
    renderUsers();
    try {
      const d = unwrap(await get(BASE + "/profile", { qq }));
      el.profileName.textContent = d.nickname || "（无昵称）";
      el.profileMeta.textContent =
        `QQ ${d.qq} · 累计 ${fmtNum(d.msg_count)} 条 · ` +
        `首次 ${d.first_seen || "—"} · 最近 ${d.last_rel || "—"}`;

      const a = d.affinity || {};
      el.profileAffinity.innerHTML = `
        <span class="aff-score ${affinityClass(a.score)}">
          ${a.score == null ? "—" : Math.round(a.score)}<small>/100</small>
        </span>
        <span class="aff-desc">
          <strong>${esc(affinityLabel(a.score))}</strong>
          ${a.locked ? '<span class="lock">🔒 手动锁定</span>' : ""}
          <br /><small>${esc(a.reason || "暂无评估理由")}${a.updated_at ? " · " + esc(a.updated_at) : ""}</small>
        </span>`;

      el.profileFacts.innerHTML = (d.profile_facts || []).length
        ? d.profile_facts.map((f) => `<div class="fact profile">${esc(f.content)}</div>`).join("")
        : `<div class="empty small">还没有生成画像（需要 ≥20 条发言，每天 00:00 自动生成）</div>`;

      el.manualFacts.innerHTML = (d.manual_facts || []).length
        ? d.manual_facts.map((f) => `<div class="fact">${esc(f.content)}</div>`).join("")
        : `<div class="empty small">没有手动记录</div>`;

      el.profileGroups.innerHTML = (d.groups || []).length
        ? d.groups
            .map(
              (g) =>
                `<span class="chip">${esc(g.label)}<b>${fmtNum(g.msg_count)}</b></span>`
            )
            .join("")
        : `<div class="empty small">无记录</div>`;

      el.profileNicks.innerHTML = (d.nicknames || []).length
        ? d.nicknames
            .map((n) => `<span class="chip">${esc(n.name)}<b>${n.count}</b></span>`)
            .join("")
        : `<div class="empty small">无记录</div>`;

      el.profileRecent.innerHTML = (d.recent || []).length
        ? d.recent
            .map(
              (m) => `
              <div class="entry">
                <div class="entry-head">
                  <span class="tag">${esc(m.label)}</span>
                  <span class="time">${esc(m.at)} · ${esc(m.rel)}</span>
                </div>
                <div class="entry-body">${esc(m.content)}</div>
              </div>`
            )
            .join("")
        : `<div class="empty small">无记录</div>`;

      showPane("profilePane");
    } catch (e) {
      el.profileName.textContent = "加载失败";
      el.profileMeta.textContent = e.message;
      el.profileFacts.innerHTML = el.manualFacts.innerHTML = "";
      el.profileGroups.innerHTML = el.profileNicks.innerHTML = el.profileRecent.innerHTML = "";
      el.profileAffinity.innerHTML = "";
      showPane("profilePane");
    }
  }

  // ---------------------------------------------------------- 好感排行
  async function loadAffinity() {
    try {
      const d = unwrap(await get(BASE + "/affinity", { limit: 40 }));
      const items = d.items || [];
      el.affinityList.innerHTML = items.length
        ? items
            .map(
              (it, i) => `
              <div class="affrow">
                <span class="rank">${i + 1}</span>
                <span class="affrow-main">
                  <span class="user-name">${esc(it.nickname || "（无昵称）")}</span>
                  <span class="user-sub">${esc(it.qq)} · ${fmtNum(it.msg_count)} 条</span>
                  <span class="affrow-reason">${esc(it.reason || "")}</span>
                </span>
                <span class="aff-score ${affinityClass(it.score)}">
                  ${Math.round(it.score)}${it.locked ? ' <span class="lock">🔒</span>' : ""}
                </span>
              </div>`
            )
            .join("")
        : `<div class="empty small">还没有好感度数据</div>`;
      showPane("affinityPane");
    } catch (e) {
      el.affinityList.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
      showPane("affinityPane");
    }
  }

  // ---------------------------------------------------------- 群组
  async function loadGroups() {
    try {
      const d = unwrap(await get(BASE + "/groups", {}));
      const items = d.items || [];
      el.groupsList.innerHTML = items.length
        ? items
            .map(
              (g) => `
              <div class="grouprow">
                <span class="group-main">
                  <span class="user-name">${esc(g.label)}</span>
                  <span class="user-sub">${esc(g.group_id || "私聊")} · 最近 ${esc(g.last_rel || "—")}</span>
                </span>
                <span class="group-nums">
                  ${fmtNum(g.people)} 人 / ${fmtNum(g.messages)} 条
                </span>
              </div>`
            )
            .join("")
        : `<div class="empty small">无数据</div>`;
      showPane("groupsPane");
    } catch (e) {
      el.groupsList.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
      showPane("groupsPane");
    }
  }

  // ---------------------------------------------------------- 检索
  async function doSearch() {
    const kw = el.searchInput.value.trim();
    if (!kw) {
      el.searchStatus.textContent = "输入点什么再搜";
      return;
    }
    el.searchStatus.className = "status";
    el.searchStatus.textContent = "搜索中...";
    el.searchResults.innerHTML = "";
    try {
      const d = unwrap(await post(BASE + "/search", { q: kw, limit: 30 }));
      el.searchStatus.textContent = `「${kw}」找到 ${d.total} 条`;
      const items = d.items || [];
      if (!items.length) {
        el.searchResults.innerHTML = `<div class="empty small">没搜到，换个词试试</div>`;
        return;
      }
      el.searchResults.innerHTML = items
        .map(
          (m) => `
          <div class="entry">
            <div class="entry-head">
              <span class="tag">${esc(m.label)}</span>
              <span class="nick" data-qq="${esc(m.qq)}">${esc(m.nickname || m.qq)}</span>
              <span class="time">${esc(m.at)} · ${esc(m.rel)}</span>
            </div>
            <div class="entry-body">${esc(m.content)}</div>
          </div>`
        )
        .join("");
      // 点击昵称跳档案
      el.searchResults.querySelectorAll(".nick").forEach((node) => {
        node.addEventListener("click", () => {
          const qq = node.getAttribute("data-qq");
          if (qq) loadProfile(qq);
        });
      });
      showPane("searchPane");
    } catch (e) {
      el.searchStatus.className = "status error";
      el.searchStatus.textContent = "搜索失败：" + e.message;
    }
  }

  // ---------------------------------------------------------- 事件绑定
  el.refreshBtn.addEventListener("click", async () => {
    await Promise.all([loadStats(), loadUsers()]);
  });

  el.userFilter.addEventListener("input", () => {
    state.filter = el.userFilter.value;
    renderUsers();
  });

  el.sortSelect.addEventListener("change", async () => {
    state.sort = el.sortSelect.value;
    await loadUsers();
  });

  el.userList.addEventListener("click", (ev) => {
    const item = ev.target.closest(".user-item");
    if (!item) return;
    const qq = item.getAttribute("data-qq");
    if (qq) loadProfile(qq);
  });

  el.searchBtn.addEventListener("click", doSearch);
  el.searchInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") doSearch();
  });

  el.viewAffinityBtn.addEventListener("click", loadAffinity);
  el.backToUsersBtn.addEventListener("click", () => {
    state.currentQq = "";
    renderUsers();
    showPane(null);
    el.emptyHint.classList.remove("hide");
  });
  el.backFromAffinityBtn.addEventListener("click", () => {
    showPane(null);
    el.emptyHint.classList.remove("hide");
  });
  el.backFromGroupsBtn.addEventListener("click", () => {
    showPane(null);
    el.emptyHint.classList.remove("hide");
  });

  el.tabUsers.addEventListener("click", () => {
    showPane(null);
    el.emptyHint.classList.remove("hide");
  });
  el.tabAffinity.addEventListener("click", loadAffinity);
  el.tabGroups.addEventListener("click", loadGroups);
  el.tabSearch.addEventListener("click", () => showPane("searchPane"));

  // ---------------------------------------------------------- 启动
  await Promise.all([loadStats(), loadUsers()]);
  showPane(null);
  el.emptyHint.classList.remove("hide");
})();
