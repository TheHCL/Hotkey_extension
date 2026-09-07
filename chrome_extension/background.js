// PWmgr Companion — service worker (MV3)
// 職責:
//   1. 與原生主機 (com.thehcl.pwmgr) 保持長連線
//   2. tab URL 變動 → query + report_url
//   3. 收到 query 命中 → 設 badge;popup 點選 → fetch + 注入 content script 填表

const HOST_NAME = "com.thehcl.pwmgr";
const BADGE_COLOR = "#0078d4";
const PENDING_FILL_TTL_MS = 30000;

let nativePort = null;
let lastTabUrl = null;   // 上次主動查詢的 URL(避免重複 query)
let currentMatches = {}; // tabId -> [{id, label, username, url}]
let pendingFill = {};    // tabId -> { username, password, url, timer }

// --- 原生主機連線管理 -------------------------------------------------------

function connectNative() {
  if (nativePort) {
    try { nativePort.disconnect(); } catch (_) {}
    nativePort = null;
  }
  try {
    nativePort = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    console.warn("[pwmgr] connectNative failed:", e);
    nativePort = null;
    return;
  }
  nativePort.onMessage.addListener(onNativeMessage);
  nativePort.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    console.warn("[pwmgr] native host disconnected:", err && err.message);
    nativePort = null;
    // 5 秒後重連
    setTimeout(connectNative, 5000);
  });
}

// --- launchAndFill 暫存管理 -----------------------------------------------

function setPendingFill(tabId, creds) {
  clearPendingFill(tabId);
  const timer = setTimeout(function () {
    console.warn("[pwmgr] 等待 tab " + tabId + " render 超過 " + (PENDING_FILL_TTL_MS / 1000) + " 秒,放棄填入");
    clearPendingFill(tabId);
  }, PENDING_FILL_TTL_MS);
  pendingFill[tabId] = { username: creds.username, password: creds.password, url: creds.url, timer: timer };
}

function clearPendingFill(tabId) {
  const p = pendingFill[tabId];
  if (!p) return;
  clearTimeout(p.timer);
  delete pendingFill[tabId];
}

function sendNative(msg) {
  return new Promise((resolve) => {
    if (!nativePort) connectNative();
    const port = nativePort; // 鎖定這次呼叫實際用的 port,避免之後 nativePort 變 null 時仍讀到舊變數
    if (!port) {
      resolve({ ok: false, code: "NO_HOST" });
      return;
    }
    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      try { port.onMessage.removeListener(onMsg); } catch (_) {}
      resolve(result);
    };
    const onMsg = (response) => {
      settle(response || { ok: false, code: "EMPTY" });
    };
    port.onMessage.addListener(onMsg);
    try {
      port.postMessage(msg);
    } catch (e) {
      settle({ ok: false, code: "SEND_FAIL", error: String(e) });
    }
    // port 若在等待回應期間斷線,不必等滿 5 秒 timeout
    port.onDisconnect.addListener(() => settle({ ok: false, code: "DISCONNECTED" }));
    // 5 秒 timeout
    setTimeout(() => {
      settle({ ok: false, code: "TIMEOUT" });
    }, 5000);
  });
}

function onNativeMessage(_msg) {
  // 原生主機主動推的訊息目前用不到(只 query 與 report_url 都是 client→host)
}

// --- URL 變動觸發 -----------------------------------------------------------

async function onTabUrlChange(tabId, url) {
  if (!url) return;
  if (url === lastTabUrl) return;
  lastTabUrl = url;

  // 跳過不支援的 scheme
  if (!/^https?:/i.test(url)) {
    await setBadge(tabId, "");
    currentMatches[tabId] = [];
    return;
  }

  // 平行送 query + report_url(原 host 只在 report_url 寫 current_url.json,query 是給 badge 用)
  const [qResp, rResp] = await Promise.all([
    sendNative({ type: "query", url, tabId }),
    sendNative({ type: "report_url", url, tabId, ts: Date.now() }),
  ]);

  if (qResp && qResp.ok) {
    currentMatches[tabId] = qResp.matches || [];
    await setBadge(tabId, currentMatches[tabId].length > 0 ? String(currentMatches[tabId].length) : "");
  } else {
    currentMatches[tabId] = [];
    await setBadge(tabId, "");
    if (qResp && qResp.code === "BUSY") {
      console.log("[pwmgr] GUI 忙碌中");
    }
  }
}

async function setBadge(tabId, text) {
  try {
    if (text) {
      await chrome.action.setBadgeText({ tabId, text });
      await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOR });
    } else {
      await chrome.action.setBadgeText({ tabId, text: "" });
    }
  } catch (e) {
    // 設 badge 失敗通常無關緊要(可能 tabId 已失效)
  }
}

