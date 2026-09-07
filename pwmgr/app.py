"""Tk GUI——主視窗 + tray + 熱鍵 + current_url 輪詢。"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import tkinter.ttk as ttk
from pathlib import Path
from tkinter import font as tkfont
from typing import Any
from urllib.parse import urlparse

from . import storage
from .config import (
    CLIPBOARD_CLEAR_SECONDS,
    EXTENSION_DIR,
    HOTKEY_KEY,
    HOTKEY_MODIFIERS,
    URL_POLL_INTERVAL_MS,
    app_dir,
)
from .hotkey import GlobalHotkey
from .matcher import matches, registered_domain
from .models import PasswordEntry
from .tray import TrayIcon

APP_TITLE = "PWmgr — 本機密碼管理員"

# tk 的 ttk.Treeview 不直接支援「星號前綴」,我們在 label 文字前手動加。
MATCH_PREFIX = "●  "

# --- 視覺樣式 ------------------------------------------------------------------
# 統一的配色/字型,取代 Tk 預設外觀。

PALETTE = {
    "bg": "#f3f4f7",
    "card": "#ffffff",
    "border": "#e3e5eb",
    "text": "#1c1f26",
    "muted": "#6b7280",
    "accent": "#3461eb",
    "accent_hover": "#2a52c9",
    "accent_active": "#20409e",
    "accent_soft": "#eaf0fe",
    "danger": "#d64545",
    "danger_soft": "#fbe9e9",
    "danger_hover": "#f6d4d4",
    "row_alt": "#f8f9fb",
}

FONT_BASE = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 12, "bold")
FONT_SMALL = ("Segoe UI", 9)


class PwmgrApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1100x780")
        self.root.minsize(960, 640)
        self.root.configure(background=PALETTE["bg"])
        self._set_window_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 隱藏到 tray
        self._tray_visible = True  # 內部標記:目前視窗是否「應該」可見
        self._hotkey_q: queue.Queue = queue.Queue()
        self._hotkey = GlobalHotkey(
            HOTKEY_MODIFIERS, HOTKEY_KEY, self._on_hotkey, self._hotkey_q
        )
        self._tray = TrayIcon(
            on_show=self._show_window, on_quit=self._quit_app
        )

        self._entries: list[PasswordEntry] = []
        self._current_url: str | None = None
        self._selected_id: str | None = None
        self._dirty = False
        self._clipboard_watchdog_after_id: str | None = None

        self._build_ui()
        self._bind_shortcuts()
        self._refresh_entries()
        self._start_url_poll()
        self._hotkey.start()
        self._tray.start()

        # 啟動時先縮到 tray(熱鍵顯示)
        self.root.after(200, self._hide_to_tray)

    # --- UI 建構 ------------------------------------------------------------

    def _set_window_icon(self) -> None:
        icon_path = EXTENSION_DIR / "icons" / "128.png"
        try:
            self._icon_img = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._icon_img)
        except (tk.TclError, OSError):
            pass

    def _setup_style(self) -> ttk.Style:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        p = PALETTE
        self.root.option_add("*Font", FONT_BASE)

        style.configure(".", background=p["bg"], foreground=p["text"], font=FONT_BASE)
        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["card"], relief="solid", borderwidth=1)
        style.configure("Card.TFrame", bordercolor=p["border"])

        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        style.configure("Card.TLabel", background=p["card"], foreground=p["text"])
        style.configure("CardMuted.TLabel", background=p["card"], foreground=p["muted"], font=FONT_SMALL)
        style.configure("Header.TLabel", background=p["card"], foreground=p["text"], font=FONT_HEADER)
        style.configure("Match.TLabel", foreground=p["accent"], background=p["card"])
        style.configure("Warning.TLabel", foreground=p["danger"], background=p["card"])
        style.configure("Status.TLabel", background=p["card"], foreground=p["muted"], font=FONT_SMALL, padding=(10, 6))

        style.configure(
            "TEntry",
            fieldbackground="white",
            bordercolor=p["border"],
            lightcolor=p["border"],
            darkcolor=p["border"],
            padding=6,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", p["accent"])],
            lightcolor=[("focus", p["accent"])],
            darkcolor=[("focus", p["accent"])],
        )

        style.configure(
            "TButton",
            background="#eceef2",
            foreground=p["text"],
            padding=(14, 8),
            borderwidth=0,
            focuscolor="",
        )
        style.map("TButton", background=[("active", "#e1e4ea"), ("pressed", "#d5d8e0")])

        style.configure(
            "Accent.TButton",
            background=p["accent"],
            foreground="white",
            padding=(16, 8),
            borderwidth=0,
            focuscolor="",
        )
        style.map(
            "Accent.TButton",
            background=[("active", p["accent_hover"]), ("pressed", p["accent_active"])],
        )

        style.configure(
            "Danger.TButton",
            background=p["danger_soft"],
            foreground=p["danger"],
            padding=(14, 8),
            borderwidth=0,
            focuscolor="",
        )
        style.map("Danger.TButton", background=[("active", p["danger_hover"])])

        style.configure(
            "Treeview",
            background="white",
            fieldbackground="white",
            foreground=p["text"],
            rowheight=30,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=p["card"],
            foreground=p["muted"],
            font=FONT_BOLD,
            relief="flat",
            padding=(10, 8),
        )
        style.map("Treeview.Heading", background=[("active", p["card"])])
        style.map(
            "Treeview",
            background=[("selected", p["accent_soft"])],
            foreground=[("selected", p["text"])],
        )

        style.configure("TCheckbutton", background=p["card"], foreground=p["text"])
        style.configure("TSeparator", background=p["border"])
        style.configure("Vertical.TScrollbar", background=p["bg"], troughcolor=p["card"], borderwidth=0, arrowsize=12)
        return style

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="新增條目", accelerator="Ctrl+N", command=self._new_entry)
        file_menu.add_command(label="儲存", accelerator="Ctrl+S", command=self._save_entry)
        file_menu.add_separator()
        file_menu.add_command(label="匯入密碼...", command=self._import_entries)
        file_menu.add_command(label="匯出密碼...", command=self._export_entries)
        file_menu.add_separator()
        file_menu.add_command(label="隱藏到 tray", accelerator="Esc", command=self._hide_to_tray)
        file_menu.add_command(label="結束", command=self._do_quit)
        menubar.add_cascade(label="檔案", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="關於 PWmgr", command=self._show_about)
        menubar.add_cascade(label="說明", menu=help_menu)

        self.root.config(menu=menubar)

    def _build_ui(self) -> None:
        self._setup_style()
        self._build_menu()

        # 上方:搜尋 + 目前 URL(卡片樣式)
        top_wrap = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        top_wrap.pack(side=tk.TOP, fill=tk.X)
        top = ttk.Frame(top_wrap, style="Card.TFrame", padding=(14, 12))
        top.pack(fill=tk.X)
        ttk.Label(top, text="🔍", style="Card.TLabel").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_listbox())
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 20))
        ttk.Label(top, text="目前瀏覽:", style="CardMuted.TLabel").pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value="(尚未回報)")
        url_label = ttk.Label(top, textvariable=self.url_var, style="Card.TLabel")
        url_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        # 中間:可拖曳調整的左 list / 右 form
        middle = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        middle.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        # 左 list(卡片)
        left = ttk.Frame(middle, style="Card.TFrame", padding=1)
        list_inner = ttk.Frame(left, style="Card.TFrame", padding=(0, 4))
        list_inner.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            list_inner,
            columns=("label", "username"),
            show="tree headings",  # 顯示樹狀展開指示器 + 兩欄 heading
            selectmode="browse",
        )
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=24, minwidth=24, stretch=False, anchor=tk.W)
        self.tree.heading("label", text="名稱 / 網域")
        self.tree.heading("username", text="帳號")
        self.tree.column("label", width=240, anchor=tk.W)
        self.tree.column("username", width=150, anchor=tk.W)
        self.tree.tag_configure("match", foreground=PALETTE["accent"], font=FONT_BOLD)
        self.tree.tag_configure("odd", background=PALETTE["row_alt"])
        self.tree.tag_configure("even", background="white")
        # 群組 parent row 用,粗體 + 淡背景
        self.tree.tag_configure(
            "group-header",
            font=FONT_BOLD,
            background=PALETTE["row_alt"],
            foreground=PALETTE["muted"],
        )
        sb = ttk.Scrollbar(list_inner, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(1, 0), pady=1)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # 拖拉重排:用 mouse event 模擬 DnD(Tk Treeview 沒原生 DnD)
        self._drag_iid: str | None = None  # 拖拉中正在拖的條目 iid
        self._drag_threshold = 6  # 超過 N 像素才算拖(避免按一下就被當 drag 開頭)
        self._drag_started = False  # 是否真的進入 drag 模式(超過 threshold 才算)
        self._drag_press_y = 0
        self.tree.bind("<ButtonPress-1>", self._on_btn_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_btn_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_btn_release, add="+")
        middle.add(left, weight=3)

        # 右 form(卡片)
        right_outer = ttk.Frame(middle, style="Card.TFrame", padding=1)
        right = ttk.Frame(right_outer, style="Card.TFrame", padding=(18, 16))
        right.pack(fill=tk.BOTH, expand=True)
        ttk.Label(right, text="條目詳情", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 12)
        )
        self._build_form(right)
        middle.add(right_outer, weight=2)

        # 下方按鈕列
        bottom = ttk.Frame(self.root, padding=(16, 0, 16, 14))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="刪除", style="Danger.TButton", command=self._delete_entry).pack(side=tk.LEFT)
        ttk.Button(bottom, text="複製密碼", command=self._copy_password).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text="複製帳號", command=self._copy_username).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text="新增", command=self._new_entry).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(bottom, text="儲存", style="Accent.TButton", command=self._save_entry).pack(side=tk.RIGHT)

        # 狀態列
        self.status_var = tk.StringVar(value="就緒")
        status = ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel", anchor=tk.W)
        status.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_form(self, parent: ttk.Frame) -> None:
        self.label_var = tk.StringVar()
        self.url_entry_var = tk.StringVar()
        self.username_var = tk.StringVar()
        self.group_var = tk.StringVar()
        self.launch_url_var = tk.StringVar()
        self.notes_text: tk.Text
        self.password_var = tk.StringVar()
        self.show_password_var = tk.BooleanVar(value=False)
        # 同一個 form 週期內已彈過的驗證警告 key;成功儲存 / 新增 / 載入時清空
        self._shown_warnings: set[str] = set()

        rows = [
            ("名稱", "label"),
            ("網域", "url_entry"),
            ("帳號", "username"),
            ("群組", "group"),
        ]
        for i, (label, key) in enumerate(rows, start=1):
            ttk.Label(parent, text=label, style="CardMuted.TLabel").grid(
                row=i, column=0, sticky=tk.W, padx=(0, 10), pady=6
            )
            var = getattr(self, f"{key}_var")
            entry = ttk.Entry(parent, textvariable=var)
            entry.grid(row=i, column=1, columnspan=2, sticky=tk.EW, pady=6)
            var.trace_add("write", lambda *_: self._on_form_change())

        # 啟動網址(可選)——inline 「開啟」按鈕
        launch_row = len(rows) + 1  # = 4
        ttk.Label(parent, text="啟動網址", style="CardMuted.TLabel").grid(
            row=launch_row, column=0, sticky=tk.W, padx=(0, 10), pady=6
        )
        launch_entry = ttk.Entry(parent, textvariable=self.launch_url_var)
        launch_entry.grid(row=launch_row, column=1, sticky=tk.EW, pady=6)
        self.launch_open_btn = ttk.Button(
            parent, text="開啟", width=6, command=self._open_launch_url
        )
        self.launch_open_btn.grid(row=launch_row, column=2, sticky=tk.E, padx=(8, 0))
        self.launch_url_var.trace_add("write", lambda *_: self._on_form_change())

        pw_row = launch_row + 1
        ttk.Label(parent, text="密碼", style="CardMuted.TLabel").grid(
            row=pw_row, column=0, sticky=tk.W, padx=(0, 10), pady=6
        )
        self.password_entry = ttk.Entry(parent, textvariable=self.password_var, show="•")
        self.password_entry.grid(row=pw_row, column=1, sticky=tk.EW, pady=6)
        ttk.Checkbutton(
            parent, text="顯示", variable=self.show_password_var, command=self._toggle_password_visibility
        ).grid(row=pw_row, column=2, sticky=tk.W, padx=(8, 0))

        hint_row = pw_row + 1
        ttk.Label(
            parent,
            text="編輯既有條目時此欄一律留白;留空儲存 = 不變更密碼,要改密碼才需輸入新的",
            style="CardMuted.TLabel",
            wraplength=280,
            justify=tk.LEFT,
        ).grid(row=hint_row, column=1, columnspan=2, sticky=tk.W, pady=(0, 4))

        notes_row = hint_row + 1
        ttk.Label(parent, text="備註", style="CardMuted.TLabel").grid(
            row=notes_row, column=0, sticky=tk.NW, padx=(0, 10), pady=6
        )
        notes_wrap = tk.Frame(parent, highlightthickness=1, highlightbackground=PALETTE["border"], background="white")
        notes_wrap.grid(row=notes_row, column=1, columnspan=2, sticky=tk.NSEW, pady=6)
        self.notes_text = tk.Text(
            notes_wrap,
            height=6,
            width=30,
            wrap=tk.WORD,
            relief=tk.FLAT,
            background="white",
            foreground=PALETTE["text"],
            insertbackground=PALETTE["text"],
            font=FONT_BASE,
            padx=8,
            pady=6,
        )
        self.notes_text.pack(fill=tk.BOTH, expand=True)
        self.notes_text.bind("<<Modified>>", self._on_text_modified)
        # 取消 Text widget 的 modified 旗標(預設會在 set 後保持)
        self._notes_modified_binding = False

        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(notes_row, weight=1)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-n>", lambda e: self._new_entry())
        self.root.bind("<Control-s>", lambda e: self._save_entry())
        self.root.bind("<Delete>", lambda e: self._delete_entry())
        self.root.bind("<Escape>", lambda e: self._hide_to_tray())

    # --- 資料 ----------------------------------------------------------------

    def _refresh_entries(self) -> None:
        try:
            self._entries = storage.load_index()
        except Exception as e:
            self._set_status(f"讀取失敗: {e}")
            self._entries = []
        self._refresh_listbox()

    def _refresh_listbox(self) -> None:
        q = self.search_var.get().strip().lower()
        # snapshot 展開狀態——URL poll 觸發 refresh 時保留使用者的開合
        open_states: dict[str, bool] = {
            iid: bool(self.tree.item(iid, "open"))
            for iid in self.tree.get_children("")
            if iid.startswith("group::g::")
        }
        self.tree.delete(*self.tree.get_children(""))
        self._drag_disabled = bool(q)  # 搜尋模式禁用拖拉

        if q:
            # 搜尋模式:扁平列舉所有符合的條目(隱藏群組結構)
            row = 0
            for e in self._entries:
                if q not in e.label.lower() and q not in e.url.lower() \
                        and q not in e.username.lower() and q not in e.group.lower():
                    continue
                self._insert_entry_row(e, parent="", row=row)
                row += 1
            return

        # 一般模式:依群組分層,群組順序採 self._entries 中第一次出現的順序
        NO_GROUP = "__none__"
        seen_groups: dict[str, str] = {}
        counts: dict[str, int] = {}
        for e in self._entries:
            key = (e.group or "").strip() or NO_GROUP
            if key not in seen_groups:
                seen_groups[key] = f"group::g::{key}"
                counts[key] = 0
            counts[key] += 1
        for key, parent_iid in seen_groups.items():
            display = "未分類" if key == NO_GROUP else key
            was_open = open_states.get(parent_iid, True)
            self.tree.insert(
                "", tk.END, iid=parent_iid,
                text="",
                values=(f"{display}  ({counts[key]})", ""),
                tags=("group-header", "even"),
                open=was_open,
            )
        for e in self._entries:
            key = (e.group or "").strip() or NO_GROUP
            self._insert_entry_row(e, parent=seen_groups[key], row=0)

    def _insert_entry_row(self, e: PasswordEntry, *, parent: str, row: int) -> None:
        """插入單筆條目 row,parent 為空字串表示 root(搜尋模式)。"""
        is_match = bool(self._current_url and matches(e.url, self._current_url, ""))
        label = (MATCH_PREFIX if is_match else "") + f"{e.label}  ({e.url})"
        tag = "match" if is_match else ("odd" if row % 2 else "even")
        self.tree.insert(parent, tk.END, iid=e.id, values=(label, e.username), tags=(tag,))

    @staticmethod
    def _is_group_iid(iid: str) -> bool:
        return iid.startswith("group::g::")

    @staticmethod
    def _group_name_from_iid(iid: str) -> str:
        return iid[len("group::g::"):] if iid.startswith("group::g::") else ""

    def _flatten_visible_order(self) -> list[str]:
        """回傳目前顯示中所有 entry iid 的扁平順序(對應 storage array)。

        一般模式:走 root 的群組 parent,逐個 group 收 entries。
        搜尋模式:parent=="" 直接列(沒群組 parent)。
        防呆:任何殘留的 group:: iid 跳過。
        """
        out: list[str] = []
        for parent_iid in self.tree.get_children(""):
            if self._is_group_iid(parent_iid):
                out.extend(self.tree.get_children(parent_iid))
            else:
                out.append(parent_iid)
        return [iid for iid in out if not self._is_group_iid(iid)]

    def _on_btn_press(self, event) -> None:
        """Treeview 按下滑鼠:記住起點 iid,尚未進入 drag 模式(等 motion 過 threshold)。"""
        if getattr(self, "_drag_disabled", False):
            # 搜尋模式禁用拖拉(避免 hidden parent 內條目被誤拖破壞群組結構)
            self._drag_iid = None
            self._drag_started = False
            return
        iid = self.tree.identify_row(event.y)
        self._drag_iid = iid if iid else None
        self._drag_source_is_group = bool(self._drag_iid and self._is_group_iid(self._drag_iid))
        self._drag_started = False
        self._drag_press_y = event.y

    def _on_btn_motion(self, event) -> None:
        """拖動中:超過 threshold 後進入 drag,依滑鼠位置移動 row(支援群組 header)。"""
        if not self._drag_iid:
            return
        if getattr(self, "_drag_disabled", False):
            return
        if not self._drag_started:
            if abs(event.y - self._drag_press_y) < self._drag_threshold:
                return
            self._drag_started = True

        target = self.tree.identify_row(event.y)
        if not target or target == self._drag_iid:
            return

        try:
            if self._drag_source_is_group:
                self._move_group(target)
            else:
                self._move_entry(target)
        except tk.TclError:
            return

    def _move_group(self, target: str) -> None:
        """拖群組 header:整組 subtree 跟著搬。"""
        # 把 target 標準化為 header iid(若指到 entry,改成其 parent)
        if self._is_group_iid(target):
            target_header = target
        else:
            target_header = self.tree.parent(target)
        if not target_header or not self._is_group_iid(target_header):
            return
        if target_header == self._drag_iid:
            return
        # 拖到自己底下任何 entry → no-op(避免自我 sub-tree shift)
        if target_header != target and self.tree.parent(target) == self._drag_iid:
            return
        target_idx = self.tree.index(target_header)
        # header 自己若在 target 之前,要 -1(因為 source 先 detach)
        src_idx = self.tree.index(self._drag_iid)
        if src_idx < target_idx:
            target_idx -= 1
        self.tree.move(self._drag_iid, "", target_idx)
        self._set_status("拖移中…放開滑鼠儲存新順序")

    def _move_entry(self, target: str) -> None:
        """拖 entry:只能在同 parent 內移動,跨群組拒絕。"""
        source_parent = self.tree.parent(self._drag_iid)
        if not source_parent:
            # 搜尋模式(扁平)——理論上 drag_disabled 已擋,但保險起見
            return
        if self._is_group_iid(target):
            # 拖到別群組 header 上 → 拒絕
            self._set_status("不可跨群組移動")
            return
        target_parent = self.tree.parent(target)
        if target_parent != source_parent:
            self._set_status("不可跨群組移動")
            return
        if target == self._drag_iid:
            return
        target_idx = self.tree.index(target)
        src_idx = self.tree.index(self._drag_iid)
        if src_idx < target_idx:
            target_idx -= 1
        self.tree.move(self._drag_iid, source_parent, target_idx)
        self._set_status("拖移中…放開滑鼠儲存新順序")

    def _on_btn_release(self, _event) -> None:
        """放開滑鼠:若有實際拖動,把目前顯示順序存回 storage。"""
        if not self._drag_iid:
            return
        if self._drag_started and not getattr(self, "_drag_disabled", False):
            new_order = self._flatten_visible_order()
            try:
                storage.set_entry_order(new_order)
                self._refresh_entries()
                self._set_status("已儲存新順序")
            except Exception as e:
                self._set_status(f"儲存順序失敗:{e}")
                self._refresh_entries()
        self._drag_iid = None
        self._drag_source_is_group = False
        self._drag_started = False
        self._drag_press_y = 0

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        # 群組 header 是 parent row,不是條目——不應載入表單
        if self._is_group_iid(sel[0]):
            self.tree.selection_remove(sel)
            return
        if self._dirty and not self._confirm_discard_changes():
            # 還原選擇
            if self._selected_id:
                self.tree.selection_set(self._selected_id)
            return
        self._selected_id = sel[0]
        entry = next((e for e in self._entries if e.id == self._selected_id), None)
        if entry is None:
            return
        self._load_entry_to_form(entry)

    def _load_entry_to_form(self, entry: PasswordEntry) -> None:
        self._suppress_change = True
        self.label_var.set(entry.label)
        self.url_entry_var.set(entry.url)
        self.username_var.set(entry.username)
        self.group_var.set(entry.group or "")
        self.launch_url_var.set(entry.launch_url)
        self.password_var.set("")  # 不在記憶體中保留
        self.notes_text.delete("1.0", tk.END)
        self.notes_text.insert("1.0", entry.notes)
        self.notes_text.edit_modified(False)
        # Text 的 <<Modified>> 是排入事件佇列、非同步觸發的——如果這裡把
        # _suppress_change 立刻改回 False,上面 delete/insert 排出的事件會在
        # 下一輪事件迴圈才送達 _on_text_modified,那時旗標已經是 False,
        # 就會被誤判成使用者手動修改而標成「未儲存」。用 after_idle 延後重設,
        # 確保排隊中的 <<Modified>> 事件先被吃掉。
        self.root.after_idle(self._end_suppress_change)
        self._dirty = False
        self._shown_warnings = set()
        self._set_status(f"已載入: {entry.label}")

    def _end_suppress_change(self) -> None:
        self._suppress_change = False

    def _on_form_change(self) -> None:
        if getattr(self, "_suppress_change", False):
            return
        self._dirty = True

    def _on_text_modified(self, _event=None) -> None:
        if getattr(self, "_suppress_change", False):
            self.notes_text.edit_modified(False)
            return
        self._dirty = True
        # 立刻重設旗標,否則 Tk 認為 Text 一直被修改
        self.notes_text.edit_modified(False)

    def _toggle_password_visibility(self) -> None:
        self.password_entry.config(show="" if self.show_password_var.get() else "•")

    # --- 按鈕動作 ------------------------------------------------------------

    def _new_entry(self) -> None:
        if self._dirty and not self._confirm_discard_changes():
            return
        self._suppress_change = True
        self.label_var.set("")
        self.url_entry_var.set(self._suggested_url())
        self.username_var.set("")
        self.group_var.set("")
        self.launch_url_var.set(self._current_url or "")
        self.password_var.set("")
        self.notes_text.delete("1.0", tk.END)
        self.notes_text.edit_modified(False)
        self.root.after_idle(self._end_suppress_change)
        self._selected_id = None
        self.tree.selection_remove(self.tree.selection())
        self._dirty = True
        self._shown_warnings = set()
        self._set_status("新增條目(尚未儲存)")

    def _suggested_url(self) -> str:
        """新增條目時,從目前瀏覽器回報的網址帶出註冊網域,方便使用者不用手動輸入。"""
        if not self._current_url:
            return ""
        try:
            host = urlparse(self._current_url).hostname or ""
        except ValueError:
            return ""
        return registered_domain(host)

    def _save_entry(self) -> None:
        label = self.label_var.get().strip()
        url = self.url_entry_var.get().strip()
        username = self.username_var.get().strip()
        group = self.group_var.get().strip()
        launch_url = self.launch_url_var.get().strip()
        password = self.password_var.get()
        notes = self.notes_text.get("1.0", tk.END).rstrip("\n")

        if not label or not url or not username:
            if "incomplete" not in self._shown_warnings:
                messagebox.showwarning("欄位不完整", "「名稱」、「網域」、「帳號」皆不可空白。")
                self._shown_warnings.add("incomplete")
            self._dirty = False
            self._set_status("欄位不完整,請補齊後再儲存")
            return
        if len(group) > 64:
            key = f"group:{len(group)}"
            if key not in self._shown_warnings:
                messagebox.showwarning("群組過長", f"「群組」最多 64 字,目前 {len(group)}。")
                self._shown_warnings.add(key)
            # 驗證失敗後清掉 dirty:使用者已從警告知道問題,不再被「未儲存」追問;
            # 之後只要繼續編輯,trace_add 會自動把 _dirty 設回 True。
            self._dirty = False
            self._set_status(f"群組過長:{len(group)}/64 字,請縮短後再儲存")
            return
        if launch_url:
            try:
                scheme = urlparse(launch_url).scheme
            except ValueError:
                scheme = ""
            if scheme not in ("http", "https"):
                messagebox.showwarning(
                    "網址格式錯誤",
                    "「啟動網址」若填寫,必須是 http(s) 開頭的完整網址。",
                )
                return
        if not password and not self._selected_id:
            messagebox.showwarning("缺少密碼", "新條目必須設定密碼。")
            return

        try:
            if self._selected_id:
                entry = next((e for e in self._entries if e.id == self._selected_id), None)
                if entry is None:
                    entry = PasswordEntry.new(label, url, username, notes, launch_url=launch_url, group=group)
                    storage.save_entry(entry, password)
                else:
                    entry.label = label
                    entry.url = url
                    entry.username = username
                    entry.notes = notes
                    entry.launch_url = launch_url
                    entry.group = group
                    if password:
                        storage.save_entry(entry, password)
                    else:
                        # 編輯既有條目時密碼欄位一律留白顯示(不把明文載回畫面)。
                        # 這裡若留空就視為「不變更密碼」,只更新其他欄位——
                        # 否則會把 keyring 裡原本的密碼覆蓋成空字串。
                        storage.update_entry(entry)
            else:
                entry = PasswordEntry.new(label, url, username, notes, launch_url=launch_url, group=group)
                storage.save_entry(entry, password)
        except storage.PasswordTooLongError as e:
            messagebox.showerror("密碼過長", str(e))
            return
        except storage.NotesTooLongError as e:
            messagebox.showerror("備註過長", str(e))
            return
        except Exception as e:
            messagebox.showerror("儲存失敗", f"{type(e).__name__}: {e}")
            return

        # 編輯後清空密碼欄,避免殘留明文
        self.password_var.set("")
        self._dirty = False
        self._shown_warnings = set()
        self._refresh_entries()
        self.tree.selection_set(entry.id)
        self._selected_id = entry.id
        self._set_status(f"已儲存: {label}")

    def _delete_entry(self) -> None:
        if not self._selected_id:
            return
        entry = next((e for e in self._entries if e.id == self._selected_id), None)
        if entry is None:
            return
        if not messagebox.askyesno("刪除確認", f"確定刪除「{entry.label}」?\n此動作無法復原。"):
            return
        try:
            storage.delete_entry(entry.id)
        except Exception as e:
            messagebox.showerror("刪除失敗", f"{type(e).__name__}: {e}")
            return
        self._selected_id = None
        self._refresh_entries()
        self._set_status(f"已刪除: {entry.label}")

    def _export_entries(self) -> None:
        if not self._entries:
            messagebox.showinfo("無資料", "目前沒有任何條目可匯出。")
            return
        if not messagebox.askyesno(
            "匯出確認",
            "匯出的檔案將包含所有帳號的明文密碼,請妥善保管、勿上傳到雲端或分享給他人。\n\n是否繼續?",
        ):
            return
        path = filedialog.asksaveasfilename(
            title="匯出密碼",
            defaultextension=".json",
            filetypes=[("JSON 檔案", "*.json"), ("所有檔案", "*.*")],
            initialfile="pwmgr_export.json",
        )
        if not path:
            return
        try:
            count = storage.export_all(Path(path))
        except Exception as e:
            messagebox.showerror("匯出失敗", f"{type(e).__name__}: {e}")
            return
        self._set_status(f"已匯出 {count} 筆條目至 {path}")

    def _import_entries(self) -> None:
        path = filedialog.askopenfilename(
            title="匯入密碼",
            filetypes=[("JSON 檔案", "*.json"), ("所有檔案", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "匯入確認",
            "匯入的條目若 id 與現有條目相同將會覆蓋原本的資料。是否繼續?",
        ):
            return
        try:
            imported, errors = storage.import_all(Path(path))
        except Exception as e:
            messagebox.showerror("匯入失敗", f"{type(e).__name__}: {e}")
            return
        self._refresh_entries()
        if errors:
            messagebox.showwarning(
                "部分匯入失敗",
                f"成功匯入 {imported} 筆,{len(errors)} 筆失敗:\n" + "\n".join(errors[:10]),
            )
        self._set_status(f"已匯入 {imported} 筆條目")

    def _copy_username(self) -> None:
        if not self._selected_id:
            return
        u = self.username_var.get().strip()
        if not u:
            return
        self._copy_to_clipboard(u)
        self._set_status("已複製帳號到剪貼簿")

    def _copy_password(self) -> None:
        if not self._selected_id:
            messagebox.showinfo("未選取", "請先選取一筆條目。")
            return
        pwd = storage.get_password(self._selected_id)
        if not pwd:
            messagebox.showinfo("無密碼", "此條目尚未設定密碼。")
            return
        self._copy_to_clipboard(pwd)
        self._set_status(f"已複製密碼(將於 {CLIPBOARD_CLEAR_SECONDS} 秒後自動清空)")

    def _open_launch_url(self) -> None:
        """以系統預設瀏覽器開啟『啟動網址』欄位的內容。"""
        url = self.launch_url_var.get().strip()
        if not url:
            messagebox.showinfo("無啟動網址", "尚未填寫啟動網址。")
            return
        try:
            scheme = urlparse(url).scheme
        except ValueError:
            scheme = ""
        if scheme not in ("http", "https"):
            messagebox.showwarning(
                "不支援的通訊協定",
                f"目前只支援 http(s),實際為:{scheme or '(無)'}",
            )
            return
        try:
            webbrowser.open(url)
            self._set_status(f"已在瀏覽器開啟:{url}")
        except Exception as e:
            messagebox.showerror("開啟失敗", f"{type(e).__name__}: {e}")

    # --- 剪貼簿 --------------------------------------------------------------

    def _copy_to_clipboard(self, text: str) -> None:
        self._last_clip_text = text
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        # 在 Windows 上需 update 才會實際寫入,否則應用程式退出時內容消失
        self.root.update_idletasks()
        # 排程清空
        if self._clipboard_watchdog_after_id is not None:
            self.root.after_cancel(self._clipboard_watchdog_after_id)
        self._clipboard_watchdog_after_id = self.root.after(
            CLIPBOARD_CLEAR_SECONDS * 1000, self._clear_clipboard
        )

    def _clear_clipboard(self) -> None:
        try:
            current = self.root.clipboard_get()
            # 只在我們放的內容還在時清空——避免清掉使用者自己複製的東西
            if current == self._last_clip_text:
                self.root.clipboard_clear()
                self._set_status("剪貼簿已清空")
        except tk.TclError:
            pass
        self._last_clip_text = None
        self._clipboard_watchdog_after_id = None

    # --- URL 輪詢 ------------------------------------------------------------

    def _start_url_poll(self) -> None:
        self._poll_current_url()
        self.root.after(URL_POLL_INTERVAL_MS, self._start_url_poll)

    def _poll_current_url(self) -> None:
        url = storage.read_current_url()
        if url != self._current_url:
            self._current_url = url
            self.url_var.set(url or "(尚未回報)")
            self._refresh_listbox()

    # --- 熱鍵 / tray ---------------------------------------------------------

    def _on_hotkey(self) -> None:
        # 這個 callback 從 hotkey thread 進來——安全做法:post 到 Tk 主執行緒
        self.root.after(0, self._toggle_window)

    def _toggle_window(self) -> None:
        if self._tray_visible:
            self._hide_to_tray()
        else:
            self._show_window()

    def _show_window(self) -> None:
        self.root.after(0, self._do_show)

    def _do_show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._tray_visible = True

    def _hide_to_tray(self) -> None:
        if self._dirty and not self._confirm_discard_changes():
            return
        self._dirty = False
        self.root.withdraw()
        self._tray_visible = False

    def _on_close(self) -> None:
        # 關 X 不退出,縮到 tray
        self._hide_to_tray()

    def _quit_app(self) -> None:
        # 從 tray 退出
        self._do_quit()

    def _do_quit(self) -> None:
        self._hotkey.stop()
        self._tray.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    # --- 工具 ----------------------------------------------------------------

    def _confirm_discard_changes(self) -> bool:
        return messagebox.askyesno("未儲存變更", "目前編輯尚未儲存,要繼續嗎?")

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _show_about(self) -> None:
        messagebox.showinfo(
            "關於 PWmgr",
            "PWmgr — 本機密碼管理員\n\n"
            "密碼儲存於 Windows Credential Manager(OS keyring)。\n"
            "OS 帳號登入即為認證;不另設主密碼。\n\n"
            f"資料目錄: {app_dir()}",
        )

    # --- mainloop ------------------------------------------------------------

    def run(self) -> int:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._do_quit()
        return 0


def run() -> int:
    return PwmgrApp().run()


if __name__ == "__main__":
    sys.exit(run())
