// popup.js — 單一視窗,自動分支:命中時 autofill,未命中時 fallback 用 cascading 群組選單

const $conn = document.getElementById("conn");
const $currentUrl = document.getElementById("current-url");
const $matches = document.getElementById("matches");
const $empty = document.getElementById("empty");
const $status = document.getElementById("status");
const $launchSearch = document.getElementById("launch-search");
const $groupMenu = document.getElementById("group-menu");
const $groupList = document.getElementById("group-list");
const $navigateToggle = document.getElementById("navigate-toggle");

let currentTabId = null;
let currentUrl = null;
let allLaunches = []; // fallback 模式快取,搜尋時即時過濾
const NO_GROUP = "__none__"; // 空群組的內部 bucket key(只用於排序,不對外顯示)

// 每個 group 一個固定色票(hash group 名稱決定),label 背景 + 條目左邊界用對應色
// 淺色背景搭配深一點的 accent,保持文字可讀
const GROUP_PALETTE = [
  { bg: "#eaf4ff", accent: "#0078d4" }, // blue
  { bg: "#e8f7ed", accent: "#2e8b3d" }, // green
  { bg: "#f1edff", accent: "#6c4dde" }, // purple
  { bg: "#fff1e6", accent: "#d2691e" }, // orange
  { bg: "#ffecf1", accent: "#d6336c" }, // pink
  { bg: "#e6f7f7", accent: "#148080" }, // teal
  { bg: "#fff8e1", accent: "#b8860b" }, // yellow
  { bg: "#ffeaea", accent: "#c33"    }, // red
];
const NEUTRAL_COLOR = { bg: "#f3f3f3", accent: "#888" }; // 未分類用中性灰

function groupColorIndex(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  }
  return Math.abs(h) % GROUP_PALETTE.length;
}

async function init() {
  // 1. 連線檢查
  const pingResp = await chrome.runtime.sendMessage({ type: "ping" });
  if (pingResp && pingResp.ok) {
    $conn.textContent = "已連線";
    $conn.classList.add("ok");
  } else {
    $conn.textContent = "未連線";
    $conn.classList.add("bad");
    showEmpty("找不到原生主機。請確認 python install.py 已執行且擴充 ID 已註冊。");
    return;
  }

  // 2. 目前 tab + URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    showEmpty("找不到目前分頁");
    return;
  }
  currentTabId = tab.id;
  currentUrl = tab.url || "";
  $currentUrl.textContent = currentUrl || "(沒有 URL)";

  // 3. 讀 navigate toggle(預設 ON)
  const { navigate_enabled = true } = await chrome.storage.sync.get({
    navigate_enabled: true,
  });
  $navigateToggle.checked = navigate_enabled;
  $navigateToggle.addEventListener("change", () => {
    chrome.storage.sync.set({ navigate_enabled: $navigateToggle.checked });
  });

  // 4. 嘗試取得命中條目
  const qResp = await chrome.runtime.sendMessage({
    type: "queryFresh",
    url: currentUrl,
    tabId: currentTabId,
  });
  const matches = (qResp && qResp.ok && qResp.matches) || [];
  if (matches.length > 0) {
    setStatus(`命中 ${matches.length} 筆,點擊自動填入`);
    renderAutofillList(matches);
    return;
  }

  // 5. 未命中 → toggle OFF 時不列 fallback,只顯示空狀態
  if (!navigate_enabled) {
    showEmpty("目前網頁沒有符合的條目。請到 PWmgr GUI 新增條目後再開啟此頁。");
    return;
  }

  // 6. 無命中 + toggle ON → fallback:列全部有 launch_url 的條目
  setStatus("未命中,以下為所有可開啟的條目");
  $launchSearch.hidden = false;
  const lResp = await chrome.runtime.sendMessage({ type: "getAllEntries" });
  if (!lResp || !lResp.ok) {
    showEmpty(
      `無法取得條目:${(lResp && (lResp.code || lResp.error)) || "unknown"}`
    );
    return;
  }
  allLaunches = (lResp.entries || []).filter((e) => e.launch_url);
  if (allLaunches.length === 0) {
    showEmpty(
      "目前網頁沒有符合的條目,且資料庫裡沒有任何啟動網址。請到 PWmgr GUI 新增條目時填寫「啟動網址」欄位。"
    );
    return;
  }
  $launchSearch.value = "";
  $launchSearch.addEventListener("input", renderLaunchList);

  // Cascading 群組選單:fallback 模式用,hover 展開該群組條目
  $groupMenu.hidden = false;
  renderLaunchList();
}

function renderAutofillList(matches) {
  $matches.innerHTML = "";
  $matches.hidden = false;
  $empty.hidden = true;
  for (const m of matches) {
    const li = document.createElement("li");
    li.dataset.id = m.id;

    const label = document.createElement("div");
    label.className = "entry-label";
    label.textContent = m.label;

    const meta = document.createElement("div");
    meta.className = "entry-meta";
    const u = document.createElement("span");
    u.textContent = m.username;
    const d = document.createElement("span");
    d.textContent = m.url;
    meta.appendChild(u);
    meta.appendChild(d);

    li.appendChild(label);
    li.appendChild(meta);
    li.addEventListener("click", () => fill(m.id, li));
    $matches.appendChild(li);
  }
}

