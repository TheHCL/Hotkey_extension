// popup.js — 顯示目前 tab 命中條目,點擊觸發自動填入

const $conn = document.getElementById("conn");
const $currentUrl = document.getElementById("current-url");
const $matches = document.getElementById("matches");
const $empty = document.getElementById("empty");
const $noHost = document.getElementById("no-host");
const $status = document.getElementById("status");

let currentTabId = null;
let currentUrl = null;

async function init() {
  // 1. 確認原生主機連線
  const pingResp = await chrome.runtime.sendMessage({ type: "ping" });
  if (pingResp && pingResp.ok) {
    $conn.textContent = "已連線";
    $conn.classList.add("ok");
  } else {
    $conn.textContent = "未連線";
    $conn.classList.add("bad");
    $noHost.hidden = false;
    return;
  }

  // 2. 拿目前 tab 與 URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    $status.textContent = "找不到目前分頁";
    return;
  }
  currentTabId = tab.id;
  currentUrl = tab.url || "";

  $currentUrl.textContent = currentUrl || "(沒有 URL)";

  // 3. 拿命中條目——即時查詢,不用快取(避免 PWmgr GUI 剛新增/修改的條目沒反映出來)
  const resp = await chrome.runtime.sendMessage({ type: "queryFresh", url: currentUrl, tabId: currentTabId });
  const matches = (resp && resp.ok && resp.matches) || [];
  if (matches.length === 0) {
    $empty.hidden = false;
    return;
  }
  renderMatches(matches);
}

function renderMatches(matches) {
  $matches.hidden = false;
  $matches.innerHTML = "";
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

async function fill(id, li) {
  $status.textContent = "填入中…";
  li.style.opacity = "0.5";
  const resp = await chrome.runtime.sendMessage({ type: "fetch", id, tabId: currentTabId });
  li.style.opacity = "1";
  if (resp && resp.ok) {
    $status.textContent = "已填入;20 秒後自動清空剪貼簿(GUI 端)";
    // 1.5 秒後關 popup
    setTimeout(() => window.close(), 1500);
  } else if (resp && resp.code === "BUSY") {
    $status.textContent = "密碼管理員忙碌中,稍後再試";
  } else {
    $status.textContent = `失敗: ${(resp && (resp.code || resp.error)) || "unknown"}`;
  }
}

init();
