// PWmgr Companion — service worker (MV3)
// 職責:
//   1. 與原生主機 (com.thehcl.pwmgr) 保持長連線
//   2. tab URL 變動 → query + report_url
//   3. 收到 query 命中 → 設 badge;popup 點選 → fetch + 注入 content script 填表

const HOST_NAME = "com.thehcl.pwmgr";
const BADGE_COLOR = "#0078d4";
const PENDING_FILL_TTL_MS = 30000;
const LAUNCH_FILL_TABS_KEY = "launchFillTabs"; // 用 storage.local 跨 SW 重啟保留
const PENDING_FILL_KEY = "pendingFill"; // storage.local key,跨 SW 重啟保留 credentials
// login trigger 站台獨立 slot,不被 regular claimPendingFill 誤砍
const LOGIN_TRIGGER_TTL_MS = 60000;

let nativePort = null;
let lastTabUrl = null;   // 上次主動查詢的 URL(避免重複 query)
let currentMatches = {}; // tabId -> [{id, label, username, url}]
let pendingFill = {};    // tabId -> { username, password, url, timer }
let launchFillTabsSync = new Set(); // launchAndFill 開的 tabId(in-memory,onUpdated listener 同步檢查用)
// 目前還在跑的 getOtp(poll_seconds>0)輪詢 promise,見 getOtp handler 內註解。
let pendingOtpPoll = null;

// --- executeScript 注入的 fillFn --------------------------------------------
//
// 這個 function 會被 chrome.scripting.executeScript serialize 注入 page context 執行。
// 不能 closure 任何 background.js 變數,只能用 page context 全域 API(DOM / HTMLInputElement 等)。
//
// 設計:wait 密碼欄位出現,有的話填 username + password,加 lime badge,return。
// Step 1(只有 email 沒 password):wait 不到密碼 → 等 30 秒 → page unload 中斷。
// Step 2(有 password):wait 1 秒內找到 → 填 → return。

async function fillFnExecutedScript(username, password) {
  // Idempotent:同一 page 內 schedule 多次注入只填一次,避免 lime badge 無限生長
  if (window.__pwmgr_es_filled__) {
    return "ALREADY_FILLED@" + location.host;
  }
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const isUsable = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  // --- Landing page auto-click -------------------------------------------------
  // 某些 Dell 內網站 (testvault / boss) 首頁是 landing page,只有一顆 "Login" 按鈕,
  // 點下去才會跳 SSO/auth 頁,密碼欄位才會出現。
  // 為這些 host 自動 click Login,後續 retry 等密碼欄位出現再填。
  //
  // 安全考量:fillFn 只從 scheduleExecuteScriptFill / onUpdated listener 呼叫,
  // 兩個入口都被 launchFillTabs gate (background.js 內 isLaunchFillTab 檢查),
  // 所以只有 user 從 PWmgr GUI 啟動的 tab 會被自動點,手動逛網站不會誤觸。
  const LOGIN_LANDING_HOSTS = new Set(["testvault.dell.com", "boss.dell.com"]);
  // 嚴格字串匹配,排除 "Continue"、"Login with Google"、"Login to your account" 等
  const LOGIN_KEYWORDS = /^(log\s*in|login|sign\s*in|signin|登入|登入系統|會員登入)$/i;
  // 排除區塊:cookie modal、cookie banner、footer (避免點到 Close / cookie policy 連結)
  const EXCLUDE_SELECTOR = '[id*="cookie" i], [class*="cookie" i], [class*="modal-footer" i], footer';
  const isLandingHost = LOGIN_LANDING_HOSTS.has(location.hostname.toLowerCase());

  // 找 landing page 上的 "Login" 鈕並 click。
  // 嚴格條件避免誤觸 cookie modal 等非主要 UI。
  // 回傳 { ok: true, text } 或 { ok: false }
  const tryClickLandingLogin = () => {
    const candidates = Array.from(
      document.querySelectorAll('button, a[href], input[type="button"], input[type="submit"]')
    ).filter((el) => isUsable(el) && !el.closest(EXCLUDE_SELECTOR));
    for (const el of candidates) {
      const text = (el.textContent || el.value || el.getAttribute("aria-label") || "").trim();
      if (LOGIN_KEYWORDS.test(text)) {
        try {
          el.click();
          return { ok: true, text: text };
        } catch (e) {
          return { ok: false, error: String(e) };
        }
      }
    }
    return { ok: false };
  };

  // 至多 30 輪 (15 秒) 等密碼欄位出現。涵蓋 SSO redirect 後 SPA 殼 render 時間。
  for (let i = 0; i < 30; i++) {
    const inputs = Array.from(document.querySelectorAll("input")).filter(isUsable);
    const pw = inputs.find((el) => el.type === "password");
    if (!pw) {
      // 沒有密碼欄位時:若是 landing host → 嘗試點 Login 按鈕導去 SSO。
      // Idempotent flag: 已點過就不再點,避免 click 後未 navigation 又再次點擊造成迴圈。
      if (isLandingHost && !window.__pwmgr_es_login_clicked__) {
        const r = tryClickLandingLogin();
        if (r.ok) {
          window.__pwmgr_es_login_clicked__ = true;
          console.log("[pwmgr] landing: 已點擊 Login 按鈕:", JSON.stringify(r.text));
          // 點完給 navigation 800ms,下一輪再找 password
          await wait(800);
          continue;
        }
      }
      await wait(500);
      continue;
    }
    // 找 username:同 form 內 password 之前的最後一個 text/email/tel input
    const form = pw.closest("form");
    let user = null;
    if (form) {
      const textInputs = Array.from(form.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="search"], input:not([type])'
      )).filter(isUsable);
      const formElements = Array.from(form.elements);
      const pwIndex = formElements.indexOf(pw);
      for (let j = pwIndex - 1; j >= 0; j--) {
        if (textInputs.includes(formElements[j])) {
          user = formElements[j];
          break;
        }
      }
    }
    if (!user) {
      // Fallback:整頁最近 password 的 text/email input。
      // 必須排除 type="submit" / type="button" / type="reset" 等 button-like elements,
      // 否則會把 username 設到 Sign In button 上,把 button text 改成 username。
      const candidates = inputs.filter((el) => {
        const t = (el.type || "").toLowerCase();
        return t !== "password" && t !== "hidden" && t !== "submit" && t !== "button" && t !== "reset" && t !== "image";
      });
      if (candidates.length > 0) user = candidates[candidates.length - 1];
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    if (user) {
      setter.call(user, username);
      user.dispatchEvent(new Event("input", { bubbles: true }));
    }
    setter.call(pw, password);
    pw.dispatchEvent(new Event("input", { bubbles: true }));
    window.__pwmgr_es_filled__ = true; // 標記 page 內已填過,避免 schedule 重複注入時重複填入
    return "FILLED@" + location.host;
  }
  return "TIMEOUT@" + location.host;
}

