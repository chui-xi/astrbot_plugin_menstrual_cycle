/* 生理周期 · 控制台前端
   原生 JS，无构建。跟 AstrBot 的通信全部走 bridge，不自己发 HTTP。
   页面数据都是不可信内容，所有插值一律 esc()。 */

const PHASES = {
  menstrual: { name: "月经期", color: "#cf4f88", soft: "#fbe4ee" },
  follicular: { name: "卵泡期", color: "#3f9e6f", soft: "#dff2e8" },
  ovulation: { name: "排卵期", color: "#c08f14", soft: "#fbf0d4" },
  luteal: { name: "黄体期", color: "#6f6bc4", soft: "#e6e5fa" },
  pms: { name: "经前期", color: "#d2643c", soft: "#fbe6dc" },
};

const VECTOR_LABELS = {
  valence: "愉悦度",
  arousal: "唤起",
  energy: "精力",
  stress: "压力",
  pain: "疼痛",
  sensitive: "敏感",
  libido: "亲密意愿",
  social: "社交意愿",
};

const el = {
  subtitle: document.getElementById("subtitle"),
  chipMode: document.getElementById("chip-mode"),
  chipLink: document.getElementById("chip-link"),
  stats: document.getElementById("stats"),
  search: document.getElementById("search"),
  userList: document.getElementById("user-list"),
  usersEmpty: document.getElementById("users-empty"),
  detail: document.getElementById("detail"),
  placeholder: document.getElementById("placeholder"),
  toasts: document.getElementById("toasts"),
  refresh: document.getElementById("btn-refresh"),
};

const app = {
  bridge: null,
  overview: null,
  key: null,
  filter: "",
  confirmAt: 0,
  confirmId: "",
  busy: false,
  editing: null,
  draft: null,
};

/* ------------------------------------------------------------------ utils */
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch];
  });

const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0));
const pct = (value) => `${Math.round(clamp01(value) * 100)}%`;
const fixed = (value, digits = 2) => (Number(value) || 0).toFixed(digits);

/* 本地日期的 YYYY-MM-DD，给 date 输入框用 */
function todayIso() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/* 359 分钟 -> 约 6 小时；25 -> 25 分钟 */
function span(minutes) {
  const m = Math.max(0, Math.round(Number(minutes) || 0));
  if (m >= 60) {
    const hours = Math.round((m / 60) * 10) / 10;
    return `约 ${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`;
  }
  return `${m} 分钟`;
}

/* 2026-12-10 -> 12-10 */
function shortDate(iso) {
  const text = String(iso || "");
  return text.length === 10 ? text.slice(5) : text;
}

function phaseOf(key) {
  return PHASES[key] || { name: key || "—", color: "#cf4f88", soft: "#fbe4ee" };
}

function shortKey(key) {
  const parts = String(key || "").split(":");
  if (parts.length >= 3) {
    return `${parts[0]} · ${parts[parts.length - 1]}`;
  }
  return String(key || "");
}

