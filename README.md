# PWmgr — 本機密碼管理員

> 公司擋掉 Chrome/Edge 內建密碼記憶功能,自己做一個本機加密保管。
> 密碼存於 Windows Credential Manager(OS 內建 keyring),由 OS 帳號登入保護。
> 附 Chrome/Edge 擴充功能,登入頁一鍵自動填入。

---

## 目錄

1. [功能](#功能)
2. [架構](#架構)
3. [安裝](#安裝)
4. [使用](#使用)
5. [瀏覽器擴充功能](#瀏覽器擴充功能)
6. [風險與限制](#風險與限制)
7. [疑難排解](#疑難排解)
8. [解除安裝](#解除安裝)
9. [開發](#開發)

---

## 功能

- **本機密碼保管** — 密碼儲存在 Windows Credential Manager(由 DPAPI 加密),不外洩到任何雲端。
- **Tk GUI** + **系統 tray** + **全域熱鍵 `Ctrl+Shift+L`** 切換視窗。
- **目前網址高亮** — 擴充功能會把瀏覽器目前 URL 同步給 GUI,符合的條目自動標 ★。
- **Chrome / Edge 擴充功能** — 偵測目前 tab 的 URL、查詢本機 vault、一鍵自動填入帳密。
- **20 秒自動清空剪貼簿** — 複製密碼後 20 秒自動清除,降低肩窺風險。
- **支援多帳號** — 同一網域可存多組帳密(如 `github.com` 個人 + 公司帳)。

## 架構

```
┌──────────────────┐  Native Messaging   ┌────────────────────┐
│ Chrome/Edge ext  │◄──────────────────►│ native_host.py     │
│  MV3 service     │   4-byte LE + JSON  │  - 訊息迴圈         │
│  worker / popup  │                     │  - stdin/stdout    │
│  content script  │                     │  (pythonw.exe)     │
└──────────────────┘                     └─────────┬──────────┘
                                                  │ 讀寫
                                                  ▼
                                        ┌────────────────────┐
                                        │ storage.py         │
                                        │  Windows Credential│
                                        │  Manager +         │
                                        │  index.json (lock) │
                                        └─────────┬──────────┘
                                                  │ 輪詢 current_url.json
                                                  ▼
                                        ┌────────────────────┐
                                        │ app.py (Tk + tray) │
                                        │  Ctrl+Shift+L      │
                                        │  Tray 常駐          │
                                        └────────────────────┘
```

- **GUI 與原生主機不直接互連**,皆透過共用檔案 `index.json` + 檔案鎖通訊。
- **擴充 → GUI** 透過 `current_url.json` 同步(擴充寫,GUI 輪詢)。
- 訊息線框:`[4 bytes LE uint32 length][UTF-8 JSON]`,依 Chrome [native-messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) 規格。

---

## 安裝

### 0. 先決條件

- **Windows 10/11**(Windows Credential Manager 是後端;Linux/macOS 可跑但 keyring 行為不同)
- **Python 3.10+**(已測 3.11)
- **pip**

### 1. 安裝相依套件

```bat
pip install -r requirements.txt
```

需要:`keyring`、`pystray`、`Pillow`。

### 2. 一鍵安裝

```bat
python install.py
```

腳本會:

1. 檢查相依套件
2. 寫入 `bin/native_host.bat`(用 `pythonw.exe` 啟動,無 console 視窗)
3. 寫入 `bin/com.danielhsieh.pwmgr.json`(Chrome/Edge 原生主機 manifest)
4. **互動式**要求貼上 Extension ID(此時請先到 [瀏覽器擴充功能](#瀏覽器擴充功能) 步驟取得 ID)
5. 註冊 Windows 登錄(Edge + Chrome 兩條)
6. 建立開始功能表捷徑

**重跑** `python install.py` 即可更新 Extension ID 或 Python 路徑。

### 打包成 exe 分發(另一台電腦不用裝 Python)

如果要在別台電腦上裝,不想要求對方裝 Python + pip install,可以打包成 exe:

**在這台開發機上打包(一次性):**

```bat
pip install -r requirements-dev.txt
python packaging\build.py
```

會在 `dist\` 產生兩個檔案:

| 檔案 | 用途 | 型態 |
|------|------|------|
| `PWmgr.exe` | GUI + Chrome/Edge 原生訊息主機(用 `--native` 分流,跟 `python -m pwmgr` 行為一致) | windowed,無 console 視窗 |
| `PWmgrSetup.exe` | 互動式安裝 / 解除安裝,取代 `python install.py` / `python uninstall.py` | console |

**在新電腦上安裝:**

1. 把整個專案資料夾複製過去(至少要有 `dist\PWmgr.exe`、`dist\PWmgrSetup.exe`、`chrome_extension\` 這三樣,放在同一層目錄下,例如都丟進 `C:\Tools\PWmgr\`)。
2. 到 `edge://extensions` 或 `chrome://extensions`,開開發人員模式 → 載入未封裝 → 選 `chrome_extension\` → 複製 Extension ID(跟前面「[載入擴充功能](#3-載入擴充功能)」步驟相同)。
3. 雙擊執行 `PWmgrSetup.exe`,依提示貼上 Extension ID。它會寫入 `bin\native_host.bat`(改指向 `PWmgr.exe`,不需要 Python)、原生主機 manifest、Windows 登錄、開始功能表捷徑。
4. 從開始功能表或雙擊 `PWmgr.exe` 啟動 GUI。
5. 解除安裝:開命令提示字元執行 `PWmgrSetup.exe --uninstall`(雙擊沒辦法帶參數)。

**注意:**

- `PWmgr.exe`、`PWmgrSetup.exe` 都沒有數位簽章,第一次執行 Windows SmartScreen 可能跳「Windows 已保護您的電腦」——點「其他資訊」→「仍要執行」即可。這比原本 `.bat` 的警告更醒目,是免裝 Python 的取捨。
- 重跑 `PWmgrSetup.exe` 可以更新 Extension ID(擴充功能重新載入 ID 會變)。
- exe 打包後的 `bin\`、開始功能表捷徑都是相對於 `PWmgr.exe` 所在資料夾建立的,**不要把 `PWmgr.exe` 搬到別的路徑**,不然要重跑一次 `PWmgrSetup.exe`。

### 3. 載入擴充功能

**Edge:**

1. 打開 `edge://extensions`
2. 開啟「**開發人員模式**」(左下角)
3. 點「**載入未封裝**」→ 選 `<專案根目錄>\chrome_extension\`
4. 複製「擴充功能 ID」(32 個英文小寫字母)

**Chrome:** 同上,網址改 `chrome://extensions`。

> ⚠️ **若公司 GPO 鎖住開發人員模式**:擴充功能無法載入,整個瀏覽器整合會失效。
> 但 **GUI 本身仍完全可用**,等於降級為「手動複製密碼」的本機密碼庫。

---

## 使用

### 啟動 GUI

任選一種:

- 開始功能表 → PWmgr
- 工作列右下角 PWmgr 圖示 → 雙擊 / 右鍵「顯示主視窗」
- 命令列:`pythonw.exe -m pwmgr`

啟動後 GUI 預設**隱藏**到系統 tray。按 **Ctrl+Shift+L** 隨時叫出。

### 基本操作

- **新增**:右側表單填好「名稱 / 網域 / 帳號 / 密碼 / 備註」→ 「儲存」
- **編輯**:左側選條目 → 改欄位 → 「儲存」
- **複製帳號** / **複製密碼**:左側選條目 → 按按鈕(20 秒後自動清空剪貼簿)
- **刪除**:左側選條目 → 「刪除」(會跳確認)
- **搜尋**:左上方搜尋框即時過濾(對 name / url / username)
- **快速鍵**:`Ctrl+N` 新增、`Ctrl+S` 儲存、`Delete` 刪除、`Esc` 隱藏到 tray

### 網域格式

「網域」欄位填**註冊網域**,例如 `github.com`。
- `https://api.github.com/login` 也會命中(子網域自動涵蓋)
- `https://www.bbc.co.uk/news` v1 **不命中** `bbc.co.uk`(簡化 eTLD 實作的限制,見 [風險](#風險與限制))

---

## 瀏覽器擴充功能

擴充功能裝好後:

1. 在 Edge / Chrome 開新分頁到 `https://github.com/login`
2. 工具列 PWmgr 圖示上會出現 badge `1`(有 1 筆命中)
3. 點圖示開啟 popup → 看到條目
4. 點條目 → 自動填入 username / password 欄位

**底層流程**:

```
分頁 URL 變動
   │
   ▼  (擴充 background.js)
原生訊息 query(url)
   │
   ▼  (native_host.py)
storage.query_by_url(url)
   │
   ▼
回傳符合的條目
   │
   ▼
擴充 badge 顯示數量
   │
   ▼  (使用者點 popup 條目)
原生訊息 fetch(id) → 拿到密碼
   │
   ▼
content script 注入 username + password
(同步派發 input / change 事件,RPA / React / Vue 表單都生效)
```

### 同步給 GUI

擴充每次偵測到 URL 變動,也會送 `report_url` 給原生主機寫到 `current_url.json`。
GUI 每 2 秒輪詢,把符合的條目標 ★。

---

## 風險與限制

1. **公司政策禁用未封裝擴充功能** ⚠️
   若公司 GPO 鎖住 `chrome://extensions/#developer-mode`,擴充功能無法載入。
   **GUI 仍可用**,降級為手動複製密碼。

2. **無主密碼 = OS 帳號即金鑰**
   任何能登入此 Windows 帳號的人皆可開 GUI 看全部密碼。
   設定 Windows 鎖屏密碼、PIN、指紋是必要的配套。
   若需要更高保護,需改用 PBKDF2 + AES 加密 vault(超出 v1 範圍)。

3. **Credential Manager 2560 byte 上限**
   Windows 對單一 credential 密碼有硬限制(實測 ~2560 bytes)。
   GUI 儲存前會檢查,超長密碼會拒絕並提示。

4. **Defender SmartScreen 可能警告**
   Python 直跑模式下首次執行 `bin\native_host.bat` 可能跳「無法辨識的應用程式」——
   解法:對 `native_host.bat` 按右鍵 → 內容 → 勾「解除封鎖」→ 確定。
   打包成 exe 分發時(見[打包成 exe 分發](#打包成-exe-分發另一台電腦不用裝-python))
   換成對未簽章 exe 的 SmartScreen 警告,一樣點「其他資訊」→「仍要執行」放行即可。

5. **eTLD 簡化實作**
   v1 將 `host` 取倒數兩段視為 eTLD+1。`co.uk` / `com.tw` / `com.au` 等**公開後綴不支援**。
   - `bbc.co.uk` 會被視為「網域 = `co.uk`」,在 `bbc.co.uk` 才命中。
   - 解決:條目存 `bbc.co.uk`,搜尋目標也應是 `bbc.co.uk`(子網域 `www.bbc.co.uk` 不命中)。
   - v1.1 引入 `tldextract` 修正。

6. **Chrome 100ms 逾時**
   原生主機收到訊息後需在 100ms 內回第一個位元組。
   Windows Credential Manager 冷啟動可能較慢,**主機會在背景 thread 預熱**緩解。

7. **DPAPI scope 為目前使用者**
   換 Windows 帳號或新機器 → 全部密碼無法解密。
   v1 沒有「匯出未加密 CSV」功能;v1.1 規劃加上以利移轉。

8. **多重副檔名 ID**
   若你同時在 Edge 與 Chrome 裝擴充,會有兩個不同 ID。
   install.py 接受多個 ID(以空白 / 逗號分隔)。

---

## 疑難排解

### 「工具列圖示沒出現 badge」

1. 確認擴充功能已啟用(Edge / Chrome 的擴充管理頁)
2. 打開 `edge://extensions` → 點 PWmgr → 「檢查視圖:背景頁」→ 看 console 錯誤
3. 確認 `bin\com.danielhsieh.pwmgr.json` 的 `allowed_origins` 包含當前擴充 ID
4. 重新執行 `python install.py`,貼上正確的 ID

### 「點 popup 沒反應 / 顯示忙碌」

- `BUSY` 表示 GUI 正在編輯其他條目(持檔案鎖)。關掉編輯對話框後重試。
- 確認 GUI 沒被防毒軟體阻擋存取 `%LOCALAPPDATA%\pwmgr\`。

### 「複製的密碼貼到網站沒生效」

- 確認網站 username / password 欄位是 `<input type="text">` / `<input type="password">`,不是 contenteditable。
- 某些網站用 React 受控元件,content script 已派發 `input` / `change` 事件;若仍無效請回報網站 + URL。

### 「Credential Manager 看不到 pwmgr 條目」

開「**認證管理員**」(Win+R 輸入 `control keymgr.dll`)→ 「Windows 認證」tab。
條目格式:`pwmgr` / `pwmgr:<id>`。

### 「Defender SmartScreen 警告」

對 `bin\native_host.bat` 與 `pythonw.exe` 都按右鍵 → 內容 → 勾「解除封鎖」。

### 完整解除安裝

```bat
python uninstall.py
```

或手動:
1. 從 tray 退出 GUI
2. `reg delete HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.danielhsieh.pwmgr /f`
3. `reg delete HKCU\Software\Google\Chrome\NativeMessagingHosts\com.danielhsieh.pwmgr /f`
4. 從 Edge / Chrome 擴充頁移除 PWmgr
5. 刪 `bin\`、`%LOCALAPPDATA%\pwmgr\`、Credential Manager 中的 `pwmgr*` 條目

---

## 開發

### 跑測試

```bat
pip install -r requirements-dev.txt
pytest tests/ -v
```

### 模組總覽

| 模組 | 職責 |
|------|------|
| `pwmgr/__main__.py` | 入口分流:`-m pwmgr` → GUI;`-m pwmgr --native` → Chrome 主機 |
| `pwmgr/app.py` | Tk GUI + URL 輪詢 + 剪貼簿管理 |
| `pwmgr/hotkey.py` | Win32 `RegisterHotKey` 全域熱鍵 |
| `pwmgr/tray.py` | pystray 系統列圖示 |
| `pwmgr/native_host.py` | Chrome/Edge 原生訊息主機 |
| `pwmgr/storage.py` | keyring + index.json 抽象層 |
| `pwmgr/models.py` | `PasswordEntry` 資料類別 |
| `pwmgr/matcher.py` | URL 正規化與比對 |
| `pwmgr/ipc.py` | 跨行程檔案鎖(msvcrt / fcntl) |
| `pwmgr/config.py` | 路徑與常數集中管理(含 frozen/exe 判斷) |
| `packaging/build.py` | 跑 PyInstaller,產生 `dist\PWmgr.exe` + `dist\PWmgrSetup.exe` |
| `packaging/entry_gui.py` | `PWmgr.exe` 的 PyInstaller 進入點 |
| `packaging/entry_setup.py` | `PWmgrSetup.exe` 的 PyInstaller 進入點 |

### 測試涵蓋

- `tests/test_matcher.py` — URL 比對 table-driven(30+ cases)
- `tests/test_storage.py` — CRUD、2560 byte 限制、lock 衝突、損壞 index 還原
- `tests/test_native_host.py` — stdin/stdout 模擬,所有訊息類型 + 錯誤路徑
- `tests/test_app_smoke.py` — GUI 構造 / 載入 / 搜尋 / 高亮

### 手動 smoke test

1. `python -m pwmgr` 開 GUI(會縮到 tray)
2. `Ctrl+Shift+L` 彈出
3. 新增 `github.com / alice / p@ssw0rd`
4. Edge 開 `https://github.com/login` → 應看到 badge `1`
5. 點 popup 條目 → 應自動填入

### 訊息協定摘要

| type | request | response |
|------|---------|----------|
| `query` | `{type, url, tabId}` | `{ok, matches:[{id,label,username,url}]}` |
| `fetch` | `{type, id}` | `{ok, entry, password}` |
| `list` | `{type}` | `{ok, entries:[...]}` |
| `save` | `{type, entry, password}` | `{ok, id}` |
| `delete` | `{type, id}` | `{ok}` |
| `report_url` | `{type, url, tabId, ts}` | `{ok}` |
| `ping` | `{type}` | `{ok, pong:true}` |

錯誤碼:`BUSY` / `NOT_FOUND` / `BAD_REQUEST` / `INTERNAL` / `PASSWORD_TOO_LONG` / `NOTES_TOO_LONG`。