// --- 排程 executeScript fill -----------------------------------------------
//
// launchAndFill 開新 tab 後,排程多次 executeScript 注入 fillFn。
// 不依賴 onUpdated listener(SW 卸載後 queued events 處理不順)。
// 涵蓋 swbm → Step 1 → Step 2 多個 navigation,直到找到密碼欄位成功填入或 TTL 到期。
// 每次 setTimeout 從 storage.local 讀最新 credentials(跨 SW 重啟一致)。

function scheduleExecuteScriptFill(tabId, username, password) {
  // 排程 20 次,間隔 2 秒,涵蓋 ~40 秒
  // (Dell SSO SPA 載入 + 兩步驟 navigation,加上 landing page auto-click (testvault/boss)
  // 點 Login 後跳 SSO/MFA 的時間)
  const intervals = [];
  for (let i = 0; i < 20; i++) {
    intervals.push(1000 + i * 2000); // 1s, 3s, 5s, ..., 39s
  }
  intervals.forEach((delay) => {
    setTimeout(() => {
      // 直接從 closure 拿 credentials,不依賴 storage(避免 SW 重啟後 storage 讀失敗)
      chrome.scripting
        .executeScript({
          target: { tabId, allFrames: true },
          func: fillFnExecutedScript,
          args: [username, password],
        })
        .then((results) => {
          const flat = (results || []).map((r) => r && r.result).filter(Boolean);
          const filled = flat.find((r) => r && r.startsWith("FILLED"));
          if (filled) {
            console.log("[pwmgr] scheduled fill SUCCESS @ delay=" + delay + "ms:", filled);
            removeLaunchFillTab(tabId);
            clearPendingFill(tabId);
          } else {
            console.log("[pwmgr] scheduled fill attempt @ delay=" + delay + "ms:", flat.join(" | ") || "no result");
          }
        })
        .catch((e) => {
          console.log("[pwmgr] scheduled fill attempt @ delay=" + delay + "ms FAILED:", e.message);
        });
    }, delay);
  });
  // 42 秒後清掉 launchFillTabs(避免後續 schedule 繼續填)
  setTimeout(() => {
    removeLaunchFillTab(tabId);
    clearPendingFill(tabId);
    console.log("[pwmgr] scheduleExecuteScriptFill timeout for tab", tabId);
  }, 42000);
  console.log("[pwmgr] scheduleExecuteScriptFill armed for tab", tabId, "20 attempts over ~40s");
}

// --- executeScript 注入的 otpFillFn -----------------------------------------
//
// 跟 fillFnExecutedScript 同樣 pattern:serialize 注入 page context 執行,
// 不能 closure 任何 background.js 變數,只能用 page context 全域 API。
//
// 用在 fillOtp 的 fallback 情境:
//   chrome://extensions Reload 會把已注入分頁的 content script 整個踢掉,
//   下次 page reload 才會重新注入。此時 popup 點 OTP → background →
//   chrome.tabs.sendMessage 會 throw(content script listener 不在)。
//   manifest 宣告的 content script 不能用 chrome.scripting.executeScript
//   { files: [...] } 重新注入(Chrome 限制),所以改成 inline func 直接
//   跑 OTP box 偵測 + 填入邏輯,繞過 listener。
//
// 邏輯跟 content.js 的 fillOtp() 對齊(Dell otpBox 多格 + AMD/Okta 單一 passcode 欄位);
// 兩邊需要同步改。實務上 OTP 邏輯變動頻率很低,duplication cost 可接受。