function labelOf(user) {
  if (user && user.nickname) return user.nickname;
  if (user && user.user_key === "character:self") return "她";
  return shortKey(user && user.user_key);
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`.trim();
  node.textContent = message;
  el.toasts.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .2s";
    setTimeout(() => node.remove(), 220);
  }, kind === "err" ? 5200 : 2800);
}

function busy(state) {
  app.busy = state;
  document.body.style.cursor = state ? "progress" : "";
}

/* ------------------------------------------------------------------ bridge */
async function waitBridge() {
  for (let i = 0; i < 40; i += 1) {
    const found =
      window.AstrBotPluginView || window.AstrBotPluginPage || null;
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return null;
}

function applyTheme(context) {
  if (!context) return;
  if (typeof context.isDark === "boolean") {
    document.documentElement.setAttribute(
      "data-theme",
      context.isDark ? "dark" : "light",
    );
  }
}

async function apiGet(endpoint, params) {
  return app.bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body) {
  return app.bridge.apiPost(endpoint, body);
}

function errorText(error) {
  const raw = String((error && error.message) || error || "请求失败");
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.message) return String(parsed.message);
    if (parsed && parsed.detail) return String(parsed.detail);
  } catch {
    /* 不是 JSON 就用原文 */
  }
  return raw.length > 240 ? `${raw.slice(0, 240)}…` : raw;
}

/* ------------------------------------------------------------------ load */
async function refresh() {
  if (!app.bridge) return;
  busy(true);
  try {
    const data = await apiGet("overview");
    if (!data || data.ok === false) {
      throw new Error((data && data.message) || "读取失败");
    }
    app.overview = data;
    renderHead(data);
    renderStats(data);
    renderList(data.users || []);
    const users = data.users || [];
    if (app.key) {
      const still = users.some((u) => u.user_key === app.key);
      if (!still) {
        app.key = null;
        showPlaceholder("这个档案已经不在了。");
        if (users.length) {
          /* 只有一个人的时候直接打开，省一次点击 */
          await openUser(users[0].user_key, { silent: true });
        }
      } else {
        await openUser(app.key, { silent: true });
      }
    } else if (users.length === 1) {
      await openUser(users[0].user_key, { silent: true });
    }
  } catch (error) {
    toast(`读取失败：${errorText(error)}`, "err");
    el.subtitle.textContent = "读取失败，稍后再试";
  } finally {
    busy(false);
  }
}

async function fetchDetail(key) {
  // 三种取法依次尝试：params / query 串 / 旧的路径形式。
  // 不同 AstrBot 版本和网关对路径里的冒号处理不一样，多试一条就多一条活路。
  const tries = [
    () => apiGet("detail", { key }),
    () => apiGet(`detail?key=${encodeURIComponent(key)}`),
    () => apiGet(`user/${key}`),
  ];
  let last = null;
  for (const attempt of tries) {
    try {
      const data = await attempt();
      if (data && data.ok !== false) return data;
      last = new Error((data && data.message) || "读取失败");
    } catch (error) {
      last = error;
    }
  }
  throw last || new Error("读取失败");
}

async function openUser(key, options = {}) {
  if (!app.bridge) return;
  app.key = key;
  renderList((app.overview && app.overview.users) || []);
  if (!options.silent) busy(true);
  try {
    const data = await fetchDetail(key);
    if (!data || data.ok === false) {
      throw new Error((data && data.message) || "读取失败");
    }
    renderDetail(data);
  } catch (error) {
    if (!options.silent) toast(`打开失败：${errorText(error)}`, "err");
  } finally {
    if (!options.silent) busy(false);
  }
}

/* ------------------------------------------------------------------ render */
function renderHead(data) {
  const plugin = data.plugin || {};
  const links = data.links || {};
  const modeMap = {
    cycle: "每周期写一次",
    realtime: "实时生成",
    off: "内置文案",
  };
  el.chipMode.textContent = `文案：${modeMap[plugin.voice_mode] || plugin.voice_mode || "—"}`;
  const linkBits = [];
  if (links.selflearn && !links.selflearn.includes("未检测到")) linkBits.push("Self Learning");
  if (links.chatplus && !links.chatplus.includes("未检测到")) linkBits.push("群情绪");
  el.chipLink.textContent = linkBits.length
    ? `情绪联动：${linkBits.join(" + ")}`
    : "情绪联动：未连接";
  el.chipLink.classList.toggle("chip-off", linkBits.length === 0);
  el.subtitle.textContent = `${data.today || ""} ｜ 档案 ${(data.stats && data.stats.users) || 0} 个 · 记录 ${
    (data.stats && data.stats.records) || 0
  } 条 · v${plugin.version || "?"}`;
}

function renderStats(data) {
  const stats = data.stats || {};
  const users = data.users || [];
  const avg = (() => {
    const list = users.filter((u) => u.cycle_length).map((u) => u.cycle_length);
    if (!list.length) return "—";
    return `${Math.round(list.reduce((a, b) => a + b, 0) / list.length)} 天`;
  })();
  const pain = (() => {
    const list = users.filter((u) => typeof u.pain === "number").map((u) => u.pain);
    if (!list.length) return "—";
    return `${Math.round((list.reduce((a, b) => a + b, 0) / list.length) * 100)}%`;
  })();
  const single = users.length === 1;
  const cards = [
    { k: "档案", v: stats.users ?? 0 },
    { k: "有记录", v: stats.active ?? 0 },
    { k: single ? "周期长度" : "平均周期", v: avg },
    { k: single ? "当下疼痛" : "平均疼痛", v: pain },
    { k: "记录总数", v: stats.records ?? 0 },
    { k: "不规律", v: stats.irregular ?? 0 },
  ];
  el.stats.innerHTML = cards
    .map(
      (card) =>
        `<div class="stat"><div class="k">${esc(card.k)}</div><div class="v">${esc(
          card.v,
        )}</div></div>`,
    )
    .join("");
}

function renderList(users) {
  const filter = app.filter.trim().toLowerCase();
  const shown = users.filter((user) => {
    if (!filter) return true;
    const hay = `${user.nickname || ""} ${user.user_key} ${user.platform || ""}`.toLowerCase();
    return hay.includes(filter);
  });

  el.usersEmpty.hidden = shown.length > 0 || app.draft === "new";
  if (users.length) {
    el.usersEmpty.textContent = "没有匹配的档案。";
  } else if (isCharacterScope()) {
    el.usersEmpty.textContent =
      "她的周期还没开始。在群里发一句「经期 开始」，或者打开面板点一下刷新，就会替她把这一段接上。";
  } else {
    el.usersEmpty.textContent =
      "还没有任何档案。可以让她在聊天里发一句「经期 开始」，也可以点下面「新建档案」先补一段时间的历史。";
  }

  el.userList.innerHTML = shown
    .map((user) => {
      const phase = phaseOf(user.phase);
      const bits = [];
      if (user.present) {
        bits.push(phase.name);
        bits.push(`第 ${user.cycle_day}/${user.cycle_length} 天`);
        if (typeof user.days_to_next === "number") {
          bits.push(
            user.days_to_next <= 0 ? "就是今天" : `还有 ${user.days_to_next} 天`,
          );
        }
      } else {
        bits.push("还没有周期记录");
      }
      const flags = [];
      if (user.paused) flags.push("观察期");
      if (user.irregular) flags.push("不规律");
      if (user.comforts) flags.push("刚被哄过");
      return `<li class="user-item${
        user.user_key === app.key ? " active" : ""
      }" data-key="${esc(user.user_key)}">
        <span class="avatar">${esc(user.emoji || "🩸")}</span>
        <span class="meta">
          <span class="name">${esc(labelOf(user))}</span>
          <span class="sub">${esc(bits.join(" · "))}</span>
        </span>
        ${
          flags.length
            ? `<span class="flag">${esc(flags.join("·"))}</span>`
            : ""
        }
      </li>`;
    })
    .join("");

  const foot = document.querySelector("#user-foot");
  if (foot) foot.innerHTML = userFooter();
}

/* 周期归她自己（character）还是按聊天对象分开（user） */
function isCharacterScope() {
  const plugin = (app.overview && app.overview.plugin) || {};
  return (plugin.scope || "character") === "character";
}

/* 列表底部的「新建档案」与内联表单；周期归她自己时不需要这个入口 */
function userFooter() {
  if (isCharacterScope()) {
    return "";
  }
  if (app.draft !== "new") {
    return `<button class="btn small ghost full" data-act="new-user">＋ 新建档案</button>`;
  }
  const platforms = (app.overview && app.overview.platforms) || [];
  const options = platforms.length
    ? platforms
        .map(
          (item) =>
            `<option value="${esc(item.id)}">${esc(item.id)}${
              item.type ? `（${esc(item.type)}）` : ""
            }</option>`,
        )
        .join("")
    : `<option value="">（没检测到平台）</option>`;
  return `<div class="list-draft">
    <div class="field"><label for="u-platform">平台</label><select id="u-platform">${options}</select></div>
    <div class="field"><label for="u-sender">她的用户 ID</label><input type="text" id="u-sender" placeholder="例如 123456789" /></div>
    <div class="field"><label for="u-nick">昵称（可不填）</label><input type="text" id="u-nick" maxlength="40" placeholder="小鹿" /></div>
    <div class="field"><label for="u-start">最近一次开始（可不填）</label><input type="date" id="u-start" max="${todayIso()}" /></div>
    <div class="list-draft-acts">
      <button class="btn small primary" data-act="save-user">创建</button>
      <button class="btn small" data-act="cancel-user">取消</button>
    </div>
    <p class="hint">用户 ID 就是她在平台上的号。填了最近的开始日，就能立刻看到她现在第几天。</p>
  </div>`;
}

function showPlaceholder(text) {
  el.detail.innerHTML = `<div class="placeholder"><span class="big">🌙</span><p>${esc(
    text,
  )}</p></div>`;
}

function bar(name, label, value, cls) {
  const v = clamp01(value);
  return `<div class="bar ${cls}">
    <div class="row"><span>${esc(label)}</span><b>${Math.round(v * 100)}%</b></div>
    <div class="meter"><i style="width:${Math.round(v * 100)}%"></i></div>
  </div>`;
}

function renderDetail(data) {
  app.detail = data;
  const user = data.user || {};
  const state = data.state;
  const settings = data.settings || {};
  const stats = data.stats || {};
  const voice = data.voice || {};
  const phase = phaseOf(state && state.phase);

  document.documentElement.style.setProperty("--phase", phase.color);
  document.documentElement.style.setProperty("--phase-soft", phase.soft);

  if (!state) {
    el.detail.innerHTML = `
      <div class="hero">
        <div class="hero-id">
          <span class="hero-emoji">🩸</span>
          <div class="hero-title">
            <h2>${esc(user.nickname || shortKey(user.user_key))}</h2>
            <div class="line">${esc(
              isCharacterScope()
                ? "她自己的周期 · 跟谁聊都是同一个身体"
                : user.user_key,
            )}</div>
          </div>
        </div>
      </div>
      <div class="notices">
        <div class="notice info"><span class="ico">·</span><span>这个档案还没有周期记录，先用下面的按钮记第一天。</span></div>
      </div>
      ${stateBlock(data)}
      ${trendBlock(data)}
      ${injectionBlock(data)}
      ${recordsBlock(data)}
      ${formBlock(settings, user)}
      ${voiceBlock(voice)}
    `;
    return;
  }

  const notices = [];
  if (state.paused) {
    notices.push(
      `<div class="notice"><span class="ico">👀</span><span>观察期到 ${esc(
        settings.paused_until || "—",
      )}，暂时不做精确推算。</span></div>`,
    );
  }
  if (state.irregular) {
    notices.push(
      `<div class="notice"><span class="ico">📉</span><span>最近几次相差得有点多（波动 ${Math.round(
        clamp01(state.variability) * 100,
      )}%），预测只能当范围看。</span></div>`,
    );
  }
  if (state.stale) {
    notices.push(
      `<div class="notice"><span class="ico">🕰️</span><span>已经超过一个周期没记录了，按最后一次推算。</span></div>`,
    );
  }
  if (state.pain_logged !== null && state.pain_logged !== undefined) {
    notices.push(
      `<div class="notice"><span class="ico">🌡️</span><span>她自己报了 ${Math.round(
        clamp01(state.pain_logged) * 10,
      )}/10 的疼，疼痛数值以这个为准。</span></div>`,
    );
  }
  if (state.comforts && state.comforts.length) {
    notices.push(
      `<div class="notice calm"><span class="ico">🫖</span><span>刚被哄过：${esc(
        state.comforts
          .map((item) => `${item.label}，还能缓${span(item.minutes)}`)
          .join("；"),
      )}</span></div>`,
    );
  }

  const predictions = (data.predictions || [])
    .map((item) => `${esc(item.start)} — ${esc(item.end)}`)
    .join("　·　");

  el.detail.innerHTML = `
    <div class="hero">
      <div class="hero-id">
        <span class="hero-emoji">${esc(state.emoji || "🩸")}</span>
        <div class="hero-title">
          <h2>${esc(user.nickname || shortKey(user.user_key))}</h2>
          <div class="line">${esc(state.phase_name)} ｜ 第 ${esc(
            state.cycle_day,
          )}/${esc(state.cycle_length)} 天 ｜ ${esc(state.summary)}</div>
          <div class="line">${esc(
            isCharacterScope()
              ? "她自己的周期 · 跟谁聊都是同一个身体"
              : user.user_key,
          )}</div>
        </div>
      </div>
      <div class="hero-count">
        <div class="num">${
          state.days_to_next <= 0 ? "今天" : state.days_to_next
        }</div>
        <div class="cap">${
          state.days_to_next <= 0 ? "就是预计那天" : "距下次预计（天）"
        }</div>
      </div>
    </div>

    <div class="track-wrap">
      <div class="track"><i style="width:${pct(state.progress)}"></i></div>
      <div class="track-marks">
        <span>今天第 ${esc(state.cycle_day)} 天</span>
        <span>${esc(state.phase_name)}</span>
        <span>下次 ${esc(shortDate(state.next_start))}</span>
      </div>
    </div>

    <div class="bars">
      ${bar("pain", "疼痛", state.pain, "pain")}
      ${bar("mood", "愉悦", (Number(state.valence) + 1) / 2, "mood")}
      ${bar("stress", "压力", state.stress, "stress")}
      ${bar("energy", "精力", state.energy, "")}
    </div>

    ${notices.length ? `<div class="notices">${notices.join("")}</div>` : ""}

    <div class="cols">
      <div class="card">
        <h3>身体</h3>
        <ul>${(state.body || [])
          .map((item) => `<li>${esc(item)}</li>`)
          .join("")}</ul>
      </div>
      <div class="card">
        <h3>此刻的倾向</h3>
        <ul>${(state.behaviors || [])
          .map((item) => `<li>${esc(item)}</li>`)
          .join("")}</ul>
      </div>
      <div class="card">
        <h3>统计</h3>
        <ul>
          <li>记录 ${esc(stats.records ?? 0)} 次，中位周期 ${
            stats.median_cycle ? `${esc(stats.median_cycle)} 天` : "还没算出来"
          }</li>
          <li>经期长度 ${esc(stats.period_length ?? "—")} 天${
            stats.from_history ? "" : "（设置值）"
          }</li>
          <li>最近三次：${predictions || "—"}</li>
          <li>提醒口吻：${
            settings.reminder_style === "in_character" ? "用她的口吻" : "系统提醒"
          }</li>
        </ul>
      </div>
    </div>

    ${stateBlock(data)}
    ${trendBlock(data)}
    ${injectionBlock(data)}
    ${recordsBlock(data)}
    ${formBlock(settings, user)}
    ${voiceBlock(voice)}
  `;
}

/* 编辑行：补录时 item 为空 */
function recordEditor(item) {
  const today = todayIso();
  const id = item ? item.id : "";
  const start = item ? item.start : today;
  const end = item && item.end ? item.end : "";
  const note = item ? item.note || "" : "";
  const length = item && item.length ? `${esc(item.length)} 天` : "按日期算";
  return `<tr class="row-edit">
    <td><input class="cell-input" type="date" id="e-start" max="${today}" value="${esc(start)}" /></td>
    <td><input class="cell-input" type="date" id="e-end" max="${today}" value="${esc(end)}" /></td>
    <td class="muted">${length}</td>
    <td><input class="cell-input" type="text" id="e-note" maxlength="60" placeholder="例如：第二天最疼" value="${esc(note)}" /></td>
    <td class="row-acts">
      <button class="btn small primary" data-act="save-record" data-id="${esc(id)}">保存</button>
      <button class="btn small" data-act="cancel-edit">取消</button>
    </td>
  </tr>`;
}

function recordsBlock(data) {
  const records = data.records || [];
  const actions = `<div class="acts">
      <button class="btn small primary" data-act="start">记今天开始</button>
      <button class="btn small" data-act="end">记今天结束</button>
      <button class="btn small" data-act="new-record">补录一条</button>
    </div>`;
  if (!records.length && app.editing !== "new") {
    return `<div class="table-wrap">
      <div class="table-head"><h3>记录</h3>${actions}</div>
      <p class="empty">还没有记录。</p>
    </div>`;
  }
  const rows = records
    .map((item) => {
      if (app.editing === item.id) return recordEditor(item);
      return `<tr>
        <td>${esc(item.start)}</td>
        <td>${item.open ? `<span class="pill open">进行中</span>` : esc(item.end)}</td>
        <td>${item.length ? `${esc(item.length)} 天` : "—"}</td>
        <td>${esc(item.note || "")}</td>
        <td class="row-acts">
          <button class="btn small" data-act="edit" data-id="${esc(item.id)}">编辑</button>
          <button class="btn small danger" data-act="del" data-id="${esc(
            item.id,
          )}">删除</button>
        </td>
      </tr>`;
    })
    .join("");
  const draft = app.editing === "new" ? recordEditor(null) : "";
  return `<div class="table-wrap">
    <div class="table-head">
      <h3>记录 ${records.length} 条</h3>
      ${actions}
    </div>
    <table>
      <thead><tr><th>开始</th><th>结束</th><th>时长</th><th>备注</th><th class="row-acts"></th></tr></thead>
      <tbody>${rows}${draft}</tbody>
    </table>
  </div>`;
}

/* 直接改当下的状态：自报疼痛、被哄、周期日、观察期、手动情绪 */
function stateBlock(data) {
  const state = data.state || {};
  const settings = data.settings || {};
  const vector = state.vector || {};
  const overrides = (data.overrides && data.overrides.values) || {};
  const logs = data.pain_logs || [];
  const today = todayIso();
  const hit = logs.find((item) => item.date === today);
  const level = hit ? Math.round(hit.level * 10) : "";
  const active = (state.comforts || []).slice(-1)[0];
  const activeKind = active ? active.kind : "";
  const options = (data.comfort_options || [])
    .map(
      (item) =>
        `<option value="${esc(item.kind)}"${
          item.kind === activeKind ? " selected" : ""
        }>${esc(item.label)}</option>`,
    )
    .join("");
  const comforts = (state.comforts || [])
    .map((item) => `${esc(item.label)}（还剩${esc(span(item.minutes))}）`)
    .join("　");
  const recent = logs
    .slice(-5)
    .reverse()
    .map((item) => `${esc(String(item.date).slice(5))} ${Math.round(item.level * 100)}%`)
    .join("　");
  return `<div class="table-wrap">
    <div class="table-head">
      <h3>她的状态</h3>
      <span class="dim">改完立刻生效，也会推给情绪插件</span>
    </div>
    <div class="state-grid">
      <div class="field">
        <label for="s-pain">今天有多疼（0–10）</label>
        <div class="inline">
          <input type="number" id="s-pain" min="0" max="10" step="1" value="${esc(level)}" placeholder="还没记" />
          <button class="btn small primary" data-act="set-pain">记下</button>
          <button class="btn small" data-act="clear-pain">清掉</button>
        </div>
        <p class="hint">${recent ? `最近：${recent}` : "她自报的数字会盖过曲线推算"}</p>
      </div>
      <div class="field">
        <label for="s-comfort">刚被照顾了一次</label>
        <div class="inline">
          <select id="s-comfort">${options}</select>
          <input type="number" id="s-comfort-hours" min="0.5" max="24" step="0.5" placeholder="小时" />
          <button class="btn small primary" data-act="add-comfort">加一次</button>
          <button class="btn small" data-act="clear-comfort">全部清掉</button>
        </div>
        <p class="hint">${comforts ? `正在生效：${comforts}` : "加一条会让疼痛和压力当场往下走"}</p>
      </div>
      <div class="field">
        <label for="s-day">今天算第几天</label>
        <div class="inline">
          <input type="number" id="s-day" min="1" max="${esc(state.cycle_length || 28)}" step="1" value="${esc(
            state.cycle_day || "",
          )}" />
          <button class="btn small primary" data-act="set-day">校正</button>
        </div>
        <p class="hint">只挪最近一次的起始日，不新增记录</p>
      </div>
      <div class="field">
        <label>观察期</label>
        <div class="inline">
          <button class="btn small" data-act="pause-30">暂停 30 天</button>
          <button class="btn small" data-act="resume">解除</button>
        </div>
        <p class="hint">${
          state.paused
            ? `当前观察期到 ${esc(settings.paused_until || "")}`
            : "怀孕、用药、作息乱的时候用"
        }</p>
      </div>
    </div>
    <div class="state-foot">
      <div class="state-head">
        <b>直接钉住情绪数值</b>
        <span class="dim">${
          data.overrides && data.overrides.until
            ? `手动调整到 ${esc(data.overrides.until.replace("T", " ").slice(5, 16))}`
            : "留空就按周期推算，填了就以你为准"
        }</span>
      </div>
      <div class="state-grid">
        ${(data.overrides && data.overrides.fields ? data.overrides.fields : [])
          .map((key) => {
            const pinned = overrides && overrides[key] !== undefined;
            const value = pinned ? overrides[key] : vector[key];
            return `<div class="field">
          <label for="o-${key}">${VECTOR_LABELS[key] || key}${pinned ? "（已钉）" : ""}</label>
          <input type="number" id="o-${key}" min="${key === "valence" ? -1 : 0}" max="1" step="0.05" value="${
            value === undefined || value === null ? "" : Number(value).toFixed(2)
          }" />
        </div>`;
          })
          .join("")}
      </div>
      <div class="inline state-acts">
        <input type="number" id="o-hours" min="0.5" max="168" step="0.5" value="12" />
        <span class="dim">小时内有效</span>
        <button class="btn small primary" data-act="set-override">按这些数值来</button>
        <button class="btn small" data-act="clear-override">恢复推算</button>
      </div>
    </div>
  </div>`;
}

function formBlock(settings, user) {
  const style = settings.reminder_style === "reminder" ? "reminder" : "in_character";
  return `<div class="table-wrap">
    <div class="table-head"><h3>设置</h3></div>
    <div style="padding:14px 14px 4px">
      <div class="form">
        <div class="field">
          <label for="f-name">她的名字</label>
          <input id="f-name" type="text" maxlength="40" placeholder="她" value="${esc(
            (user && user.nickname) || "",
          )}" />
          <span class="hint">留空就叫「她」</span>
        </div>
        <div class="field">
          <label for="f-start">最近一次开始日</label>
          <input id="f-start" type="date" max="${todayIso()}" value="${esc(
            settings.start_date || "",
          )}" />
          <span class="hint">改这里等于整条周期往前/往后挪</span>
        </div>
        <div class="field">
          <label for="f-cycle">周期长度（天）</label>
          <input id="f-cycle" type="number" min="15" max="60" step="1" value="${esc(
            settings.cycle_length,
          )}" />
          <span class="hint">记录够 3 次后会自动按历史算</span>
        </div>
        <div class="field">
          <label for="f-period">经期长度（天）</label>
          <input id="f-period" type="number" min="1" max="12" step="1" value="${esc(
            settings.period_length,
          )}" />
        </div>
        <div class="field">
          <label for="f-pain">疼痛敏感度</label>
          <input id="f-pain" type="number" min="0.2" max="2.5" step="0.1" value="${esc(
            settings.pain_sensitivity,
          )}" />
          <span class="hint">1.0 是标准，往上是更怕疼</span>
        </div>
        <div class="field">
          <label for="f-emotion">情绪幅度</label>
          <input id="f-emotion" type="number" min="0.2" max="2.5" step="0.1" value="${esc(
            settings.emotion_gain,
          )}" />
        </div>
        <div class="field">
          <label for="f-pms">PMS 强度</label>
          <input id="f-pms" type="number" min="0" max="3" step="0.1" value="${esc(
            settings.pms_strength,
          )}" />
          <span class="hint">1.0 是标准，0 等于没有经前期</span>
        </div>
        <div class="field">
          <label for="f-voice">提醒口吻</label>
          <select id="f-voice">
            <option value="in_character" ${
              style === "in_character" ? "selected" : ""
            }>用她的口吻说</option>
            <option value="reminder" ${
              style === "reminder" ? "selected" : ""
            }>系统提醒口吻</option>
          </select>
        </div>
        <div class="field">
          <label for="f-pause">观察期到（可空）</label>
          <input id="f-pause" type="date" value="${esc(settings.paused_until || "")}" />
          <span class="hint">填了就先不做精确推算</span>
        </div>
        <div class="field check">
          <input id="f-remind" type="checkbox" ${
            settings.reminder ? "checked" : ""
          } />
          <label for="f-remind">主动提醒</label>
        </div>
      </div>
      <div class="form-foot">
        <button class="btn primary" data-act="save">保存设置</button>
        <button class="btn danger" data-act="reset">重置档案</button>
        <span class="note">改完立刻生效，下一句回复就用新设置。</span>
      </div>
    </div>
  </div>`;
}

function trendBlock(data) {
  const trend = (data && data.trend) || [];
  if (trend.length < 2) return "";
  const W = 100;
  const H = 34;
  const step = W / (trend.length - 1);
  const line = (key, lo, hi) => {
    const out = [];
    trend.forEach((item, i) => {
      const v = item[key];
      if (typeof v !== "number") return;
      const x = i * step;
      const y = H - ((v - lo) / (hi - lo)) * H;
      out.push(`${x.toFixed(2)},${y.toFixed(2)}`);
    });
    return out.join(" ");
  };
  const painLine = line("pain", 0, 1);
  const moodLine = line("mood", -1, 1);
  if (!painLine && !moodLine) return "";
  const last = trend[trend.length - 1];
  const lastPain = typeof last.pain === "number" ? Math.round(last.pain * 100) : null;
  const days = trend
    .map((item) => `<span>${esc(shortDate(item.date))}</span>`)
    .join("");
  const dots = (key, lo, hi, cls) =>
    trend
      .map((item, i) => {
        const v = item[key];
        if (typeof v !== "number") return "";
        const x = i * step;
        const y = H - ((v - lo) / (hi - lo)) * H;
        return `<line class="${cls}" x1="${x.toFixed(2)}" y1="${y.toFixed(2)}" x2="${x.toFixed(2)}" y2="${y.toFixed(2)}"></line>`;
      })
      .join("");
  return `<div class="table-wrap">
    <div class="table-head">
      <h3>最近 ${trend.length} 天</h3>
      <span class="dim">${lastPain === null ? "红线是疼，绿线是心情" : `今天疼 ${lastPain}%`} · 数值是推算的</span>
    </div>
    <div class="trend">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="最近走势">
        <line class="grid" x1="0" y1="${H / 2}" x2="${W}" y2="${H / 2}"></line>
        ${moodLine ? `<polyline class="mood-line" points="${moodLine}"></polyline>` : ""}
        ${painLine ? `<polyline class="pain-line" points="${painLine}"></polyline>` : ""}
        ${dots("mood", -1, 1, "mood-dot")}
        ${dots("pain", 0, 1, "pain-dot")}
      </svg>
      <div class="trend-legend"><span>疼</span><span>心情</span></div>
      <div class="trend-days">${days}</div>
    </div>
  </div>`;
}

function injectionBlock(data) {
  const text = data.injection || "";
  if (!text) return "";
  return `<div class="table-wrap">
    <div class="table-head">
      <h3>她要看到的那段话</h3>
      <span class="dim">每次回复前拼进系统提示词的原文</span>
    </div>
    <pre class="injection">${esc(text)}</pre>
  </div>`;
}

function voiceBlock(voice) {
  const modeMap = {
    cycle: "每周期写一次",
    realtime: "每次实时生成",
    off: "关闭（内置文案）",
  };
  return `<div class="voice">
    <div class="text">
      <b>这一轮的说法</b>
      <span class="dim">（${esc(modeMap[voice.mode] || voice.mode || "—")}）</span><br />
      ${esc(voice.status || "")}
    </div>
    <div>
      <button class="btn small" data-act="voice">重写这一轮</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ actions */
