// content.js — 自動填入 username / password,以及 captcha
// 規則:
//   1. 找頁面上所有 input[type=password] (限可見、未被 disabled)
//   2. 每個 password 往上找同一 <form> 中前一個 text/email/tel input 作為 username
//   3. 設值後派發 input + change 事件(React/Vue 受控元件才會更新 state)
//   4. captcha 偵測:由 popup 觸發,找頁面上疑似 captcha 的 <img> 與對應 input,
//      把圖轉 dataURL 送 native host 跑 OCR,結果填回 input(失敗就甚麼都不做)
//   5. login trigger 站台(如 Dell SSO landing):先請 background 把帳密存成
//      pendingFill,再點 login 按鈕觸發 SSO 跳轉,redirect 後新頁面的 content
//      script 透過 claimPendingFill 撿到帳密繼續填。

// 防止重複注入:extension reload 後 background 可能用 chrome.scripting.executeScript
// 把 content.js 重新注入到已開啟的分頁(原 content script 的 isolated world 已被踢掉)。
// 若同一個 isolated world 內被注入兩次,onMessage listener 會重複註冊,
// watchForPasswordAndClaim 與 captcha/OTP 偵測的 MutationObserver / Interval 也會重複跑。
// 用 __pwmgr_injected__ flag 在 IIFE 內入口處 bail out,確保邏輯只跑一次。