async function otpFillFnExecutedScript(code) {
  const isUsable = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const otpBoxes = Array.from(
    document.querySelectorAll('input.otpBox, input[class~="otpBox"], input[class*="otpBox" i]')
  ).filter(isUsable);
  const chars = String(code || "").replace(/\D/g, "").split("");
  if (chars.length === 0) {
    return { ok: false, code: "EMPTY_CODE", error: "code 沒有有效數字" };
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;

  if (otpBoxes.length >= 4) {
    // 排序:id 末段數字優先(Dell 1..N);fallback DOM 順序
    const getTailNum = (el) => {
      const m = (el.id || "").match(/(\d+)\s*$/);
      return m ? parseInt(m[1], 10) : 0;
    };
    otpBoxes.sort((a, b) => {
      const ai = getTailNum(a);
      const bi = getTailNum(b);
      if (ai > 0 && bi > 0) return ai - bi;
      const pos = a.compareDocumentPosition(b);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
    let filledCount = 0;
    const n = Math.min(otpBoxes.length, chars.length);
    for (let i = 0; i < n; i++) {
      const el = otpBoxes[i];
      const before = el.value;
      setter.call(el, chars[i]);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (el.value === chars[i] && before !== chars[i]) filledCount++;
    }
    const last = otpBoxes[n - 1];
    if (last) {
      try { last.focus(); } catch (_) {}
    }
    if (filledCount === 0) {
      return { ok: false, code: "FILL_NOOP", error: "OTP 輸入框已存在值或拒絕寫入", total: otpBoxes.length };
    }
    return { ok: true, filled: filledCount, total: otpBoxes.length, code: chars.join("") };
  }

  // AMD / Okta 風格單一輸入框(name/id 含 "passcode" 或 "otp"):完整 code 一次填入
  const singleCandidates = Array.from(
    document.querySelectorAll(
      'input[name*="passcode" i], input[id*="passcode" i], input[name*="otp" i], input[id*="otp" i]'
    )
  ).filter((el) => isUsable(el) && (el.type === "text" || !el.type) && el.type !== "password");
  if (singleCandidates.length === 0) {
    return { ok: false, code: "NOT_FOUND", error: "頁面上找不到 OTP 輸入框(需要 otpBox class 或 passcode 欄位)" };
  }
  const el = singleCandidates[0];
  const before = el.value;
  const full = chars.join("");
  setter.call(el, full);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  try { el.focus(); } catch (_) {}
  if (el.value !== full || before === full) {
    return { ok: false, code: "FILL_NOOP", error: "OTP 輸入框已存在值或拒絕寫入", total: 1 };
  }
  return { ok: true, filled: 1, total: 1, code: full };
}
// 各 tabId 的 loginTriggerPending 自動清除 timer(service worker 重啟會掉,
let loginTriggerTimers = {}; // tabId -> setTimeout handle

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

// --- launchAndFill 啟動的 tab 追蹤 ---------------------------------------
//
// 為什麼要追蹤:content script 的 requestFill 需要知道「這個 tab 是 launchAndFill
// 開的嗎?」才能決定是否走 query + fetch 自動配對。launchAndFill 開的 tab 才
// 自動配對(因為 user 已明確同意自動填),其他 tab 不自動配對(避免誤觸 click
// Continue 等侵入式行為)。
//
// 為什麼用 storage.local 不用 storage.session:chrome.storage.session 在
// extension reload/update 時會被 chrome 清掉(見 chrome docs),即使 setPendingFill
// 寫了 session,reload 後就空了。storage.local 跨 reload/update/SW 重啟都保留。

async function addLaunchFillTab(tabId) {
  try {
    const data = await chrome.storage.local.get(LAUNCH_FILL_TABS_KEY);
    const tabs = new Set(data[LAUNCH_FILL_TABS_KEY] || []);
    tabs.add(tabId);
    await chrome.storage.local.set({ [LAUNCH_FILL_TABS_KEY]: Array.from(tabs) });
  } catch (e) {
    console.warn("[pwmgr] addLaunchFillTab failed:", e && e.message || e);
  }
}

async function removeLaunchFillTab(tabId) {
  try {
    const data = await chrome.storage.local.get(LAUNCH_FILL_TABS_KEY);
    const tabs = new Set(data[LAUNCH_FILL_TABS_KEY] || []);
    tabs.delete(tabId);
    await chrome.storage.local.set({ [LAUNCH_FILL_TABS_KEY]: Array.from(tabs) });
  } catch (e) {
    console.warn("[pwmgr] removeLaunchFillTab failed:", e && e.message || e);
  }
}

async function isLaunchFillTab(tabId) {
  try {
    const data = await chrome.storage.local.get(LAUNCH_FILL_TABS_KEY);
    const tabs = new Set(data[LAUNCH_FILL_TABS_KEY] || []);
    return tabs.has(tabId);
  } catch (e) {
    return false;
  }
}

// --- launchAndFill 暫存管理 -----------------------------------------------

function setPendingFill(tabId, creds) {
  clearPendingFill(tabId);
  const timer = setTimeout(function () {
    console.warn("[pwmgr] 等待 tab " + tabId + " render 超過 " + (PENDING_FILL_TTL_MS / 1000) + " 秒,放棄填入");
    clearPendingFill(tabId);
  }, PENDING_FILL_TTL_MS);
  pendingFill[tabId] = { username: creds.username, password: creds.password, url: creds.url, timer: timer };
  // 寫到 storage.local(跨 SW 重啟保留)— listener 在 SW 重啟後仍能讀到 credentials 繼續 executeScript fill
  // 注意:storage.local 是 plain text,對 single-user extension 可接受;
  // 多人共用電腦需考慮用 native host 加密後再存,或改用 chrome.identity
  const payload = { tabId: tabId, username: creds.username, password: creds.password, url: creds.url };
  chrome.storage.local.set({ [PENDING_FILL_KEY]: payload }).catch(function (e) {
    console.warn("[pwmgr] storage.local.set pendingFill failed", e && e.message || e);
  });
  // 同步寫到 chrome.storage.session(更快速訪問,搭配 content script claimPull)
  chrome.storage.session.set({ pendingFill: payload }).catch(function (e) {
    console.warn("[pwmgr] storage.session.set failed", e && e.message || e);
  });
}

function clearPendingFill(tabId) {
  const p = pendingFill[tabId];
  if (!p) return;
  clearTimeout(p.timer);
  delete pendingFill[tabId];
  // 從 storage.local + storage.session 清掉
  chrome.storage.local.get(PENDING_FILL_KEY, function (data) {
    if (data && data[PENDING_FILL_KEY] && data[PENDING_FILL_KEY].tabId === tabId) {
      chrome.storage.local.remove(PENDING_FILL_KEY);
    }
  });
  chrome.storage.session.get("pendingFill", function (data) {
    if (data && data.pendingFill && data.pendingFill.tabId === tabId) {
      chrome.storage.session.remove("pendingFill");
    }
  });
}

// 預設 timeout。注意:getOtp(poll_seconds>0)不能沿用這個常數 —
// native host 那邊的 polling 迴圈本身就要花 poll_seconds 秒,再加上訊息傳遞
// 跟每次 COM 呼叫的開銷,實際回應時間一定會超過 poll_seconds。之前這裡固定
// 15000ms 剛好等於 popup.js 的 OTP_POLL_SECONDS(15s),等於是兩邊時間卡在
// 同一條線上賽跑 — native 那邊只會「更慢」不會「更快」,所以 JS 這裡幾乎每次
// 都先 timeout,導致 user 看到「取得失敗:TIMEOUT」,即使 native host 其實
// 再等一下就會回傳正確的 code。呼叫端(getOtp)要自己算夠寬的 timeoutMs 傳進來。
const DEFAULT_NATIVE_TIMEOUT_MS = 15000;

function sendNative(msg, timeoutMs) {
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
    // port 若在等待回應期間斷線,不必等滿 timeout
    port.onDisconnect.addListener(() => settle({ ok: false, code: "DISCONNECTED" }));
    setTimeout(() => {
      settle({ ok: false, code: "TIMEOUT" });
    }, timeoutMs || DEFAULT_NATIVE_TIMEOUT_MS);
  });
}

function onNativeMessage(_msg) {
  // 原生主機主動推的訊息目前用不到(只 query 與 report_url 都是 client→host)
}

// --- 錯誤回報(fail log) -----------------------------------------------------
//
// background(MV3 service worker)閒置會被 Chrome 直接砍掉、popup 的 DevTools
// console 一關就跟著消失,平常這兩處的未捕捉例外幾乎沒辦法回頭查。這裡把它們
// 轉送給 native host,併進同一份 pwmgr.log(見 native_host._handle_report_error),
// dev 開 log 資料夾就能同時看到 Python + extension 兩邊的失敗紀錄。
//
// Fire-and-forget:回報失敗(例如 native host 還沒連上)不重試、不擋任何流程,
// log 本來就只是輔助診斷用。

function reportError(source, message, stack, url) {
  try {
    sendNative({
      type: "report_error",
      source,
      message: String(message || ""),
      stack: stack || null,
      url: url || null,
    });
  } catch (_) {
    // 連 sendNative 都丟例外(理論上不會,它自己包 try/catch)就放棄,
    // 避免錯誤回報本身變成新的未捕捉例外來源。
  }
}

self.addEventListener("error", (event) => {
  reportError("background", event.message, event.error && event.error.stack);
});
self.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  reportError("background", String(reason && reason.message || reason), reason && reason.stack);
});