function readForm() {
  const grab = (id) => document.getElementById(id);
  const payload = {};
  const num = (id) => {
    const node = grab(id);
    if (!node || node.value === "") return undefined;
    const value = Number(node.value);
    return Number.isFinite(value) ? value : undefined;
  };
  const cycle = num("f-cycle");
  const name = grab("f-name");
  if (name) payload.nickname = name.value.trim();
  const start = grab("f-start");
  if (start && start.value) payload.start_date = start.value;
  const period = num("f-period");
  const pain = num("f-pain");
  const emotion = num("f-emotion");
  const pms = num("f-pms");
  if (cycle !== undefined) payload.cycle_length = cycle;
  if (period !== undefined) payload.period_length = period;
  if (pain !== undefined) payload.pain_sensitivity = pain;
  if (emotion !== undefined) payload.emotion_gain = emotion;
  if (pms !== undefined) payload.pms_strength = pms;
  const remind = grab("f-remind");
  if (remind) payload.reminder = Boolean(remind.checked);
  const style = grab("f-voice");
  if (style) payload.reminder_style = style.value;
  const pause = grab("f-pause");
  if (pause) payload.paused_until = pause.value || "";
  return payload;
}

/* 状态改写统一走这里，成功就整个详情重渲染 */
async function postAction(op, payload) {
  const key = app.key;
  if (!key) return;
  return apiPost("action", { key, op, ...payload });
}