(function () {
  "use strict";

  if (window.__pwmgr_injected__) {
    console.log("[pwmgr] content script 已在,跳過重複注入");
    return;
  }
  window.__pwmgr_injected__ = true;

  console.log("[pwmgr] CONTENT SCRIPT INJECTED at", Date.now(), "url=", location.href);

  // content script 的 log 混在該網頁自己的 console 裡,頁面一 reload/導頁就沒了
  // ——轉送給 background(它有 nativePort)寫進 pwmgr.log,才找得回來。
  //
  // 只回報「這支 content script 自己」的例外,不回報該網頁本身的 JS 錯誤:
  // window 的 error/unhandledrejection 事件理論上不會跨 isolated world(content
  // script 與頁面本身的 JS 是分開的執行環境),但保險起見仍用 filename/stack
  // 是否含 chrome-extension:// 過濾一次,避免不小心把使用者瀏覽的網站自己的
  // 錯誤內容送出去。
  function reportError(message, stack, filename) {
    if (filename && !String(filename).includes("chrome-extension://")) return;
    if (!filename && stack && !String(stack).includes("chrome-extension://")) return;
    try {
      chrome.runtime.sendMessage({
        type: "reportError",
        source: "content",
        message: String(message || ""),
        stack: stack || null,
        url: location.href,
      });
    } catch (_) {
      // extension context invalidated(擴充功能重載中)等情況下放棄回報
    }
  }
  window.addEventListener("error", (event) => {
    reportError(event.message, event.error && event.error.stack, event.filename);
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    reportError(String(reason && reason.message || reason), reason && reason.stack, null);
  });

  if (window.__pwmgr_filling__) return; // 防止重複
  window.__pwmgr_filling__ = true;

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "fill") {
      console.log("[pwmgr] content received fill", msg.username ? "(has username)" : "(no username)");
      (async () => {
        try {
          await fillFormWithLoginTrigger(msg.username || "", msg.password || "");
        } catch (e) {
          console.warn("[pwmgr] fill error:", e);
        } finally {
          window.__pwmgr_filling__ = false;
        }
      })();
      // 自動接 captcha(若有)。延遲 500ms 讓頁面把 captcha 圖/輸入框 render 出來再嘗試。
      // solveCaptcha 找不到會回 ok:false 自行處理,這裡 fire-and-forget。
      if (msg.auto_captcha !== false) {
        setTimeout(() => {
          solveCaptcha().then((r) => {
            if (r && r.ok) {
              console.log("[pwmgr] auto captcha filled:", r.text);
            } else if (r && r.code) {
              console.log("[pwmgr] auto captcha skipped:", r.code);
            }
          }).catch((e) => {
            console.warn("[pwmgr] auto captcha threw:", e);
          });
        }, 500);
      }
    }
  });

  // Watcher 模式:content script 偵測到 password input 出現,就主動跟 background
  // 要 fill credentials。完全繞過 background push 的 race condition(background
  // 在 status=complete 送 fill,但 listener 還沒 attach 訊號會掉)。
  //
  // 行為:
  //   - 一啟動就開始觀察 DOM
  //   - 第一次偵測到可見的 password input → 跟 background 要 launchAndFill 留下的
  //     pendingFill(如果有),或 query + fetch 自動配對(看 background handler 決定)
  //   - 拿到 fill → 跑 fillForm
  //
  // 為何不用 polling 問 background「有沒有 pendingFill」:因為即使有,也要等
  // page render 完才能 fill,而 page render 完的訊號就是 password input 出現,
  // 直接觀察 DOM 更直覺。Polling 在 9.5 秒 SPA 殼期間其實是空轉。
  (function watchForPasswordAndClaim() {
    let done = false;
    const maxWaitMs = 60000; // 最多等 60 秒
    const startTs = Date.now();
    const tryClaim = () => {
      if (done) return;
      if (Date.now() - startTs > maxWaitMs) {
        done = true;
        cleanup();
        console.log("[pwmgr] watch timeout, no login input appeared within 60s");
        return;
      }
      // 偵測「看起來像登入表單」:有可見的 email/username input 或 password input。
      // Dell SWBM 兩步驟 SSO 第一步只有 email,沒有 password — 也要觸發,
      // 否則 push race 時整個流程卡死。
      const loginish = isLoginFormLikely();
      if (!loginish) return;
      done = true;
      cleanup();
      console.log("[pwmgr] 偵測到登入表單,主動跟 background 要 fill, url=", location.href);
      chrome.runtime.sendMessage({ type: "requestFill", url: location.href }, function (resp) {
        if (chrome.runtime.lastError) {
          console.warn("[pwmgr] requestFill runtime error:", chrome.runtime.lastError.message);
          return;
        }
        if (resp && resp.ok && resp.username != null) {
          console.log("[pwmgr] 從 background 拿到 fill, 開始填入");
          try {
            fillForm(resp.username || "", resp.password || "");
          } catch (e) {
            console.warn("[pwmgr] fill error:", e);
          } finally {
            window.__pwmgr_filling__ = false;
          }
          // 串 captcha
          setTimeout(() => {
            solveCaptcha().then((r) => {
              if (r && r.ok) {
                console.log("[pwmgr] auto captcha filled:", r.text);
              } else if (r && r.code) {
                console.log("[pwmgr] auto captcha skipped:", r.code);
              }
            }).catch((e) => {
              console.warn("[pwmgr] auto captcha threw:", e);
            });
          }, 500);
        } else {
          console.log("[pwmgr] background 沒給 fill:", resp && resp.code, "(這是正常:可能此 tab 不是 launchAndFill 開的)");
        }
      });
    };
    const observer = new MutationObserver(tryClaim);
    const target = document.body || document.documentElement;
    if (target) {
      observer.observe(target, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["style", "class", "type", "disabled", "hidden"],
      });
    }
    const pollId = setInterval(tryClaim, 300);
    const timeoutId = setTimeout(() => {
      if (!done) {
        done = true;
        cleanup();
        console.log("[pwmgr] watch hard timeout (60s), giving up");
      }
    }, maxWaitMs + 1000);
    function cleanup() {
      try { observer.disconnect(); } catch (_) {}
      clearInterval(pollId);
      clearTimeout(timeoutId);
    }
    // 立即跑一次檢查(page load 完可能 input 已經在了)
    tryClaim();
  })();

  // 判斷 page 上是否已有「登入表單」任一徵兆(可見的 email/username input 或 password input)。
  // 用於 watcher 條件 — Dell SWBM 兩步驟 SSO 第一步只有 email,沒有 password,
  // 也要算登入表單,以便兩步驟流程完整自動化。
  function isLoginFormLikely() {
    const usable = Array.from(document.querySelectorAll("input")).filter(isUsable);
    if (usable.length === 0) return false;
    for (const el of usable) {
      if (findPasswordCandidates().includes(el)) return true;
      // email/username/text/search 類,且不是 captcha
      const t = (el.type || "").toLowerCase();
      if (["email", "text", "tel", "url", "search", ""].includes(t) && !looksLikeCaptchaField(el)) {
        return true;
      }
    }
    return false;
  }

  // 兩步驟 SSO 第一步:email + Continue/Next 按鈕,沒有 password。
  // 自動 click Next 讓 page navigation 到密碼頁。
  // 嚴格條件降低誤觸風險:
  //   1. 按鈕必須在某個 <form> 內(避免 click cookie banner 等非登入 UI)
  //   2. 按鈕文字必須嚴格匹配關鍵字(避免 click 到「Cancel」之類的)
  //   3. 過濾不可見 / disabled 的按鈕
  function tryClickNextButton() {
    const keywords = /^(continue|next|proceed|sign\s*in|signin|log\s*in|login|登入|下一步|繼續|確認)$/i;
    const forms = document.querySelectorAll("form");
    for (const form of forms) {
      // 只看 form 內的 submit-like button
      const candidates = Array.from(
        form.querySelectorAll('button, input[type="submit"], input[type="button"]')
      ).filter(isUsable);
      for (const btn of candidates) {
        const text = (
          btn.value ||
          btn.textContent ||
          btn.getAttribute("aria-label") ||
          ""
        ).trim();
        if (keywords.test(text)) {
          console.log("[pwmgr] 兩步驟 SSO:點擊 Next/Continue 按鈕:", JSON.stringify(text));
          btn.click();
          return true;
        }
      }
    }
    return false;
  }
  // 啟動後主動問 background 有沒有 pending fill(解決 MV3 sendMessage 與
  // content script listener attach 之間的 race condition)。
  //
  // 同時 claim 兩個 slot:
  //   - regular pendingFill:launchAndFill 一進場就 setPendingFill
  //   - loginTriggerPending:Dell SSO landing 頁 handleLoginTriggerSite 在 click 前設定
  // 兩個隔離開才不會 race(原本都擠 pendingFill,setupLoginTrigger 跟 claimPendingFill
  // 會互搶)。
  setTimeout(async function () {
    // 從 window 或 message sender 拿 tabId(content script 沒有直接的 tabId API,
    // 改由 background 從 sender.tab.id 推斷;若 sender 沒給,我們仍送但不帶 tabId,
    // background 會從 storage 比對)
    let resp = null;
    try {
      const [regular, loginTrigger] = await Promise.all([
        chrome.runtime.sendMessage({ type: "claimPendingFill" }).catch(function () { return null; }),
        chrome.runtime.sendMessage({ type: "claimLoginTrigger" }).catch(function () { return null; }),
      ]);
      // 優先用 loginTrigger(若 setupLoginTrigger 已設,代表已經 click 過,
      // 來到 SSO 頁,直接填即可);fallback 用 regular
      resp = (loginTrigger && loginTrigger.ok) ? loginTrigger : regular;
    } catch (e) {
      console.warn("[pwmgr] claim error:", e);
    }
    if (resp && resp.ok && resp.username != null) {
      console.log("[pwmgr] content claimed pending fill");
      try {
        await fillFormWithLoginTrigger(resp.username || "", resp.password || "");
      } catch (e) {
        console.warn("[pwmgr] claim fill error:", e);
      } finally {
        window.__pwmgr_filling__ = false;
      }
      // claimPendingFill 也代表「launchAndFill 開新分頁的自動流程」,同樣串 captcha
      setTimeout(() => {
        solveCaptcha().then((r) => {
          if (r && r.ok) {
            console.log("[pwmgr] auto captcha filled:", r.text);
          } else if (r && r.code) {
            console.log("[pwmgr] auto captcha skipped:", r.code);
          }
        }).catch((e) => {
          console.warn("[pwmgr] auto captcha threw:", e);
        });
      }, 500);
    }
  }, 200);

  // ===== Login trigger 站台(Dell SSO 等) =====================================
  //
  // 流程:
  //   1. 偵測目前 URL 是否在 LOGIN_TRIGGER_SITES 清單內
  //   2. 找到「登入」按鈕(用文字 + aria-label 模糊比對)
  //   3. 請 background 把帳密暫存成 pendingFill(同 tabId)
  //   4. 點按鈕 → 觸發 SSO redirect → 頁面 navigate → 當前 content script 死掉
  //   5. redirect 後的新頁面載入新 content script → claimPendingFill 撿到帳密 → 填入
  //
  // 重要:必須先存再點。順序反過來的話 click 會立即 navigate,內容腳本來不及送
  // setupLoginTrigger 訊息給 background。
  //
  // 注意:login 按鈕若用 target="_blank" 開新 tab,目前流程不會處理(pending fill
  // 會綁在原 tabId,新 tab 撈不到)。Dell 兩個站台都是同 tab 跳轉,先這樣。

  const LOGIN_TRIGGER_SITES = [
    /^https:\/\/testvault\.dell\.com\//i,
    /^https:\/\/boss\.dell\.com\//i,
  ];

  function isLoginTriggerSite(url) {
    if (!url) return false;
    return LOGIN_TRIGGER_SITES.some((re) => re.test(url));
  }

  function findLoginButton() {
    // 候選:可見的 <a> / <button> / [role=button] / <input type=submit>
    const candidates = Array.from(
      document.querySelectorAll('a, button, [role="button"], input[type="submit"], input[type="button"]')
    ).filter(isUsable);
    // 文字 / aria-label / title / value 中任一命中:
    //   - 英:login / sign in / log on / sign-in / log-in
    //   - 繁中:登入 / 登錄 / 登入帳戶
    //   - 簡中:登录 / 登入
    //   - 日:ログイン
    //   - 韓:로그인
    //   - 德/法/義/西/葡 沒列——Dell IBP 主要是英中
    const loginRe = /\blog[\s\-]*in\b|\bsign[\s\-]*in\b|\blog\s*on\b|登入|登錄|登录|ログイン|로그인/i;
    const hits = [];
    for (const el of candidates) {
      const haystack = (
        (el.textContent || "") + " " +
        (el.getAttribute("aria-label") || "") + " " +
        (el.title || "") + " " +
        (el.value || "")
      ).trim();
      if (!loginRe.test(haystack)) continue;
      // 排除明顯是登出 / 切換帳號連結(避免點錯)
      if (/\blog\s*out\b|\bsign\s*out\b|\bswitch\s*(user|account)\b/i.test(haystack)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      hits.push({ el, area: r.width * r.height });
    }
    if (hits.length === 0) return null;
    // 取面積最大(最顯眼)的當作主登入鈕
    hits.sort((a, b) => b.area - a.area);
    return hits[0].el;
  }

  async function handleLoginTriggerSite(username, password) {
    console.log("[pwmgr] login trigger 站台:", location.href);
    const btn = findLoginButton();
    if (!btn) {
      // 沒按鈕 → 降級走 fillForm;landing 頁通常沒 password 欄位,只會印 log 無害
      console.log("[pwmgr] 找不到 login 按鈕,降級走一般 fillForm");
      return false;
    }
    // 1. 先把帳密交給 background 存成 pendingFill,redirect 後新分頁的
    //    content script 才撈得到。必須在 click 之前送達。
    try {
      const resp = await chrome.runtime.sendMessage({
        type: "setupLoginTrigger",
        username,
        password,
        url: location.href,
      });
      if (!resp || !resp.ok) {
        console.warn("[pwmgr] setupLoginTrigger 失敗:", resp);
        return false;
      }
    } catch (e) {
      console.warn("[pwmgr] setupLoginTrigger 例外:", e);
      return false;
    }
    // 2. 記下點擊前的 URL,等下判斷是 navigate(SSO 跳轉)還是同頁(modal)
    const startHref = location.href;
    console.log("[pwmgr] 點 login 按鈕(觸發 SSO 跳轉):", btn.textContent && btn.textContent.trim());
    btn.click();
    // 3. 觀察 250ms:
    //    - URL 變了 → navigate 走了 → 新分頁的 content script 接手,return true
    //    - URL 沒變 → 可能是 modal/同頁登入 → 清掉 pendingFill(用不到),
    //      return false 讓 caller 走 fillForm 填 modal
    await new Promise((r) => setTimeout(r, 250));
    if (location.href !== startHref) {
      console.log("[pwmgr] URL 已變(已 navigate),新頁面 content script 接手");
      return true;
    }
    console.log("[pwmgr] URL 未變,可能是 modal,清 pendingFill 並降級走 fillForm");
    try {
      await chrome.runtime.sendMessage({ type: "cancelLoginTrigger" });
    } catch (_) {
      // 清不掉也沒關係,TTL 30s 會自動過期
    }
    return false;
  }

  // fillForm 的 async 包裝:login trigger 站台先點按鈕再走原有流程。
  // 兩個 fill 入口(popup fill message + claimPendingFill)都改用這個。
  async function fillFormWithLoginTrigger(username, password) {
    if (isLoginTriggerSite(location.href)) {
      const handled = await handleLoginTriggerSite(username, password);
      if (handled) return; // 點完就等新頁面的 content script 接續
      // 找不到按鈕 → 降級走一般 fillForm
    }
    fillForm(username, password);
  }

  function fillForm(username, password) {
    const passwordInputs = findPasswordCandidates();
    if (passwordInputs.length === 0) {
      // 部分網站(如 Dell SWBM / Google / Microsoft)採兩步驟登入:
      // 先填 email,按「Continue」後密碼欄位才出現。
      // 先填 email,然後嘗試自動 click Continue 跳到密碼頁;若無對應按鈕則 fallback 等密碼欄位。
      const filledUsername = fillUsernameOnly(username);
      if (filledUsername) {
        console.log("[pwmgr] 尚無密碼欄位,已先填帳號");
        const clicked = tryClickNextButton();
        if (!clicked) {
          // 沒找到 Continue/Next 按鈕(可能 user 手動按、或其他文字)— 等密碼欄位出現後自動補填
          console.log("[pwmgr] 未找到 Continue/Next 按鈕,改為等待密碼欄位出現後補填");
          waitForPasswordField(username, password);
        }
        // click 成功的話:page 會 navigation,舊 content script 卸載,
        // 新 content script 在 Step 2 注入後由 watcher 自動要 fill → 填入密碼。
      } else {
        // 頁面還在 SPA 載入中、連帳號欄位都還沒 render。
        // 等任何 input 出現後再嘗試一次(整個流程重來)。
        console.log("[pwmgr] 頁面還沒 render input,等待 input 出現後重試 fill");
        waitForAnyInput(username, password);
      }
      return;
    }

    let filled = 0;
    for (const pwInput of passwordInputs) {
      // 找 username:同一 form 中前一個 text/email/tel/url/search
      // 注意:要排除 captcha 欄位(looksLikeCaptchaField),免得在「密碼 + captcha」頁面
      // 把 username 塞進 captcha 輸入框。
      const form = pwInput.closest("form");
      let userInput = null;
      if (form) {
        const candidates = Array.from(form.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="search"], input:not([type])'));
        // 取 password 之前的最後一個
        for (const c of candidates) {
          if (!isUsable(c) || looksLikePasswordField(c) || looksLikeCaptchaField(c)) continue;
          const idxPw = indexInForm(form, pwInput);
          const idxC = indexInForm(form, c);
          if (idxC >= 0 && idxC < idxPw) {
            userInput = c;
          }
        }
      }
      // 同一 form 找不到時退回:整頁最接近 password 的可填欄位
      if (!userInput) {
        userInput = nearestUsernameInput(pwInput);
      }

      setValue(pwInput, password);
      if (userInput) {
        setValue(userInput, username);
      } else {
        console.log("[pwmgr] 找不到 username 欄位(可能此頁只有密碼 + captcha),跳過 username 填入");
      }
      filled++;
    }
    console.log(`[pwmgr] 已填入 ${filled} 個密碼欄位`);
  }

  // 注意:querySelectorAll("input") 若不限制 type,會連 type="submit"/"button" 這種
  // 按鈕都一起抓進來 —— 這類按鈕的 value 屬性其實是顯示文字(如「Verify」),
  // 一旦被誤判成「username 欄位」寫入 setValue,按鈕文字就會被換成帳號 email。
  // 只挑文字型 input(text/email/tel/url/search/未指定 type),排除 submit/button/
  // checkbox/radio/hidden/file/image/reset 等非文字型。
  function fillUsernameOnly(username) {
    const allInputs = Array.from(
      document.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="search"], input:not([type])'
      )
    );
    const usableList = allInputs.filter((el) => isUsable(el) && !looksLikePasswordField(el) && !looksLikeCaptchaField(el));
    console.log("[pwmgr] fillUsernameOnly candidates", usableList.length, "/ total inputs:", allInputs.length);
    if (allInputs.length > 0 && usableList.length === 0) {
      console.log("[pwmgr] no usable input; all inputs:", allInputs.map(function (el) {
        return { id: el.id, name: el.name, type: el.type, hidden: el.offsetParent === null, rect: { w: el.getBoundingClientRect().width, h: el.getBoundingClientRect().height } };
      }));
    }
    const candidates = usableList;
    if (candidates.length === 0) return false;
    const best =
      candidates.find((el) => (el.autocomplete || "").toLowerCase().includes("username")) ||
      candidates.find((el) => el.type === "email") ||
      candidates[0];
    setValue(best, username);
    return true;
  }

  function waitForPasswordField(username, password, timeoutMs = 60000) {
    // 有些網站(尤其舊式企業系統,如 Agile PLM 這類 JSP 頁面)不是「新增」密碼欄位節點,
    // 而是把既有欄位用 CSS class / style / disabled 屬性切換成可見——單靠 MutationObserver
    // 監聽 childList 抓不到這種變化,所以額外加 attributes 監聽 + 定時輪詢當保險。
    // timeout 拉長到 60 秒:部分網站需要使用者自己按「下一步/Continue」才會出現密碼欄位,
    // 這段等待時間要涵蓋使用者「看到帳號填好→手動點按鈕」的真實反應時間。
    let done = false;
    const cleanup = () => {
      observer.disconnect();
      clearInterval(pollId);
      clearTimeout(timeoutId);
    };
    const tryFill = () => {
      if (done) return;
      if (findPasswordCandidates().length === 0) return;
      done = true;
      cleanup();
      console.log("[pwmgr] 密碼欄位已出現,補填密碼");
      fillForm(username, password);
    };
    const observer = new MutationObserver(tryFill);
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["style", "class", "type", "disabled", "hidden"],
    });
    const pollId = setInterval(tryFill, 300);
    const timeoutId = setTimeout(() => {
      if (done) return;
      done = true;
      cleanup();
      console.log("[pwmgr] 等待密碼欄位逾時,放棄補填");
    }, timeoutMs);
  }

  // SPA 載入延遲:整個頁面連一個 input 都還沒 render。
  // 等任何 input 出現後整個 fillForm 流程重跑一次。
  function waitForAnyInput(username, password, timeoutMs) {
    timeoutMs = timeoutMs || 60000;
    let done = false;
    const cleanup = () => {
      observer.disconnect();
      clearInterval(pollId);
      clearTimeout(timeoutId);
    };
    const tryFill = () => {
      if (done) return;
      if (document.querySelectorAll("input").length === 0) return;
      done = true;
      cleanup();
      console.log("[pwmgr] 偵測到 input 出現,重試 fillForm");
      fillForm(username, password);
    };
    const observer = new MutationObserver(tryFill);
    observer.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true,
    });
    const pollId = setInterval(tryFill, 300);
    const timeoutId = setTimeout(() => {
      if (done) return;
      done = true;
      cleanup();
      console.log("[pwmgr] 等待任何 input 逾時,放棄");
    }, timeoutMs);
  }

  // 找密碼欄位:優先抓真正的 type="password"。
  // 部分網站(如 Kiteworks)刻意把密碼欄位偽裝成 type="text" 並取奇怪的 name(例如
  // "fake-password-element")來閃避瀏覽器/密碼管理工具的自動偵測,這時退回用
  // id / name / class / aria-label / placeholder 裡的關鍵字判斷。
  function findPasswordCandidates() {
    const strict = Array.from(document.querySelectorAll('input[type="password"]')).filter(isUsable);
    if (strict.length > 0) return strict;
    return Array.from(document.querySelectorAll('input[type="text"], input:not([type])'))
      .filter(isUsable)
      .filter(looksLikePasswordField);
  }

  function looksLikePasswordField(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if (el.type === "password") return true;
    if (el.type !== "text" && el.type !== "") return false;
    const haystack = [
      el.id,
      el.name,
      el.className,
      el.getAttribute("aria-label") || "",
      el.placeholder || "",
      el.autocomplete || "",
    ]
      .join(" ")
      .toLowerCase();
    return /password|passwd|密碼|密码|パスワード|비밀번호/.test(haystack);
  }

  // 跟 looksLikePasswordField 對稱:用同樣的關鍵字偵測「看起來像 captcha 輸入框」的元素,
  // fillForm 找 username 候選時要把這些排除,免得密碼 + captcha 頁面被誤把 captcha 欄位
  // 填成 username。
  // 關鍵字跟 CAPTCHA_KEYWORDS 對齊,但排除 password 自己的關鍵字避免重疊。
  function looksLikeCaptchaField(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if (el.type === "password") return false;
    if (el.type !== "text" && el.type !== "") return false;
    const haystack = [
      el.id,
      el.name,
      el.className,
      el.getAttribute("aria-label") || "",
      el.placeholder || "",
      el.autocomplete || "",
    ]
      .join(" ")
      .toLowerCase();
    return /captcha|verify|verification|code|驗證|驗証|認證|认证|確認碼|確認コード|보안|인증/.test(haystack);
  }

  function isUsable(el) {
    if (!el) return false;
    if (el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    return true;
  }

  function indexInForm(form, el) {
    const list = Array.from(form.elements);
    return list.indexOf(el);
  }

  function nearestUsernameInput(pwInput) {
    const candidates = Array.from(
      document.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="search"], input:not([type])'
      )
    ).filter((el) => isUsable(el) && el !== pwInput && !looksLikePasswordField(el) && !looksLikeCaptchaField(el));
    if (candidates.length === 0) return null;
    const pwRect = pwInput.getBoundingClientRect();
    candidates.sort((a, b) => {
      const da = Math.abs(a.getBoundingClientRect().top - pwRect.top);
      const db = Math.abs(b.getBoundingClientRect().top - pwRect.top);
      return da - db;
    });
    return candidates[0];
  }

  function setValue(el, value) {
    // 處理 React 受控元件:用 native setter 觸發 React 的 onChange
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) {
      setter.set.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ===== Captcha 偵測 + 解碼 =================================================
  //
  // 觸發:popup 傳 solveCaptcha 訊息 -> content script 找頁面上的 captcha 圖,
  //       轉 dataURL 送 background -> native host 跑 OCR -> 填回對應 input
  //
  // 設計保守:找不到就回 ok=false,絕不亂填。

  // 「可能是 captcha 圖」的關鍵字——涵蓋中英日韓常見用詞
  const CAPTCHA_KEYWORDS = /(captcha|verify|verification|code|驗證|驗証|認證|认证|確認碼|確認コード|보안|인증)/i;

  function isLikelyCaptchaImg(img) {
    if (!img || img.tagName !== "IMG") return false;
    if (!img.src) return false;
    // 排除 data:image/svg+xml (常是 icon)
    if (img.src.startsWith("data:image/svg")) return false;
    const rect = img.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    // 過濾:大圖(> 500px 寬)通常不是 captcha;太小(< 30px)也跳過
    if (rect.width < 30 || rect.width > 500) return false;
    if (rect.height < 15 || rect.height > 150) return false;
    // 屬性命中關鍵字
    const haystack = [
      img.src,
      img.id || "",
      img.name || "",
      img.alt || "",
      img.className || "",
      img.getAttribute("aria-label") || "",
      img.getAttribute("title") || "",
    ].join(" ");
    if (CAPTCHA_KEYWORDS.test(haystack)) return true;
    // 寬高比:傳統 captcha 寬:高 約 3:1 ~ 6:1,且寬 < 300
    const ratio = rect.width / rect.height;
    if (ratio >= 2 && ratio <= 7 && rect.width <= 300 && rect.height <= 80) {
      // 額外要求:同一 form/parent 內有可疑空 input(下一段 findAssociatedInput 會檢查)
      // 這裡先放寬,讓 findAssociatedInput 決定要不要採信
      return true;
    }
    return false;
  }

  // 找與 captcha 圖相關聯的 text input
  // 規則優先序:
  //   1. 同 form 內、緊接在 img 後面 的第一個可見空 input
  //   2. 視覺上相鄰(垂直 y 座標差距 < 30px)的 input
  //   3. input 的 name/id/placeholder 含 captcha/verify/code 關鍵字
  function findAssociatedInput(img) {
    const allInputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'))
      .filter((el) => isUsable(el) && !looksLikePasswordField(el));
    if (allInputs.length === 0) return null;

    const imgRect = img.getBoundingClientRect();

    // 1. 同 form
    const form = img.closest("form");
    if (form) {
      const formInputs = allInputs.filter((el) => el.closest("form") === form);
      const imgInForm = Array.from(form.querySelectorAll("img")).indexOf(img);
      let best = null;
      let bestDist = Infinity;
      for (const inp of formInputs) {
        // 用 DOM 順序距離當啟發式
        const inFormInputs = Array.from(form.querySelectorAll("input,img"));
        const inpIdx = inFormInputs.indexOf(inp);
        const dist = Math.abs(inpIdx - imgInForm);
        if (dist > 0 && dist < bestDist) {
          best = inp;
          bestDist = dist;
        }
      }
      if (best) return best;
    }

    // 2. 視覺相鄰
    let nearest = null;
    let nearestDist = Infinity;
    for (const inp of allInputs) {
      const r = inp.getBoundingClientRect();
      const dy = Math.abs((r.top + r.height / 2) - (imgRect.top + imgRect.height / 2));
      const dx = Math.abs(r.left - imgRect.right);
      // 同一水平帶、且在 img 右邊(或上下重疊)
      const dist = dy + Math.min(dx, 1000);
      if (dy < 40 && dx < 300 && dist < nearestDist) {
        nearest = inp;
        nearestDist = dist;
      }
    }
    if (nearest) return nearest;

    // 3. 關鍵字
    const kwMatch = allInputs.find((inp) => {
      const h = (inp.name + " " + inp.id + " " + (inp.placeholder || "") + " " + (inp.getAttribute("aria-label") || "")).toLowerCase();
      return CAPTCHA_KEYWORDS.test(h);
    });
    return kwMatch || null;
  }

  // 把 img 轉成 dataURL(用 canvas)。同源圖可;跨源若沒 CORS header 會 taint canvas,
  // 此時 try/catch 改送原始 URL 給 background(用 fetch 帶 cookie 抓,某些網站需要)
  function imgToDataUrl(img) {
    return new Promise((resolve, reject) => {
      try {
        const canvas = document.createElement("canvas");
        // 用 naturalWidth/Height 確保拿到原始解析度
        canvas.width = img.naturalWidth || img.width;
        canvas.height = img.naturalHeight || img.height;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          reject(new Error("canvas context 建立失敗"));
          return;
        }
        ctx.drawImage(img, 0, 0);
        const url = canvas.toDataURL("image/png");
        resolve(url);
      } catch (e) {
        // 通常是 cross-origin taint,退回用 URL
        reject(e);
      }
    });
  }

  // 預載圖片(若是 lazy-loaded:<img loading="lazy">)以確保 naturalWidth/Height 有值
  function preloadImg(img) {
    return new Promise((resolve) => {
      if (img.complete && img.naturalWidth > 0) {
        resolve();
        return;
      }
      const done = () => {
        img.removeEventListener("load", done);
        img.removeEventListener("error", done);
        resolve();
      };
      img.addEventListener("load", done);
      img.addEventListener("error", done);
      // 強制觸發載入(若 src 是空的或 data URL,這個無效但也不會壞)
      if (!img.src) {
        resolve();
      } else {
        // 重新指 src 觸發 reload
        const src = img.src;
        img.src = "";
        img.src = src;
      }
      // 5 秒 timeout
      setTimeout(done, 5000);
    });
  }

  // 對頁面所有「可能是 captcha」的 img 評分,取最高分
  function findBestCaptchaCandidate() {
    const imgs = Array.from(document.querySelectorAll("img")).filter(isLikelyCaptchaImg);
    if (imgs.length === 0) return null;
    // 評分:命中關鍵字 > 緊鄰 input > 寬高比
    const scored = imgs.map((img) => {
      let score = 0;
      const haystack = (img.src + " " + (img.id || "") + " " + (img.name || "") + " " + (img.alt || "") + " " + (img.className || "")).toLowerCase();
      if (CAPTCHA_KEYWORDS.test(haystack)) score += 10;
      const rect = img.getBoundingClientRect();
      const ratio = rect.width / rect.height;
      if (ratio >= 2.5 && ratio <= 5) score += 3;
      // 找得到關聯 input 加分
      if (findAssociatedInput(img)) score += 5;
      return { img, score };
    });
    scored.sort((a, b) => b.score - a.score);
    return scored[0].img;
  }

  // solveCaptcha 主流程(被 background 觸發)
  async function solveCaptcha() {
    const img = findBestCaptchaCandidate();
    if (!img) {
      return { ok: false, code: "NOT_FOUND", error: "頁面上找不到 captcha 圖" };
    }
    const input = findAssociatedInput(img);
    if (!input) {
      return { ok: false, code: "NO_INPUT", error: "找不到對應的輸入框" };
    }
    // 預載 + 轉 dataURL
    await preloadImg(img);
    let dataUrl;
    try {
      dataUrl = await imgToDataUrl(img);
    } catch (e) {
      // 跨源 taint:退回用 URL 讓 native host 抓
      console.warn("[pwmgr] canvas taint, falling back to URL fetch:", e);
      const resp = await chrome.runtime.sendMessage({
        type: "solveCaptcha",
        imageUrl: img.src,
      });
      return await fillCaptchaResponse(input, resp);
    }
    const resp = await chrome.runtime.sendMessage({
      type: "solveCaptcha",
      image: dataUrl,
    });
    return await fillCaptchaResponse(input, resp);
  }

  function fillCaptchaResponse(input, resp) {
    if (resp && resp.ok && resp.text) {
      setValue(input, resp.text);
      console.log("[pwmgr] captcha filled:", resp.text, "confidence:", resp.confidence);
      return { ok: true, text: resp.text, confidence: resp.confidence };
    }
    if (resp && resp.code === "LOW_CONFIDENCE") {
      console.log("[pwmgr] captcha low confidence (std:", resp.std, "beta:", resp.beta, ")—not filling");
      return { ok: false, code: "LOW_CONFIDENCE", std: resp.std, beta: resp.beta };
    }
    if (resp && resp.code === "OCR_UNAVAILABLE") {
      console.warn("[pwmgr] ddddocr 未安裝");
      return { ok: false, code: "OCR_UNAVAILABLE", error: resp.error };
    }
    console.warn("[pwmgr] captcha solve failed:", resp);
    return { ok: false, code: (resp && resp.code) || "UNKNOWN", error: (resp && resp.error) || "unknown" };
  }

  // 註冊來自 background 的 solveCaptcha 訊息
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "solveCaptcha") return false;
    solveCaptcha().then(sendResponse).catch((e) => {
      console.warn("[pwmgr] solveCaptcha threw:", e);
      sendResponse({ ok: false, code: "EXCEPTION", error: String(e) });
    });
    return true; // 保持 sendResponse 開啟(async)
  });

  // 讓 popup 可以詢問「目前頁面有沒有 captcha」來決定要不要顯示按鈕
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "detectCaptcha") return false;
    const img = findBestCaptchaCandidate();
    if (!img) {
      sendResponse({ ok: true, found: false });
      return false;
    }
    const input = findAssociatedInput(img);
    sendResponse({ ok: true, found: !!input, hasImg: true });
    return false;
  });

  // ===== OTP 自動填(Dell 風格 otpBox 多格輸入 / AMD 風格單一 passcode 欄位) =========
  //
  // 偵測目標(Dell):
  //   <input class="otpBox ..." maxlength="1" type="tel" autocomplete="one-time-code">
  //   同一 form-group 內 6 格並排,id 像 otpBoxmfaOtpBox1..6
  //
  // 為何限定 otpBox class(Dell-specific 而非通用):
  //   - user 明確選 Dell 專用:避免誤觸普通 input(如電話分機、數量…)
  //   - 6 格並排 + maxlength=1 + autocomplete=one-time-code 三者同時成立仍可能誤判
  //
  // 排序策略:用 id 尾端數字(Dell 的 otpBoxmfaOtpBox1..6);抓不到就退回 DOM 順序。
  // 大部分網站把這 6 格依序放在同一個 container,DOM 順序通常就是 1→6。
  function findOtpBoxes() {
    const otpBoxes = Array.from(
      document.querySelectorAll('input.otpBox, input[class~="otpBox"], input[class*="otpBox" i]')
    ).filter(isUsable);
    if (otpBoxes.length < 4) return [];
    // 排序:id 末段數字優先(Dell 1..N);fallback DOM 順序
    const getTailNum = (el) => {
      const m = (el.id || "").match(/(\d+)\s*$/);
      return m ? parseInt(m[1], 10) : 0;
    };
    const sorted = otpBoxes.slice().sort((a, b) => {
      const ai = getTailNum(a);
      const bi = getTailNum(b);
      if (ai > 0 && bi > 0) return ai - bi;
      // 至少一邊沒數字 id → 用 DOM 順序(a 是不是 b 的 preceding)
      const pos = a.compareDocumentPosition(b);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
    return sorted;
  }

  // 偵測目標(AMD / Okta 風格單一輸入框):
  //   <input type="text" name="credentials.passcode" id="input80" ...>
  //   跟 Dell 的 6 格分開輸入不同,這種只有一格,完整 6 碼一次填進去。
  //
  // 用 name/id 含 "passcode" 判斷(Okta MFA widget 的慣用命名),
  // 限定 type=text 且非 password,避免誤觸帳密欄位。
  function findOtpSingleInput() {
    const candidates = Array.from(
      document.querySelectorAll(
        'input[name*="passcode" i], input[id*="passcode" i], input[name*="otp" i], input[id*="otp" i]'
      )
    ).filter((el) => isUsable(el) && (el.type === "text" || !el.type) && el.type !== "password");
    return candidates.length > 0 ? [candidates[0]] : [];
  }

  function fillOtp(code) {
    let boxes = findOtpBoxes();
    let singleField = false;
    if (boxes.length === 0) {
      boxes = findOtpSingleInput();
      singleField = boxes.length > 0;
    }
    if (boxes.length === 0) {
      return { ok: false, code: "NOT_FOUND", error: "頁面上找不到 OTP 輸入框(需要 otpBox class 或 passcode 欄位)" };
    }
    // 過濾非數字,OTP 一定是 6 位數;若 native host 回 alphanumeric 也安全(只取數字部分)
    const chars = String(code || "").replace(/\D/g, "").split("");
    if (chars.length === 0) {
      return { ok: false, code: "EMPTY_CODE", error: "code 沒有有效數字" };
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    let filledCount = 0;

    if (singleField) {
      // 單一輸入框:完整 code 一次填入同一格(不像 Dell 一格一碼)
      const el = boxes[0];
      const before = el.value;
      const full = chars.join("");
      setter.call(el, full);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (el.value === full && before !== full) filledCount = 1;
      try { el.focus(); } catch (_) {}
      if (filledCount === 0) {
        return {
          ok: false,
          code: "FILL_NOOP",
          error: "OTP 輸入框已存在值或拒絕寫入(可能已被 user 手動填過)",
          total: 1,
        };
      }
      return { ok: true, filled: 1, total: 1, code: full };
    }

    const n = Math.min(boxes.length, chars.length);
    for (let i = 0; i < n; i++) {
      const el = boxes[i];
      const before = el.value;
      setter.call(el, chars[i]);
      // React / Vue / jQuery 監聽 input / change 才能同步內部 state
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (el.value === chars[i] && before !== chars[i]) filledCount++;
    }
    // focus 最後一個有填的格,符合 user 打完最後一碼的視覺習慣
    const last = boxes[n - 1];
    if (last) {
      try { last.focus(); } catch (_) {}
    }
    if (filledCount === 0) {
      return {
        ok: false,
        code: "FILL_NOOP",
        error: "OTP 輸入框已存在值或拒絕寫入(可能已被 user 手動填過)",
        total: boxes.length,
      };
    }
    return { ok: true, filled: filledCount, total: boxes.length, code: chars.join("") };
  }

  // popup 詢問「目前頁面有沒有 OTP box」→ 決定要不要顯示 OTP 按鈕
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "detectOtp") return false;
    const boxes = findOtpBoxes();
    if (boxes.length >= 4) {
      sendResponse({ ok: true, found: true, count: boxes.length });
      return false;
    }
    const single = findOtpSingleInput();
    sendResponse({ ok: true, found: single.length > 0, count: single.length });
    return false;
  });

  // 注意:實際填入 OTP 已改由 background.js 用 chrome.scripting.executeScript
  // ({allFrames:true}) 直接注入 otpFillFnExecutedScript 執行(見 background.js
  // fillOtp handler 上方註解——sendMessage 廣播給多個 frame 時只有一個回應會被
  // 採用,容易被無關 frame 搶答蓋掉真正的填入結果),這裡不再需要對應的
  // chrome.runtime.onMessage listener。fillOtp() 函式本身還留著給
  // window.__pwmgrDoFill__ 之類的除錯/未來用途參考。

  // 暴露 fillForm 給 executeScript 直接呼叫,繞過 listener race / storage race。
  // 用 unique-ish key 降低被其他 extension 誤觸的風險(並非真正安全隔離)。
  // executeScript 注入的 func 會 await __pwmgrDoFill__ 直到可用,然後呼叫填入。
  try {
    window.__pwmgrDoFill__ = function (username, password) {
      try {
        fillForm(username || "", password || "");
        return true;
      } catch (e) {
        console.warn("[pwmgr] __pwmgrDoFill__ error:", e && e.message || e);
        return false;
      }
    };
  } catch (e) {
    // 某些 page 鎖死 window(不常見),略過
    console.warn("[pwmgr] cannot expose __pwmgrDoFill__:", e && e.message || e);
  }
})();