// --- 事件訂閱 ---------------------------------------------------------------

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    onTabUrlChange(tabId, changeInfo.url);
  } else if (tab.active && tab.url && tab.url !== lastTabUrl) {
    // 部分瀏覽器 onUpdated 不給 url,但 url 變了——主動讀
    onTabUrlChange(tabId, tab.url);
  }

  // launchAndFill:新分頁 render 完 → 送 fill 訊息
  if (changeInfo.status === "complete" && pendingFill[tabId]) {
    const creds = pendingFill[tabId];
    clearPendingFill(tabId);
    chrome.tabs.sendMessage(tabId, { type: "fill", username: creds.username, password: creds.password }).catch(function () {
      // content script 可能未注入(non-http 跳轉)
    });
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && tab.url) {
      onTabUrlChange(tabId, tab.url);
    }
  } catch (_) {}
});

chrome.tabs.onRemoved.addListener((tabId) => {
  delete currentMatches[tabId];
  clearPendingFill(tabId);
});

chrome.runtime.onStartup.addListener(() => {
  connectNative();
});

// --- 訊息路由(popup / content 來問) ----------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "getMatches") {
    const tabId = msg.tabId;
    sendResponse({ ok: true, matches: currentMatches[tabId] || [] });
    return false;
  }

  if (msg.type === "queryFresh") {
    // popup 開啟時用:不吃 currentMatches 快取(該快取只在 URL 變動時更新,
    // 使用者若在同一頁面用 PWmgr GUI 新增/修改條目,快取不會反映),
    // 直接重新問一次原生主機以取得最新結果。
    (async () => {
      const resp = await sendNative({ type: "query", url: msg.url, tabId: msg.tabId });
      if (resp && resp.ok) {
        currentMatches[msg.tabId] = resp.matches || [];
        await setBadge(msg.tabId, currentMatches[msg.tabId].length > 0 ? String(currentMatches[msg.tabId].length) : "");
      }
      sendResponse(resp);
    })();
    return true;
  }

  if (msg.type === "fetch") {
    (async () => {
      const resp = await sendNative({ type: "fetch", id: msg.id });
      if (resp && resp.ok && msg.tabId != null) {
        try {
          await chrome.tabs.sendMessage(msg.tabId, {
            type: "fill",
            username: resp.entry.username,
            password: resp.password,
          });
        } catch (e) {
          // content script 可能未注入(非 http/https 頁)
        }
      }
      sendResponse(resp);
    })();
    return true; // 保持 sendResponse 開啟
  }

  if (msg.type === "ping") {
    (async () => {
      const resp = await sendNative({ type: "ping" });
      sendResponse(resp);
    })();
    return true;
  }

  if (msg.type === "getAllEntries") {
    (async () => {
      const resp = await sendNative({ type: "list" });
      sendResponse(resp);
    })();
    return true;
  }

  if (msg.type === "openLaunch") {
    (async () => {
      const url = String(msg.url || "");
      if (!/^https?:\/\//i.test(url)) {
        sendResponse({ ok: false, code: "BAD_URL", error: "僅支援 http(s)" });
        return;
      }
      try {
        await chrome.tabs.create({ url });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, code: "TAB_CREATE_FAIL", error: String(e) });
      }
    })();
    return true;
  }

  if (msg.type === "launchAndFill") {
    // fallback 模式 click → 開新分頁 + 等 render + 自動填入
    (async () => {
      const url = String(msg.url || "");
      const id = String(msg.id || "");
      console.log("[pwmgr] launchAndFill start", url, id);
      if (!/^https?:\/\//i.test(url)) {
        sendResponse({ ok: false, code: "BAD_URL", error: "僅支援 http(s)" });
        return;
      }
      if (!id) {
        sendResponse({ ok: false, code: "BAD_REQUEST", error: "缺少 id" });
        return;
      }

      // 1. 先 fetch credentials,避免 status=complete 時還在等原生主機
      const fResp = await sendNative({ type: "fetch", id });
      console.log("[pwmgr] launchAndFill fetch resp", fResp);
      if (!fResp || !fResp.ok) {
        sendResponse(fResp || { ok: false, code: "FETCH_FAIL" });
        return;
      }

      // 2. 建立新分頁
      let newTab;
      try {
        newTab = await chrome.tabs.create({ url });
        console.log("[pwmgr] launchAndFill tab created", newTab && newTab.id);
      } catch (e) {
        console.warn("[pwmgr] launchAndFill TAB_CREATE_FAIL", e);
        sendResponse({ ok: false, code: "TAB_CREATE_FAIL", error: String(e) });
        return;
      }

      // 3. 暫存 credentials,等 onUpdated status=complete 觸發 fill
      setPendingFill(newTab.id, {
        username: fResp.entry.username,
        password: fResp.password,
        url: url,
      });

      console.log("[pwmgr] launchAndFill sending ok resp, tabId=", newTab.id);
      sendResponse({ ok: true, tabId: newTab.id });
    })();
    return true;
  }

  return false;
});

// 啟動時連一次
connectNative();
