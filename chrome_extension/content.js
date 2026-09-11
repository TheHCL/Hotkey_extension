// content.js — 自動填入 username / password,以及 captcha
// 規則:
//   1. 找頁面上所有 input[type=password] (限可見、未被 disabled)
//   2. 每個 password 往上找同一 <form> 中前一個 text/email/tel input 作為 username
//   3. 設值後派發 input + change 事件(React/Vue 受控元件才會更新 state)
//   4. captcha 偵測:由 popup 觸發,找頁面上疑似 captcha 的 <img> 與對應 input,
//      把圖轉 dataURL 送 native host 跑 OCR,結果填回 input(失敗就甚麼都不做)

(function () {
  "use strict";

  if (window.__pwmgr_filling__) return; // 防止重複
  window.__pwmgr_filling__ = true;

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "fill") {
      console.log("[pwmgr] content received fill", msg.username ? "(has username)" : "(no username)");
      try {
        fillForm(msg.username || "", msg.password || "");
      } catch (e) {
        console.warn("[pwmgr] fill error:", e);
      } finally {
        window.__pwmgr_filling__ = false;
      }
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

  // 啟動後主動問 background 有沒有 pending fill(解決 MV3 sendMessage 與
  // content script listener attach 之間的 race condition)
  setTimeout(function () {
    // 從 window 或 message sender 拿 tabId(content script 沒有直接的 tabId API,
    // 改由 background 從 sender.tab.id 推斷;若 sender 沒給,我們仍送但不帶 tabId,
    // background 會從 storage 比對)
    chrome.runtime.sendMessage({ type: "claimPendingFill" }, function (resp) {
      if (resp && resp.ok && resp.username != null) {
        console.log("[pwmgr] content claimed pending fill");
        try {
          fillForm(resp.username || "", resp.password || "");
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
    });
  }, 200);

  function fillForm(username, password) {
    const passwordInputs = findPasswordCandidates();
    if (passwordInputs.length === 0) {
      // 部分網站(如 Google/Microsoft)採兩步驟登入:先填帳號、按「繼續」後密碼欄位才出現。
      // 先填帳號,再等待密碼欄位出現後自動補填。
      const filledUsername = fillUsernameOnly(username);
      if (filledUsername) {
        console.log("[pwmgr] 尚無密碼欄位,已先填帳號,等待密碼欄位出現");
        waitForPasswordField(username, password);
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
      const form = pwInput.closest("form");
      let userInput = null;
      if (form) {
        const candidates = Array.from(form.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="search"], input:not([type])'));
        // 取 password 之前的最後一個
        for (const c of candidates) {
          if (!isUsable(c) || looksLikePasswordField(c)) continue;
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
      }
      filled++;
    }
    console.log(`[pwmgr] 已填入 ${filled} 個密碼欄位`);
  }

  function fillUsernameOnly(username) {
    const allInputs = Array.from(document.querySelectorAll("input"));
    const usableList = allInputs.filter((el) => isUsable(el) && !looksLikePasswordField(el));
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
    ).filter((el) => isUsable(el) && el !== pwInput && !looksLikePasswordField(el));
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
})();