// --- Cold-start retry helper -----------------------------------------------
//
// Chrome MV3 service worker 是 event-driven:SW idle-kill 後第一次被觸發會 spawn 新
// Python native host process(沒繼承 GUI 那邊的 import cache,完全 cold start)。
// Python cold start 包含 import pywin32 + Outlook COM proxy 建立,新機器可達 2-5s。
// Chrome stdin-read timeout 約 5s,Python 還沒讀 stdin 就被殺掉 → port disconnect
// → 訊息丟失 → user 看到 TIMEOUT / DISCONNECTED。
//
// 解法:第一次 sendNative 若拿到 transient failure,主動 disconnect 舊 port、
// 重新 connectNative、給新 spawn 的 process 一段時間暖機、再 retry 一次。
// 重點:此 helper 是 cold-start 防護,只對「可能撞 cold start 的訊息」使用
// (ping / getOtp 等 user 直接操作)。
//
// retryWaitMs 不同 caller 用不同值:
//   ping:300ms(輕量訊息,Python 只要 stdin ready 就能回)
//   getOtp:1500ms(getOtp 走 fetch_latest_otp 還要載 COM proxy / 翻 stores,
//     預熱需要更久 — 配合 native_host._warmup_outlook 預熱可縮短)
//
// 第二次失敗就照原本 result 回傳(可能是 OUTLOOK_UNAVAILABLE / NO_CODE 等真正的
// 業務錯誤,也可能仍是 cold start — 但 popup 端 pingWithRetry 也會再用 sendMessage
// 包一層 retry,給 user 端兜底)。
async function sendNativeWithColdStartRetry(msg, retryWaitMs) {
  retryWaitMs = retryWaitMs || 1500;
  let resp = await sendNative(msg);
  const transientFailure =
    resp &&
    !resp.ok &&
    (resp.code === "TIMEOUT" ||
     resp.code === "DISCONNECTED" ||
     resp.code === "NO_HOST" ||
     resp.code === "SEND_FAIL");
  if (transientFailure) {
    console.log("[pwmgr] sendNative cold-start retry:", msg.type, "first code:", resp.code);
    if (nativePort) {
      try { nativePort.disconnect(); } catch (_) {}
      nativePort = null;
    }
    connectNative();
    // 給新 spawn 的 native host 一點時間讀 stdin(尤其 Python 冷啟動慢)
    await new Promise((r) => setTimeout(r, retryWaitMs));
    resp = await sendNative(msg);
    if (resp && resp.ok) {
      console.log("[pwmgr] sendNative cold-start retry success:", msg.type);
    } else {
      console.log("[pwmgr] sendNative cold-start retry still failing:", msg.type, "second code:", resp && resp.code);
    }
  }
  return resp || { ok: false, code: "EMPTY", error: "native host 沒回應" };
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

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    onTabUrlChange(tabId, changeInfo.url);
  } else if (tab.active && tab.url && tab.url !== lastTabUrl) {
    // 部分瀏覽器 onUpdated 不給 url,但 url 變了——主動讀
    onTabUrlChange(tabId, tab.url);
  }

  // launchAndFill:新分頁 render 完 → 送 fill 訊息(重試直到 content script ready 或 timeout)
  if (changeInfo.status === "complete" && pendingFill[tabId]) {
    const creds = pendingFill[tabId];
    // 不立刻 clearPendingFill — 重試期間仍要保留
    console.log("[pwmgr] onUpdated complete, will retry-send fill to tab", tabId);
    sendFillWithRetry(tabId, creds);
  }

  // launchAndFill:executeScript 直接注入 fillFn + 帶 credentials,
  // 繞過 storage 清空 / URL 不匹配 / push race 等所有問題。
  // listener async + 從 storage.local 讀 launchFillTabs 跟 pendingFill(不用 in-memory,
  // 避免 SW 重啟後 in-memory 空的問題)。Promise.race 5 秒 timeout 避免 fillFn hang。
  if (changeInfo.status === "complete") {
    try {
      const [launchData, fillData] = await Promise.all([
        chrome.storage.local.get(LAUNCH_FILL_TABS_KEY),
        chrome.storage.local.get(PENDING_FILL_KEY),
      ]);
      const launchTabs = new Set(launchData[LAUNCH_FILL_TABS_KEY] || []);
      if (!launchTabs.has(tabId)) return;
      launchFillTabsSync.add(tabId);
      const creds = fillData[PENDING_FILL_KEY];
      if (!creds || creds.tabId !== tabId) return;
      console.log("[pwmgr] onUpdated complete, executeScript fill start, tab=", tabId, "url=", changeInfo.url);
      const executePromise = chrome.scripting.executeScript({
        target: { tabId, allFrames: true },
        func: fillFnExecutedScript,
        args: [creds.username, creds.password],
      });
      const timeoutPromise = new Promise((resolve) => setTimeout(() => resolve([]), 5000));
      const results = await Promise.race([executePromise, timeoutPromise]);
      if (results && results.length > 0) {
        const flat = results.map((r) => r && r.result).filter(Boolean);
        console.log("[pwmgr] executeScript fill results:", flat.join(" | "));
      } else {
        console.log("[pwmgr] executeScript fill timeout (5s), tab=", tabId);
      }
    } catch (e) {
      console.warn("[pwmgr] executeScript fill FAILED:", e && e.message || e);
    }
  }
});

