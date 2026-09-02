// content.js — 自動填入 username / password
// 規則:
//   1. 找頁面上所有 input[type=password] (限可見、未被 disabled)
//   2. 每個 password 往上找同一 <form> 中前一個 text/email/tel input 作為 username
//   3. 設值後派發 input + change 事件(React/Vue 受控元件才會更新 state)

(function () {
  "use strict";

  if (window.__pwmgr_filling__) return; // 防止重複
  window.__pwmgr_filling__ = true;

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "fill") {
      try {
        fillForm(msg.username || "", msg.password || "");
      } catch (e) {
        console.warn("[pwmgr] fill error:", e);
      } finally {
        window.__pwmgr_filling__ = false;
      }
    }
  });

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
        console.log("[pwmgr] 找不到密碼輸入欄位");
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
    const candidates = Array.from(
      document.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], input:not([type])'
      )
    ).filter((el) => isUsable(el) && !looksLikePasswordField(el));
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
})();