function renderLaunchList() {
  const keyword = $launchSearch.value.trim().toLowerCase();
  // 先依搜尋過濾(若有)
  let shown = allLaunches;
  if (keyword) {
    shown = shown.filter(
      (e) =>
        (e.label || "").toLowerCase().includes(keyword) ||
        (e.launch_url || "").toLowerCase().includes(keyword) ||
        (e.url || "").toLowerCase().includes(keyword) ||
        (e.group || "").toLowerCase().includes(keyword)
    );
  }

  if (shown.length === 0) {
    renderGroupMenu(shown, keyword);
    if (keyword) {
      showEmpty(`沒有符合「${$launchSearch.value}」的條目`);
    } else {
      showEmpty("沒有可開啟的條目");
    }
    return;
  }

  $empty.hidden = true;
  renderGroupMenu(shown, keyword);
}

function renderGroupMenu(entries, keyword) {
  // 清空舊 DOM
  $groupList.replaceChildren();
  $groupMenu.hidden = entries.length === 0;
  if (entries.length === 0) return;

  // 依 allLaunches 第一次出現順序分組
  const order = [];
  const buckets = new Map();
  for (const e of entries) {
    const key = (e.group || "").trim() || NO_GROUP;
    if (!buckets.has(key)) {
      buckets.set(key, []);
      order.push(key);
    }
    buckets.get(key).push(e);
  }

  const isSearching = !!keyword;
  for (const key of order) {
    const items = buckets.get(key);
    const li = document.createElement("li");
    li.className = "group-item";
    li.dataset.group = key;

    // 依 group 名稱 hash 決定色票(同名稱永遠同一顏色)
    const color =
      key === NO_GROUP
        ? NEUTRAL_COLOR
        : GROUP_PALETTE[groupColorIndex(key)];
    li.style.setProperty("--group-bg", color.bg);
    li.style.setProperty("--group-accent", color.accent);

    const label = document.createElement("div");
    label.className = "group-label";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = key === NO_GROUP ? "未分類" : key;
    const countSpan = document.createElement("span");
    countSpan.className = "count";
    countSpan.textContent = `${items.length}`;
    label.appendChild(nameSpan);
    label.appendChild(countSpan);
    li.appendChild(label);

    const ul = document.createElement("ul");
    ul.className = "entry-list";
    for (const e of items) {
      const entryLi = document.createElement("li");
      entryLi.dataset.id = e.id;
      entryLi.textContent = e.label;
      entryLi.addEventListener("click", (ev) => {
        ev.stopPropagation();
        launchAndFill(e.launch_url, e.id);
      });
      ul.appendChild(entryLi);
    }
    li.appendChild(ul);

    // hover 展開 / 離開收合(搜尋時永遠展開)
    li.addEventListener("mouseenter", () => li.classList.add("is-open"));
    li.addEventListener("mouseleave", () => {
      if (!isSearching) li.classList.remove("is-open");
    });
    // 觸控裝置 fallback:點 label toggle
    label.addEventListener("click", (ev) => {
      ev.stopPropagation();
      li.classList.toggle("is-open");
    });

    if (isSearching) li.classList.add("is-open");
    $groupList.appendChild(li);
  }
}

async function fill(id, li) {
  setStatus("填入中…");
  li.style.opacity = "0.5";
  const resp = await chrome.runtime.sendMessage({
    type: "fetch",
    id,
    tabId: currentTabId,
  });
  li.style.opacity = "1";
  if (resp && resp.ok) {
    setStatus("已填入;20 秒後自動清空剪貼簿(GUI 端)");
    setTimeout(() => window.close(), 1500);
  } else if (resp && resp.code === "BUSY") {
    setStatus("密碼管理員忙碌中,稍後再試");
  } else {
    setStatus(`失敗: ${(resp && (resp.code || resp.error)) || "unknown"}`);
  }
}

async function openLaunch(url) {
  setStatus("開啟中…");
  const resp = await chrome.runtime.sendMessage({ type: "openLaunch", url });
  if (resp && resp.ok) {
    setStatus("已在新分頁開啟");
    setTimeout(() => window.close(), 800);
  } else {
    setStatus(`開啟失敗: ${(resp && (resp.code || resp.error)) || "unknown"}`);
  }
}

async function launchAndFill(url, id) {
  setStatus("開啟中…");
  let resp;
  try {
    resp = await chrome.runtime.sendMessage({ type: "launchAndFill", url, id });
  } catch (e) {
    console.warn("[pwmgr] launchAndFill sendMessage threw", e);
    setStatus(`開啟失敗: sendMessage 例外 ${e && e.message || e}`);
    return;
  }
  console.log("[pwmgr] launchAndFill resp", resp);
  if (resp && resp.ok) {
    setStatus("已在新分頁開啟,等待頁面 render 後自動填入");
    setTimeout(() => window.close(), 800);
  } else {
    setStatus(`開啟失敗: ${(resp && (resp.code || resp.error)) || "unknown"}`);
  }
}

function showEmpty(text) {
  $matches.innerHTML = "";
  $matches.hidden = true;
  $launchSearch.hidden = true;
  $empty.textContent = text;
  $empty.hidden = false;
}

function setStatus(text) {
  $status.textContent = text;
}

init();