async function postState(payload) {
  const key = app.key;
  if (!key) return;
  if (app.busy) return;
  busy(true);
  try {
    const data = await postAction("state", payload);
    if (!data || data.ok === false) {
      throw new Error((data && data.message) || "没有改成");
    }
    toast(data.message || "已更新", "ok");
    if (data.detail) renderDetail({ ...data.detail });
    await refresh();
  } catch (error) {
    toast(errorText(error), "err");
  } finally {
    busy(false);
  }
}

/* 从左侧内联表单建档案 */
async function createUser() {
  const pick = (id) => {
    const node = document.getElementById(id);
    return node ? String(node.value || "").trim() : "";
  };
  const sender = pick("u-sender");
  if (!sender) {
    toast("先填她的用户 ID", "err");
    const node = document.getElementById("u-sender");
    if (node) node.focus();
    return;
  }
  busy(true);
  try {
    const data = await apiPost("user", {
      platform: pick("u-platform"),
      sender,
      nickname: pick("u-nick"),
      start: pick("u-start"),
    });
    if (!data || data.ok === false) {
      throw new Error((data && data.message) || "没有建成");
    }
    app.draft = null;
    toast(data.message || "已创建", "ok");
    const created = (data.detail && data.detail.user && data.detail.user.user_key) || "";
    await refresh();
    if (created) await openUser(created, { silent: true });
  } catch (error) {
    toast(errorText(error), "err");
  } finally {
    busy(false);
  }
}

