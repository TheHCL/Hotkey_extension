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
const $captchaBtn = document.getElementById("captcha-btn");
const $otpBtn = document.getElementById("otp-btn");

// captcha 按鈕的 click handler 改在這裡就綁一次,不再放進 async detectCaptchaOnTab
// 的條件分支內。原因:
//   - async + if 內 addEventListener 容易在 popup 被 Chrome 提前關閉/重開的時機
//     漏綁(這就是先前「按鈕可見但 click 不觸發」的根因)
//   - 顯示/隱藏交給 $captchaBtn.hidden 控制,handler 本身永遠在
$captchaBtn.addEventListener("click", onCaptchaClick);
$otpBtn.addEventListener("click", onOtpClick);

// OTP polling 設定
const OTP_POLL_SECONDS = 15; // 按按鈕後最多等 15 秒
const OTP_POLL_TICK_MS = 1000; // 倒數 UI 更新頻率

let currentTabId = null;
let currentUrl = null;
let allLaunches = []; // fallback 模式快取,搜尋時即時過濾
let groupColors = {}; // {group_name: "#rrggbb"}——使用者透過 PWmgr GUI 設定的覆寫
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

// 派生 {bg, accent}:把使用者自訂的單一 hex 展開成 CSS 用的兩個變數。
//  - accent = hex 本身(給 .entry-list border-left 用)
//  - bg     = 與白色混 85%(給 .group-label background 用,確保文字可讀)
function hexToBgAccent(hex) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return null;
  const r = parseInt(m[1], 16);
  const g = parseInt(m[2], 16);
  const b = parseInt(m[3], 16);
  // component-wise mix(hex, #ffffff, t=0.85)
  const mix = (c) => Math.round(c + (255 - c) * 0.85).toString(16).padStart(2, "0");
  return {
    accent: hex.toLowerCase(),
    bg: `#${mix(r)}${mix(g)}${mix(b)}`,
  };
}

// 統一決定某個 group 該用什麼色。優先序:
//   1. 未分類(NO_GROUP) → NEUTRAL_COLOR(系統固定)
//   2. 使用者已在 GUI 自訂(groupColorsMap 有) → 派生自該 hex
//   3. fallback → GROUP_PALette[hash(name)]
function resolveGroupColor(key, groupColorsMap) {
  if (key === NO_GROUP) return NEUTRAL_COLOR;
  const override = groupColorsMap && groupColorsMap[key];
  if (override) {
    const derived = hexToBgAccent(override);
    if (derived) return derived;
  }
  return GROUP_PALETTE[groupColorIndex(key)];
}

async function pingWithRetry(maxAttempts, perAttemptTimeoutMs) {
  // Chrome 會 idle-kill service worker (~30s 沒活動),下次開 popup → SW cold start
  // → native host Python 也要冷啟動(import pywin32/keyring 等可達 2-5s)。
  // chrome.runtime.sendMessage 本身沒有 timeout,但 background 內部 sendNative 有
  // 15s timeout — 第一次 ping 失敗時 background 會 reconnect + retry 一次,
  // 這裡再加一個 8s 等候避免 popup 跟著 cold start 卡死。
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    let timer = null;
    try {
      const resp = await Promise.race([
        chrome.runtime.sendMessage({ type: "ping" }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("ping no-response")), perAttemptTimeoutMs);
        }),
      ]);
      if (timer) clearTimeout(timer);
      if (resp && resp.ok) return resp;
    } catch (e) {
      if (timer) clearTimeout(timer);
      // 等一下再試,給 SW / native host 多一點暖機時間
      if (attempt < maxAttempts) {
        await new Promise((r) => setTimeout(r, 600));
      }
    }
  }
  return null;
}