function sendFillWithRetry(tabId, creds, attempt) {
  attempt = attempt || 0;
  const maxAttempts = 10;
  chrome.tabs.sendMessage(tabId, { type: "fill", username: creds.username, password: creds.password }).then(function () {
    console.log("[pwmgr] fill sent ok to tab", tabId, "after", attempt, "retries");
    // 注意:不在這裡 clearPendingFill — chrome.tabs.sendMessage resolve 只代表「訊號已
    // 送到 tab 的 frame 0 message channel」,不代表 content script listener 真的接到並
    // 執行 fillForm(race condition:background 在 status=complete 時送,但 content script
    // 在 document_idle 才 inject,可能 listener 還沒 attach)。保留 pendingFill 直到 content
    // script 透過 claimPendingFill 主動拉走才清。
  }).catch(function (e) {
    if (attempt >= maxAttempts) {
      console.warn("[pwmgr] fill sendMessage failed after", maxAttempts, "attempts for tab", tabId, e && e.message || e);
      // push 全部失敗也不清 — polling 模式是獨立的另一條路,可能仍能成功。
      // pendingFill 由 PENDING_FILL_TTL_MS timeout 或 content script claim 清掉。
      return;
    }
    // 500ms / 1s / 1.5s / ... / 5s(等差遞增)
    const delay = Math.min(500 + attempt * 500, 5000);
    console.log("[pwmgr] fill sendMessage retry in", delay, "ms (attempt", attempt + 1, ")");
    setTimeout(function () { sendFillWithRetry(tabId, creds, attempt + 1); }, delay);
  });
}

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
  removeLaunchFillTab(tabId);
  launchFillTabsSync.delete(tabId);
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
    // Ping 是 popup init 第一個呼叫,容易踩到 SW cold start + native host Python
    // 冷啟動(Python import pywin32/keyring 可能 2-5s,Chrome stdin-read timeout
    // 約 5s,過了就會殺掉 native host process → DISCONNECTED)。
    // retry 邏輯統一在 sendNativeWithColdStartRetry。
    (async () => {
      const resp = await sendNativeWithColdStartRetry({ type: "ping" }, 300);
      sendResponse(resp || { ok: false, code: "EMPTY", error: "native host 沒回應" });
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

  if (msg.type === "solveCaptcha") {
    // content script 抓到 captcha 圖(已是 dataURL)或 URL,forward 給 native host 跑 OCR
    (async () => {
      const payload = {};
      if (typeof msg.image === "string" && msg.image.startsWith("data:")) {
        payload.image = msg.image;
      } else if (typeof msg.imageUrl === "string") {
        payload.image_url = msg.imageUrl;
      } else {
        sendResponse({ ok: false, code: "BAD_REQUEST", error: "缺少 image 或 imageUrl" });
        return;
      }
      const resp = await sendNative({ type: "solve_captcha", ...payload });
      sendResponse(resp);
    })();
    return true;
  }

  if (msg.type === "getOtp") {
    // popup 觸發:user 按「取得驗證碼」按鈕 → 跟 native host 要最近一筆 OTP code。
    // 拿到 code 後不要直接送 content script(本 background 不該知道 tab 細節);
    // 由 popup 收到 resp 後決定要 forward 給哪個 tab(見 popup.js onOtpClick)。
    //
    // 支援兩種輪詢模式(poll_seconds):
    //   - 0 / 沒給:單次 lookup(原本行為,user 已經按過按鈕但信可能還在路上)
    //   - >0:lazy polling — native host 在 poll_seconds 秒內每 2 秒撈一次 Outlook,
    //         拿到 code 就 return,timeout 就回 NO_CODE。這是「按按鈕才 polling」的實作。
    //
    // 走 sendNativeWithColdStartRetry 因為 getOtp 是 user 主動觸發、容易踩到
    // SW cold start + Python cold start 的 race condition。
    // 重要:走 retry 不能跟 poll_seconds 同時用 — 會把 poll 時間 double。所以只有
    // poll_seconds=0 才走 retry helper,有 poll_seconds 直接 sendNative(python 那邊
    // 自己會輪詢 + retry)。
    const pollSeconds = typeof msg.pollSeconds === "number" && msg.pollSeconds > 0
      ? msg.pollSeconds
      : 0;
    (async () => {
      try {
        // user 連點按鈕(popup 每次點擊都是獨立的 chrome.runtime.sendMessage,
        // 就算上一次點擊的 popup 已經關掉,前一個 getOtp 請求還是會在 native
        // host 那邊繼續跑)。native host 是單執行緒逐一處理請求(見
        // native_host.py 的 run() 主迴圈),所以連點 N 下 = 排 N 個 get_otp
        // 請求,每個都可能各自跑滿 poll_seconds、每 2 秒對 Outlook 做一次完整
        // COM 掃描 —— 疊加起來就是連續好幾十秒的 Outlook COM 呼叫,切回
        // Outlook 視窗時感覺卡頓。
        // 解法:poll_seconds>0 的請求全部共用同一個 in-flight promise —— 如果
        // 已經有一輪 polling 在跑,新按下的按鈕直接等那一輪的結果,不會再送一個
        // 新請求給 native host。
        let resp;
        if (pollSeconds > 0) {
          if (!pendingOtpPoll) {
            console.log("[pwmgr] getOtp: 開始跟 native host 要 OTP (poll=" + pollSeconds + "s)");
            const req = { type: "get_otp", poll_seconds: pollSeconds };
            // native host 的輪詢迴圈本身就要花 pollSeconds 秒,還要加上每次 COM
            // 呼叫 + 訊息傳遞的開銷,JS 這邊的 timeout 必須明顯比 pollSeconds 寬,
            // 否則幾乎每次都會在 native host 真正回應前就先 timeout。
            pendingOtpPoll = sendNative(req, pollSeconds * 1000 + 10000).finally(() => {
              pendingOtpPoll = null;
            });
          } else {
            console.log("[pwmgr] getOtp: 已有一輪 polling 在跑,搭同一班車等結果");
          }
          resp = await pendingOtpPoll;
        } else {
          resp = await sendNativeWithColdStartRetry({ type: "get_otp" }, 1500);
        }
        console.log("[pwmgr] getOtp: native host resp =", resp);
        sendResponse(resp || { ok: false, code: "EMPTY", error: "native host 回傳空" });
      } catch (e) {
        console.warn("[pwmgr] getOtp handler 拋例外:", e && e.message || e);
        try {
          sendResponse({ ok: false, code: "EXCEPTION", error: String(e && e.message || e) });
        } catch (_) {
          // channel 可能已關,沒辦法
        }
      }
    })();
    return true;
  }

  if (msg.type === "listOtpFolders") {
    // popup 「OTP folder 設定」UI 用:列出可選的 Outlook folders。
    (async () => {
      try {
        const resp = await sendNativeWithColdStartRetry({ type: "list_otp_folders" }, 1500);
        sendResponse(resp || { ok: false, code: "EMPTY", error: "native host 回傳空" });
      } catch (e) {
        try {
          sendResponse({ ok: false, code: "EXCEPTION", error: String(e && e.message || e) });
        } catch (_) {}
      }
    })();
    return true;
  }

  if (msg.type === "setOtpFolder") {
    // popup 設完 OTP folder 後送到 native host 落地存 settings.json。
    (async () => {
      try {
        const resp = await sendNative({ type: "set_otp_folder", path: msg.path });
        sendResponse(resp || { ok: false, code: "EMPTY", error: "native host 回傳空" });
      } catch (e) {
        try {
          sendResponse({ ok: false, code: "EXCEPTION", error: String(e && e.message || e) });
        } catch (_) {}
      }
    })();
    return true;
  }

  if (msg.type === "fillOtp") {
    // popup 拿到 OTP code 後,要求 background 把 code 寫進指定 tab 的頁面。
    //
    // 為什麼一律走 chrome.scripting.executeScript({allFrames:true}) 而不是
    // chrome.tabs.sendMessage(tabId, ...) 廣播給 content script:
    //   OTP 驗證碼頁常見多個 frame(例如 Okta 的 discovery/device-fingerprint
    //   iframe)。content_scripts 設定是 all_frames:true,所以每個 frame 都會有
    //   一份 fillOtp 的 chrome.runtime.onMessage listener。sendMessage 沒指定
    //   frameId 時會廣播給所有 frame,但只有「第一個回應」會被採用——如果不相關
    //   的 frame(例如某個追蹤用 iframe 裡剛好有欄位 name/id 含 "otp" 之類的
    //   子字串)比真正的驗證碼頁 frame 先回應 NOT_FOUND 或誤填了不相關欄位,
    //   popup 就會顯示「已填入」但其實真正看得到的欄位完全沒被寫入 —— 這正是
    //   實測遇到的「popup 說已填入,網頁上卻沒變化」。
    //
    //   executeScript({allFrames:true}) 則會回傳「每個 frame各自的執行結果」
    //   陣列,我們可以明確挑出真正 ok:true 的那個 frame,不會被無關 frame 的
    //   回應蓋掉。
    (async () => {
      const tabId = msg.tabId;
      const code = String(msg.code || "");
      const finish = (result, extra) => {
        let url = null;
        try {
          url = extra && extra.url;
        } catch (_) {}
        console.log("[pwmgr] fillOtp finish:", result, "url=", url);
        reportError(
          "otp_fill_debug",
          `tabId=${tabId} code=${code || "(empty)"} result=${JSON.stringify(result)}`,
          null,
          url
        );
        sendResponse(result);
      };
      if (!Number.isInteger(tabId)) {
        finish({ ok: false, code: "BAD_TAB_ID" });
        return;
      }
      if (!code) {
        finish({ ok: false, code: "EMPTY_CODE" });
        return;
      }
      let tabUrl = null;
      try {
        const tab = await chrome.tabs.get(tabId);
        tabUrl = tab && tab.url;
      } catch (_) {}
      try {
        const results = await chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          func: otpFillFnExecutedScript,
          args: [code],
        });
        const flat = (results || []).map((r) => r && r.result).filter(Boolean);
        console.log("[pwmgr] fillOtp per-frame results:", flat);
        const filled = flat.find((r) => r && r.ok);
        if (filled) {
          finish({ ok: true, ...filled }, { url: tabUrl });
          return;
        }
        const errResult = flat.find((r) => r && !r.ok);
        if (errResult) {
          finish({ ok: false, code: errResult.code || "FILL_FAIL", error: errResult.error }, { url: tabUrl });
          return;
        }
        finish(
          { ok: false, code: "FILL_FAIL", error: "沒有任何 frame 回傳結果(frames=" + (results || []).length + ")" },
          { url: tabUrl }
        );
      } catch (e) {
        // executeScript 失敗(tabId 失效 / 非 http(s) / 權限不足)
        finish({ ok: false, code: "FORWARD_FAIL", error: (e && e.message) || String(e) }, { url: tabUrl });
      }
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

  if (msg.type === "setupLoginTrigger") {
    // content script 在 Dell SSO landing 頁(testvault/boss.dell.com)準備點
    // login 按鈕前呼叫,把帳密暫存成 loginTriggerPending。
    //
    // ⚠ 為什麼不用 regular pendingFill slot?
    // launchAndFill 流程會在 create tab 之前就先 setPendingFill。tab 載入後
    // content script 200ms 的 claimPendingFill timer 會搶先撈掉 regular
    // pendingFill,造成 SSO 跳轉後的新 content script 撈不到 → 不會填。
    // 解法:獨立 slot loginTriggerPending,跟 regular 完全隔離,兩個 claim
    // 都跑,誰有資料用誰。
    const senderTabId = _sender && _sender.tab && _sender.tab.id;
    if (!senderTabId) {
      sendResponse({ ok: false, code: "NO_TAB_ID" });
      return false;
    }
    const creds = {
      tabId: senderTabId,
      username: String(msg.username || ""),
      password: String(msg.password || ""),
      url: String(msg.url || ""),
    };
    chrome.storage.session.set({ loginTriggerPending: creds }).catch(function (e) {
      console.warn("[pwmgr] loginTriggerPending.set failed", e && e.message || e);
    });
    // 60s TTL 自動清:避免 SSO 卡住時 stale credential 永遠留著被誤填。
    // 用 in-memory timer 對齊 set 內容(若期間被 overwrite,舊 timer 失效)。
    if (loginTriggerTimers[senderTabId]) clearTimeout(loginTriggerTimers[senderTabId]);
    loginTriggerTimers[senderTabId] = setTimeout(function () {
      chrome.storage.session.remove("loginTriggerPending", function () {
        // best-effort 清掉,失敗也無妨
      });
      delete loginTriggerTimers[senderTabId];
    }, LOGIN_TRIGGER_TTL_MS);
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "claimLoginTrigger") {
    // SSO 跳轉後的新 content script 主動 claim。tabId 對得到才回。
    const senderTabId = _sender && _sender.tab && _sender.tab.id;
    chrome.storage.session.get("loginTriggerPending", function (data) {
      const p = data && data.loginTriggerPending;
      if (!p || p.tabId !== senderTabId) {
        sendResponse({ ok: false, code: "NO_PENDING" });
        return;
      }
      chrome.storage.session.remove("loginTriggerPending");
      if (loginTriggerTimers[senderTabId]) {
        clearTimeout(loginTriggerTimers[senderTabId]);
        delete loginTriggerTimers[senderTabId];
      }
      sendResponse({ ok: true, username: p.username, password: p.password, url: p.url });
    });
    return true;
  }

  if (msg.type === "cancelLoginTrigger") {
    // content script 點完 login 按鈕後,如果 URL 沒變(同頁 modal),不需要
    // 留著 loginTriggerPending,清掉避免下一頁誤填。同時也清 regular pendingFill
    // 作為保險。
    const senderTabId = _sender && _sender.tab && _sender.tab.id;
    if (senderTabId) {
      clearPendingFill(senderTabId);
      chrome.storage.session.remove("loginTriggerPending");
      if (loginTriggerTimers[senderTabId]) {
        clearTimeout(loginTriggerTimers[senderTabId]);
        delete loginTriggerTimers[senderTabId];
      }
    }
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "claimPendingFill") {
    // content script 啟動後主動問「有沒有 pending fill 給我這個 tab?」
    // 解 MV3 sendMessage 與 listener attach 之間的 race condition
    const senderTabId = _sender && _sender.tab && _sender.tab.id;
    chrome.storage.session.get("pendingFill", function (data) {
      const p = data && data.pendingFill;
      if (!p || p.tabId !== senderTabId) {
        sendResponse({ ok: false, code: "NO_PENDING" });
        return;
      }
      // 找到對應的 pending fill → 送給 content script,清掉
      clearPendingFill(senderTabId);
      sendResponse({ ok: true, username: p.username, password: p.password, url: p.url });
    });
    return true;
  }

  if (msg.type === "requestFill") {
    // Content script 偵測到登入表單(Step 1 email 或 Step 2 password),
    // 主動來要 fill credentials。
    //
    // 兩條路徑:
    //   1. launchAndFill 留下的 pendingFill(storage.session,可能在 reload 後被清)
    //   2. 此 tab 是 launchAndFill 開的 → 走 query + fetch 自動配對第一個匹配
    //
    // 路徑 2 是 fallback,確保即使 storage.session 被清、SW 重啟,
    // launchAndFill tab 仍能從 native host 重新拿 credentials 填入。
    // 非 launchAndFill tab 不會走路徑 2(避免一般 login page 也自動配對)。
    (async () => {
      const senderTabId = _sender && _sender.tab && _sender.tab.id;
      const url = String(msg.url || "");

      // 1. 先看有沒有 launchAndFill 留下的 pendingFill(優先,免去 query + fetch 成本)
      try {
        const data = await chrome.storage.session.get("pendingFill");
        const p = data && data.pendingFill;
        if (p && p.tabId === senderTabId) {
          clearPendingFill(senderTabId);
          sendResponse({ ok: true, username: p.username, password: p.password, url: p.url });
          return;
        }
      } catch (e) {
        console.warn("[pwmgr] requestFill storage read failed:", e && e.message || e);
      }

      // 2. 此 tab 是 launchAndFill 開的嗎?(storage.local 跨 SW 重啟保留)
      if (!(await isLaunchFillTab(senderTabId))) {
        sendResponse({ ok: false, code: "NO_PENDING" });
        return;
      }

      // 3. 是 launchAndFill tab → query + fetch 自動配對第一個匹配
      if (!/^https?:/i.test(url)) {
        sendResponse({ ok: false, code: "BAD_URL" });
        return;
      }
      const qResp = await sendNative({ type: "query", url, tabId: senderTabId });
      if (!qResp || !qResp.ok || !qResp.matches || qResp.matches.length === 0) {
        sendResponse({ ok: false, code: "NO_MATCH" });
        return;
      }
      const entry = qResp.matches[0];
      const fResp = await sendNative({ type: "fetch", id: entry.id });
      if (!fResp || !fResp.ok) {
        sendResponse(fResp || { ok: false, code: "FETCH_FAIL" });
        return;
      }
      sendResponse({
        ok: true,
        username: fResp.entry.username,
        password: fResp.password,
        url: url,
      });
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
      // 標記此 tab 是 launchAndFill 開的,讓後續 content script requestFill 走 query + fetch
      // 即使 SW 重啟 / extension reload 清掉 storage.session 也能繼續運作
      addLaunchFillTab(newTab.id);
      launchFillTabsSync.add(newTab.id); // 同步 in-memory(讓 onUpdated listener 同步檢查)

      // 排程多次 executeScript,涵蓋 swbm → Step 1 → Step 2 多個 navigation。
      // 不依賴 onUpdated listener(listener 在 SW 卸載後 queue 內 events 處理不順)。
      // 每次 setTimeout 從 storage.local 讀 credentials(跨 SW 重啟一致)。
      scheduleExecuteScriptFill(newTab.id, fResp.entry.username, fResp.password);

      console.log("[pwmgr] launchAndFill sending ok resp, tabId=", newTab.id);
      sendResponse({ ok: true, tabId: newTab.id });
    })();
    return true;
  }

  if (msg.type === "reportError") {
    // popup / content script 沒有 nativePort,經 background 轉送給 native host。
    reportError(msg.source, msg.message, msg.stack, msg.url);
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

// 啟動時連一次
connectNative();