async function act(name, node) {
  /* 新建档案要在「没选中任何人」时也能点，所以放在 key 检查之前 */
  if (name === "new-user" || name === "cancel-user" || name === "save-user") {
    if (app.busy) return;
    if (name === "save-user") {
      await createUser();
      return;
    }
    app.draft = name === "new-user" ? "new" : null;
    renderList((app.overview && app.overview.users) || []);
    if (app.draft === "new") {
      const first = document.querySelector("#u-sender");
      if (first) first.focus();
    }
    return;
  }

  const key = app.key;
  if (!key) return;
  if (app.busy) return;

  if (name === "start" || name === "end") {
    busy(true);
    try {
      const data = await postAction("record", { action: name });
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有成功");
      }
      toast(data.message || "已记录", "ok");
      await refresh();
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      busy(false);
    }
    return;
  }

  if (name === "del") {
    const id = node.dataset.id || "";
    const now = Date.now();
    if (app.confirmId !== id || now - app.confirmAt > 4000) {
      app.confirmId = id;
      app.confirmAt = now;
      node.textContent = "再点一次";
      setTimeout(() => {
        if (document.body.contains(node)) node.textContent = "删除";
      }, 4000);
      return;
    }
    app.confirmId = "";
    busy(true);
    try {
      const data = await postAction("record", { action: "delete", id });
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有成功");
      }
      toast(data.message || "已删除", "ok");
      await refresh();
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      busy(false);
    }
    return;
  }

  if (name === "edit") {
    app.editing = node.dataset.id || null;
    if (app.detail) renderDetail({ ...app.detail });
    return;
  }

  if (name === "new-record") {
    app.editing = "new";
    if (app.detail) renderDetail({ ...app.detail });
    return;
  }

  if (name === "cancel-edit") {
    app.editing = null;
    if (app.detail) renderDetail({ ...app.detail });
    return;
  }

  if (name === "save-record") {
    const id = node.dataset.id || "";
    const read = (sel) => {
      const input = document.querySelector(sel);
      return input ? input.value.trim() : "";
    };
    const payload = {
      action: id ? "update" : "create",
      start: read("#e-start"),
      end: read("#e-end"),
      note: read("#e-note"),
    };
    if (id) payload.id = id;
    if (!payload.start) {
      toast("先选开始日", "err");
      return;
    }
    busy(true);
    try {
      const data = await postAction("record", payload);
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有保存");
      }
      app.editing = null;
      toast(data.message || "已保存", "ok");
      if (data.detail) renderDetail({ ...data.detail });
      await refresh();
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      busy(false);
    }
    return;
  }

  if (name === "set-pain" || name === "clear-pain") {
    const input = document.querySelector("#s-pain");
    const payload = {
      pain: name === "clear-pain" ? "" : input ? input.value : "",
    };
    if (name === "set-pain" && (payload.pain === "" || payload.pain === null)) {
      toast("先填 0～10 的数字", "err");
      return;
    }
    await postState(payload);
    return;
  }

  if (name === "add-comfort" || name === "clear-comfort") {
    const select = document.querySelector("#s-comfort");
    const hours = document.querySelector("#s-comfort-hours");
    const payload =
      name === "clear-comfort"
        ? { comfort: "" }
        : {
            comfort: select ? select.value : "",
            comfort_hours: hours && hours.value ? hours.value : "",
          };
    await postState(payload);
    return;
  }

  if (name === "set-day") {
    const input = document.querySelector("#s-day");
    if (!input || !input.value) {
      toast("先填第几天", "err");
      return;
    }
    await postState({ cycle_day: input.value });
    return;
  }

  if (name === "set-override") {
    const fields = (app.detail && app.detail.overrides && app.detail.overrides.fields) || [];
    const overrides = {};
    let touched = 0;
    for (const key of fields) {
      const input = document.querySelector(`#o-${key}`);
      if (!input) continue;
      const raw = input.value.trim();
      const current = app.detail.state && app.detail.state.vector ? app.detail.state.vector[key] : undefined;
      const parsed = raw === "" ? NaN : Number(raw);
      if (!Number.isFinite(parsed)) continue;
      if (current !== undefined && Math.abs(parsed - Number(current)) < 0.001) continue;
      overrides[key] = parsed;
      touched += 1;
    }
    if (!touched) {
      toast("没有改动：只把想覆盖的那几项改掉就行", "err");
      return;
    }
    const hours = document.querySelector("#o-hours");
    await postState({
      overrides,
      override_hours: hours && hours.value ? Number(hours.value) : 12,
    });
    return;
  }

  if (name === "clear-override") {
    await postState({ overrides: "" });
    return;
  }

  if (name === "pause-30") {
    await postState({ pause: 30 });
    return;
  }

  if (name === "resume") {
    await postState({ pause: "" });
    return;
  }

  if (name === "save") {
    const payload = readForm();
    busy(true);
    try {
      const data = await postAction("settings", payload);
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有保存");
      }
      toast(data.message || "已保存", "ok");
      if (data.detail) renderDetail({ ...data.detail });
      await refresh();
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      busy(false);
    }
    return;
  }

  if (name === "reset") {
    const now = Date.now();
    if (app.confirmId !== "reset" || now - app.confirmAt > 4000) {
      app.confirmId = "reset";
      app.confirmAt = now;
      node.textContent = "再点一次清空";
      setTimeout(() => {
        if (document.body.contains(node)) node.textContent = "重置档案";
      }, 4000);
      return;
    }
    app.confirmId = "";
    busy(true);
    try {
      const data = await postAction("reset", {});
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有成功");
      }
      toast("档案已清空", "ok");
      app.key = null;
      showPlaceholder("这个档案已经清空了。");
      await refresh();
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      busy(false);
    }
    return;
  }

  if (name === "voice") {
    busy(true);
    node.disabled = true;
    try {
      const data = await postAction("voice", { action: "write" });
      if (!data || data.ok === false) {
        throw new Error((data && data.message) || "没有写成");
      }
      toast(data.message || "已重写", "ok");
      if (data.detail) renderDetail({ ...data.detail });
    } catch (error) {
      toast(errorText(error), "err");
    } finally {
      node.disabled = false;
      busy(false);
    }
  }
}