async function init() {
  // 1. 連線檢查(背景 SW cold start + native host Python cold start 可能數秒)
  const pingResp = await pingWithRetry(/* maxAttempts */ 2, /* perAttemptTimeoutMs */ 8000);
  if (pingResp && pingResp.ok) {
    $conn.textContent = "已連線";
    $conn.classList.add("ok");
  } else {
    $conn.textContent = "未連線";
    $conn.classList.add("bad");
    showEmpty(
      "找不到原生主機。請確認 PWmgr 已啟動且常駐,或重新開啟 extension(service worker 可能剛被 idle-kill)。"
    );
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
  // queryFresh 回傳的 group_colors 是最新的覆寫——
  // 即使這次命中切到 autofill 路徑,先把色票存起來,
  // 之後若 toggle 切到 fallback 也拿得到。
  if (qResp && qResp.group_colors) groupColors = qResp.group_colors;
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
  if (lResp.group_colors) groupColors = lResp.group_colors;
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

  // 7. captcha 按鈕:目前頁面有偵測到 captcha 才顯示
  detectCaptchaOnTab();
  // 8. OTP 按鈕:目前頁面有偵測到 otpBox 多格輸入才顯示(Dell 風格)
  detectOtpOnTab();
}

async function detectCaptchaOnTab() {
  if (!currentTabId) return;
  try {
    const resp = await chrome.tabs.sendMessage(currentTabId, { type: "detectCaptcha" });
    // click handler 已在 script 載入時就綁好,這裡只 toggle 顯示
    $captchaBtn.hidden = !(resp && resp.ok && resp.found);
    console.log("[pwmgr][popup] detectCaptcha:", resp);
  } catch (e) {
    // 沒有 content script 注入(非 http(s) 或 SPA 還沒載入)就當沒有
    $captchaBtn.hidden = true;
    console.log("[pwmgr][popup] detectCaptcha threw:", e && e.message || e);
  }
}

async function onCaptchaClick() {
  console.log("[pwmgr][popup] onCaptchaClick start, currentTabId=", currentTabId);
  $captchaBtn.disabled = true;
  const orig = $captchaBtn.textContent;
  $captchaBtn.textContent = "解碼中…";
  setStatus("送出 captcha 圖給 native host 解碼…");
  try {
    const resp = await chrome.tabs.sendMessage(currentTabId, { type: "solveCaptcha" });
    console.log("[pwmgr][popup] solveCaptcha resp:", resp);
    if (resp && resp.ok) {
      setStatus(`已填入:${resp.text} (${resp.confidence === "high" ? "高信心" : "低信心"})`);
    } else if (resp && resp.code === "LOW_CONFIDENCE") {
      setStatus(`無法辨識(std:${resp.std || "?"} / beta:${resp.beta || "?"}),請手動輸入`);
    } else if (resp && resp.code === "OCR_UNAVAILABLE") {
      setStatus(`ddddocr 未安裝,請 pip install ddddocr`);
    } else if (resp && resp.code === "NOT_FOUND") {
      setStatus("找不到 captcha 圖,重新整理頁面後再試");
    } else if (resp && resp.code === "NO_INPUT") {
      setStatus("找到 captcha 圖但找不到對應的輸入框");
    } else {
      setStatus(`失敗:${(resp && (resp.code || resp.error)) || "unknown"}`);
    }
  } catch (e) {
    console.warn("[pwmgr][popup] onCaptchaClick threw:", e);
    setStatus(`例外:${(e && e.message) || e}`);
  } finally {
    $captchaBtn.disabled = false;
    $captchaBtn.textContent = orig;
  }
}

// ===== OTP 自動填 =============================================================
//
// popup 流程:
//   1. user 在 OTP 頁打開 extension → detectOtpOnTab 問 content script
//      「有沒有 otpBox 多格輸入?」→ 有就顯示按鈕
//   2. user 按按鈕 → onOtpClick 先問 background 要 OTP code
//   3. background → native host → on-demand 直接查 Outlook(沒有背景常駐監聽 /
//      沒有 cache,按按鈕當下才查)
//   4. 拿到 code 後請 background 轉發給 content script → 填入
//
// 兩個獨立 round trip 是刻意的:不要把 native host 的 token 暴露給 content script,
// 也讓 native host 的回應格式對 popup/background 統一。
async function detectOtpOnTab() {
  if (!currentTabId) return;
  try {
    const resp = await chrome.tabs.sendMessage(currentTabId, { type: "detectOtp" });
    // 跟 captcha 一樣:handler 永遠在,只 toggle 顯示
    $otpBtn.hidden = !(resp && resp.ok && resp.found);
    console.log("[pwmgr][popup] detectOtp:", resp);
  } catch (e) {
    // 沒有 content script(非 http(s) 頁 / SPA 還沒載入)
    $otpBtn.hidden = true;
    console.log("[pwmgr][popup] detectOtp threw:", e && e.message || e);
  }
}

async function onOtpClick() {
  console.log("[pwmgr][popup] onOtpClick start, currentTabId=", currentTabId);
  $otpBtn.disabled = true;
  const orig = $otpBtn.textContent;
  // 倒數 UI:每 1 秒更新按鈕文字(例「取得中… (14s)」)
  let remaining = OTP_POLL_SECONDS;
  $otpBtn.textContent = `取得中… (${remaining}s)`;
  const tickId = setInterval(() => {
    remaining -= 1;
    if (remaining < 0) remaining = 0;
    $otpBtn.textContent = `取得中… (${remaining}s)`;
  }, OTP_POLL_TICK_MS);
  try {
    // 1. 跟 native host 要 code(pollSeconds 讓 native host 在這段時間內輪詢)
    const otpResp = await chrome.runtime.sendMessage({
      type: "getOtp",
      pollSeconds: OTP_POLL_SECONDS,
    });
    console.log("[pwmgr][popup] getOtp resp:", otpResp);
    if (!otpResp || !otpResp.ok) {
      // 常見錯誤:
      //   NO_CODE → outlook 還沒收到信 / 範圍內沒符合
      //   OUTLOOK_UNAVAILABLE → pywin32 沒裝 / Outlook 沒在跑
      //   PYWIN32_MISSING → requirements 漏裝
      //   OTP_FOLDER_NOT_FOUND → GUI 設的 folder 路徑錯了
      const code = (otpResp && otpResp.code) || "EMPTY";
      const err = (otpResp && otpResp.error) || "";
      if (code === "NO_CODE") {
        const attempts = otpResp.attempts || 1;
        setStatus(`OTP ${OTP_POLL_SECONDS}s 內沒找到(輪 ${attempts} 次),請到 PWmgr GUI「OTP 設定」確認 folder 是否正確`);
      } else if (code === "OTP_FOLDER_NOT_FOUND") {
        setStatus(`OTP folder 設錯了:${err} — 請到 PWmgr GUI「OTP 設定」重設`);
      } else if (code === "OUTLOOK_UNAVAILABLE" || code === "PYWIN32_MISSING") {
        setStatus(`Outlook 無法使用(${err}),請確認 Outlook 已開 + PWmgr 常駐`);
      } else if (code === "OTP_DISABLED") {
        setStatus("OTP 自動填入已停用,請到 PWmgr GUI「OTP 設定」啟用");
      } else if (code === "EXCEPTION") {
        setStatus(`background 例外:${err}(看 chrome://extensions > service worker console)`);
      } else if (code === "EMPTY") {
        setStatus("native host 沒回應,請看 chrome://extensions console log");
      } else {
        setStatus(`取得失敗:${code} ${err}`);
      }
      return;
    }
    const code = String(otpResp.code || "");
    if (!code) {
      setStatus("native host 回傳空 code,請手動輸入");
      return;
    }

    // 2. 把 code 轉發給 content script 填入
    const fillResp = await chrome.runtime.sendMessage({
      type: "fillOtp",
      tabId: currentTabId,
      code,
    });
    console.log("[pwmgr][popup] fillOtp resp:", fillResp);
    if (fillResp && fillResp.ok) {
      const ageStr =
        typeof otpResp.received_at === "number"
          ? `(${Math.max(0, Math.round(Date.now() / 1000 - otpResp.received_at))}秒前收到)`
          : "";
      const srcStr = otpResp.source === "cache" ? "cache" : "live";
      const folderStr = otpResp.folder ? ` [${otpResp.folder}]` : "";
      setStatus(`已填入 ${code}${ageStr} [${srcStr}]${folderStr} — 按 Enter 送出`);
    } else if (fillResp && fillResp.code === "NOT_FOUND") {
      setStatus("頁面已變動,找不到 OTP 輸入框(請重新整理)");
    } else if (fillResp && fillResp.code === "FILL_NOOP") {
      setStatus("OTP 框已存在值,未覆寫(可能已被 user 填過)");
    } else if (fillResp && fillResp.code === "FORWARD_FAIL") {
      setStatus("無法把 code 送到頁面(content script 未就緒)");
    } else {
      setStatus(`填入失敗:${(fillResp && (fillResp.code || fillResp.error)) || "unknown"}`);
    }
  } catch (e) {
    console.warn("[pwmgr][popup] onOtpClick threw:", e);
    setStatus(`例外:${(e && e.message) || e}`);
  } finally {
    clearInterval(tickId);
    $otpBtn.disabled = false;
    $otpBtn.textContent = orig;
  }
}

// --- OTP folder picker 已搬到 PWmgr GUI 的「OTP 設定」dialog ---
// (popup.js 不再做 picker UI。background.js 的 listOtpFolders / setOtpFolder
//  handler 也不再需要,但保留 dispatch entry 避免 native host 端 dispatch table
//  對不上 — 兩端都標 deprecate,後續版本可移除。)

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

    // 解析該 group 的色票:優先用 GUI 自訂的覆寫,fallback 到 hash palette
    const color = resolveGroupColor(key, groupColors);
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
    // 若頁面有偵測到 captcha,多等 1.5s 讓 content script 完成自動解碼再看結果
    const hasCaptcha = !$captchaBtn.hidden;
    if (hasCaptcha) {
      setStatus("已填入;正在自動解 captcha…");
      setTimeout(() => window.close(), 2800);
    } else {
      setStatus("已填入;20 秒後自動清空剪貼簿(GUI 端)");
      setTimeout(() => window.close(), 1500);
    }
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