/* ------------------------------------------------------------------ events */
function bindEvents() {
  el.refresh.addEventListener("click", () => refresh());

  el.search.addEventListener("input", () => {
    app.filter = el.search.value || "";
    renderList((app.overview && app.overview.users) || []);
  });

  el.userList.addEventListener("click", (event) => {
    const item = event.target.closest(".user-item");
    if (!item) return;
    const key = item.dataset.key;
    if (key && key !== app.key) openUser(key);
  });

  const foot = document.querySelector("#user-foot");
  if (foot) {
    foot.addEventListener("click", (event) => {
      const button = event.target.closest("[data-act]");
      if (!button) return;
      event.stopPropagation();
      act(button.dataset.act, button);
    });
  }

  el.detail.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    act(button.dataset.act, button);
  });
}

async function boot() {
  const bridge = await waitBridge();
  if (!bridge) {
    el.subtitle.textContent = "页面桥接没加载出来，刷新一下试试";
    showPlaceholder("没找到 AstrBot 的页面桥接脚本。");
    return;
  }
  app.bridge = bridge;
  try {
    const context = await bridge.ready();
    applyTheme(context);
    if (bridge.onContext) bridge.onContext(applyTheme);
  } catch {
    /* 上下文拿不到也能用 */
  }
  bindEvents();
  el.subtitle.textContent = "正在读取…";
  await refresh();
}

boot();
