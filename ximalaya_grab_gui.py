#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
喜马拉雅音频抓取 —— 图形界面版

核心逻辑复用同目录的 ximalaya_grab.py（抓取、去重、下载全部一样），
这里只负责界面、线程调度和进度显示。

运行：
    python ximalaya_grab_gui.py

依赖：
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ximalaya_grab as core  # noqa: E402

PAD = 8
APP_TITLE = "喜马拉雅音频抓取"
HINT = "仅保存你有权下载且账号可正常播放的内容；不提供解密或访问权限绕过。"
PROFILE_DIR = Path(__file__).resolve().parent / ".xm_profile"


StopRequested = core.StopRequested


def human(size: float) -> str:
    if size >= 1048576:
        return f"{size / 1048576:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{int(size)} B"


def normalize_url(text: str) -> str:
    text = text.strip().strip("<>\"' ")
    if not text:
        return ""
    if text.isdigit():
        return f"https://www.ximalaya.com/album/{text}"
    if text.startswith("/"):
        return "https://www.ximalaya.com" + text
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text


def to_int(var: tk.Variable, default: int, low: int = 1) -> int:
    """Spinbox 被清空或填了非数字时不要炸掉整个界面。"""
    try:
        return max(low, int(var.get()))
    except Exception:  # noqa: BLE001
        return default


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


class GrabApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.handshake = threading.Event()   # 用户手动点「我已完成登录 / 继续」
        self.worker: threading.Thread | None = None
        self.closing = False
        self.rows: dict[int, str] = {}
        self.total = 0
        self.finished = 0
        self.out_dir = Path.cwd()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain)

    # ---------------- 界面 ----------------

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1000x720")
        self.root.minsize(840, 600)

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", foreground="#0b5cad")
        style.configure("Big.TButton", padding=(14, 6))

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        form = ttk.LabelFrame(outer, text="任务设置", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.var_url = tk.StringVar()
        self.var_out = tk.StringVar(value=str(Path.cwd() / "downloads"))
        self.var_count = tk.IntVar(value=30)
        self.var_start = tk.IntVar(value=1)
        self.var_mp3 = tk.BooleanVar(value=False)
        self.var_force = tk.BooleanVar(value=False)
        self.var_verbose = tk.BooleanVar(value=True)
        self.var_login_wait = tk.IntVar(value=300)
        self.var_media_wait = tk.IntVar(value=15)
        self.var_dl_timeout = tk.IntVar(value=180)

        ttk.Label(form, text="专辑 / 单曲地址").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(form, textvariable=self.var_url).grid(
            row=0, column=1, sticky="ew", padx=6, pady=(0, 6)
        )
        ttk.Button(form, text="粘贴", width=8, command=self._paste).grid(
            row=0, column=2, pady=(0, 6)
        )

        ttk.Label(form, text="保存到").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(form, textvariable=self.var_out).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(0, 6)
        )
        ttk.Button(form, text="浏览…", width=8, command=self._browse).grid(
            row=1, column=2, pady=(0, 6)
        )

        opts = ttk.Frame(form)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(opts, text="抓取数量").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=9999, width=6, textvariable=self.var_count).pack(
            side="left", padx=(6, 16)
        )
        ttk.Label(opts, text="起始编号").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=9999, width=6, textvariable=self.var_start).pack(
            side="left", padx=(6, 16)
        )
        ttk.Checkbutton(opts, text="转成 MP3", variable=self.var_mp3).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="已存在也重下", variable=self.var_force).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="显示每条请求", variable=self.var_verbose).pack(side="left")

        adv = ttk.Frame(form)
        adv.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        for label, var, lo, hi in (
            ("登录等待(秒)", self.var_login_wait, 30, 1800),
            ("等待请求(秒)", self.var_media_wait, 3, 120),
            ("单文件超时(秒)", self.var_dl_timeout, 30, 1800),
        ):
            ttk.Label(adv, text=label).pack(side="left")
            ttk.Spinbox(adv, from_=lo, to=hi, width=6, textvariable=var).pack(
                side="left", padx=(6, 16)
            )

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(10, 6))

        self.btn_start = ttk.Button(
            bar, text="开始抓取", style="Big.TButton", command=self._start
        )
        self.btn_start.pack(side="left")

        self.btn_stop = ttk.Button(
            bar, text="停止", style="Big.TButton", command=self._stop, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=8)

        self.btn_open = ttk.Button(bar, text="打开文件夹", command=self._open_folder)
        self.btn_open.pack(side="left")

        self.btn_confirm = ttk.Button(
            bar, text="我已完成登录", style="Big.TButton",
            command=self._confirm, state="disabled",
        )
        self.btn_confirm.pack(side="left", padx=8)

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status, style="Status.TLabel").pack(
            side="left", padx=16
        )

        ttk.Label(outer, text=HINT, style="Hint.TLabel").pack(fill="x", pady=(0, 8))

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)

        tree_frame = ttk.Frame(panes)
        cols = ("idx", "title", "status", "size")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=12)
        for col, text, width, anchor in (
            ("idx", "#", 54, "center"),
            ("title", "曲目标题", 560, "w"),
            ("status", "状态", 100, "center"),
            ("size", "大小", 110, "e"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "title"))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("fail", foreground="#c62828")
        self.tree.tag_configure("work", foreground="#0b5cad")
        self.tree.tag_configure("skip", foreground="#888888")
        panes.add(tree_frame, weight=3)

        log_frame = ttk.LabelFrame(panes, text="日志", padding=4)
        self.log_text = ScrolledText(
            log_frame, height=8, wrap="word", font=("Consolas", 9),
            background="#fbfbfb", borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        panes.add(log_frame, weight=2)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.var_progress = tk.StringVar(value="0 / 0")
        self.pbar = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.var_progress, width=16, anchor="e").pack(
            side="left", padx=(10, 0)
        )

    # ---------------- 小工具 ----------------

    def _paste(self) -> None:
        try:
            self.var_url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择保存目录")
        if chosen:
            self.var_out.set(chosen)

    def _open_folder(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.out_dir))  # noqa: S606  - Windows 桌面应用
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"打不开文件夹：{exc}")

    def _confirm(self) -> None:
        """自动检测没认出来时，用户手动放行。"""
        self.handshake.set()
        self.btn_confirm.configure(state="disabled")
        self._append_log("  ✓ 已手动确认，继续下一步。")
        self.var_status.set("手动确认，继续…")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _snapshot(self) -> dict:
        return {
            "url": normalize_url(self.var_url.get()),
            "out": Path(self.var_out.get().strip() or "downloads"),
            "count": to_int(self.var_count, 30),
            "start": to_int(self.var_start, 1),
            "mp3": bool(self.var_mp3.get()),
            "force": bool(self.var_force.get()),
            "verbose": bool(self.var_verbose.get()),
            "login_timeout": to_int(self.var_login_wait, 300, 10),
            "media_wait": to_int(self.var_media_wait, 15, 3),
            "download_timeout": to_int(self.var_dl_timeout, 180, 30),
            "profile": PROFILE_DIR,
        }

    # ---------------- 队列 ----------------

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._append_log(payload)
        elif kind == "status":
            self.var_status.set(payload)
        elif kind == "plan":
            self.total = payload["count"]
            self.finished = 0
            self.pbar.configure(maximum=self.total, value=0)
            self.var_progress.set(f"0 / {self.total}")
        elif kind == "track":
            self._upsert_row(payload)
        elif kind == "fileprogress":
            idx, got, total = payload
            item = self.rows.get(idx)
            if item:
                pct = f"{got * 100 // total:3d}%" if total else ""
                self.tree.set(item, "size", f"{human(got)} {pct}".strip())
        elif kind == "await_user":
            self.btn_confirm.configure(text=payload, state="normal")
        elif kind == "await_end":
            self.btn_confirm.configure(text="我已完成登录", state="disabled")
        elif kind == "done":
            if self.worker and self.worker.is_alive():
                self.root.after(100, lambda: self._handle(kind, payload))
                return
            self._finish(payload)
        elif kind == "error":
            if self.worker and self.worker.is_alive():
                self.root.after(100, lambda: self._handle(kind, payload))
                return
            if not self.closing:
                messagebox.showerror(APP_TITLE, payload)
            self.var_status.set("出错")
            self._set_running(False)

    def _upsert_row(self, info: dict) -> None:
        idx = info["idx"]
        tag = {"完成": "done", "失败": "fail", "下载中": "work", "已存在": "skip"}.get(
            info["status"], ""
        )
        item = self.rows.get(idx)
        if item is None:
            item = self.tree.insert(
                "", "end",
                values=(idx, info["title"], info["status"], info.get("size", "")),
                tags=(tag,) if tag else (),
            )
            self.rows[idx] = item
            self.tree.see(item)
        else:
            self.tree.set(item, "status", info["status"])
            if info.get("size"):
                self.tree.set(item, "size", info["size"])
            if info.get("title"):
                self.tree.set(item, "title", info["title"])
            self.tree.item(item, tags=(tag,) if tag else ())
            self.tree.see(item)
        if info["status"] in ("完成", "失败", "已存在"):
            self.finished += 1
            self.pbar.configure(value=self.finished)
            self.var_progress.set(f"{self.finished} / {self.total}")

    def _finish(self, summary: dict) -> None:
        self.pbar.configure(value=self.finished)
        self.var_progress.set(f"{self.finished} / {self.total}")
        if summary.get("stopped"):
            self.var_status.set(f"已停止，本次完成 {summary.get('ok', 0)} 首")
        elif summary.get("error"):
            self.var_status.set(f"结束：{summary['error']}")
        else:
            self.var_status.set(
                f"完成 {summary.get('ok', 0)} 首，失败 {summary.get('fail', 0)} 首"
                + ("；未捕获下一首，提前结束" if summary.get("early_end") else "")
                + (f"；MP3 转换失败 {summary['conversion_fail']} 首" if summary.get("conversion_fail") else "")
            )
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if not running:
            self.btn_confirm.configure(text="我已完成登录", state="disabled")

    # ---------------- 控制 ----------------

    def _start(self) -> None:
        if self.closing or (self.worker and self.worker.is_alive()):
            return
        cfg = self._snapshot()
        if not cfg["url"]:
            messagebox.showwarning(APP_TITLE, "先填专辑或单曲地址。")
            return
        try:
            core.validate_url(cfg["url"])
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        self.var_url.set(cfg["url"])
        self.out_dir = cfg["out"].resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cfg["out"] = self.out_dir

        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.finished = 0
        self.total = cfg["count"]
        self.pbar.configure(maximum=self.total, value=0)
        self.var_progress.set(f"0 / {self.total}")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._append_log(f"目标：{cfg['url']}")
        self._append_log(f"保存到：{self.out_dir}")
        self.var_status.set("启动浏览器…")
        self.stop_flag.clear()
        self.handshake.clear()
        self._set_running(True)

        self.worker = threading.Thread(target=self._run, args=(cfg,), daemon=False)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_flag.set()
        self.var_status.set("正在停止…")
        self.btn_stop.configure(state="disabled")

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(APP_TITLE, "任务还在跑，确定退出？"):
                return
            self.closing = True
            self._stop()
            self.btn_start.configure(state="disabled")
            self.btn_confirm.configure(state="disabled")
            self.var_status.set("正在停止并关闭浏览器…")
            self.root.after(100, self._wait_for_close)
        else:
            self.root.destroy()

    def _wait_for_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._wait_for_close)
        else:
            self.root.destroy()

    # ---------------- 后台线程 ----------------

    def _run(self, cfg: dict) -> None:
        q = self.q
        stopped = self.stop_flag.is_set
        hs = self.handshake
        ok = 0
        bad = 0
        conversion_bad = 0
        ctx = None

        def log(msg: str = "") -> None:
            q.put(("log", str(msg)))

        def ask_user(button_text: str, timeout_s: float) -> bool:
            """亮起手动按钮并等用户点；返回 False 表示超时或用户停止。"""
            hs.clear()
            q.put(("await_user", button_text))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if stopped():
                    break
                if hs.is_set():
                    hs.clear()
                    q.put(("await_end", None))
                    return True
                page.wait_for_timeout(300)
            q.put(("await_end", None))
            return False

        previous_log = core.log
        core.log = log  # 让核心脚本里的日志也进 GUI 日志框

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                q.put(("status", "启动浏览器…"))
                ctx = pw.chromium.launch_persistent_context(
                    str(Path(cfg["profile"]).resolve()),
                    headless=False,
                    user_agent=core.UA,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    args=[
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    cap = core.MediaCapture(verbose=cfg["verbose"])
                    page.on("response", cap.on_response)

                    def wait_for_login() -> bool:
                        """自动检测解锁 + 手动兜底。返回 False 表示用户点了停止。

                        「登录等待(秒)」只是软超时：超过之后不再报错退出，而是继续
                        等你点【我已完成登录】或【停止】，免得自动检测不准时卡死。
                        """
                        deadline = time.time() + cfg["login_timeout"]
                        asked = False
                        warned = False
                        while True:
                            if stopped():
                                q.put(("await_end", None))
                                return False
                            if hs.is_set():
                                hs.clear()
                                q.put(("await_end", None))
                                log("  ✓ 已按你的手动确认继续。")
                                return True
                            try:
                                locked = page.query_selector(core.SEL_LOCK) is not None
                            except Exception:  # noqa: BLE001
                                locked = True
                            if not locked:
                                if asked:
                                    log("  ✓ 检测到播放器已解锁，继续。")
                                q.put(("await_end", None))
                                return True
                            if not asked:
                                asked = True
                                q.put(("await_user", "我已完成登录"))
                                q.put(("status", "等你在浏览器窗口里登录…"))
                                log("")
                                log("  ⚠ 播放器当前是未登录状态（页面元素带 is-locked）。")
                                log("    请在自动打开的浏览器窗口里登录，登录完会自动继续。")
                                log("    要是你明明登了、这里却没认出来，直接点【我已完成登录】放行。")
                            if not warned and time.time() > deadline:
                                warned = True
                                log(f"  · 已超过设定的 {int(cfg['login_timeout'])} 秒。继续等你操作："
                                    "登录后点【我已完成登录】，或者点【停止】。")
                            page.wait_for_timeout(1000)

                    q.put(("status", "打开页面…"))
                    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(4000)

                    q.put(("status", "检查登录状态…"))
                    if not wait_for_login():
                        q.put(("done", {"stopped": True, "ok": 0, "fail": 0}))
                        return

                    q.put(("status", "播放第一首…"))
                    is_album = "/album/" in cfg["url"]
                    media_url = core.start_playback(
                        page, cap, cfg["media_wait"], is_album, should_stop=stopped
                    )
                    while not media_url and not stopped():
                        log("")
                        log("  ⚠ 没捕获到音频请求。请在浏览器窗口里确认这一首点得动、能出声，")
                        log("    然后点【我已确认能播放，重试】。工具只能保存当前账号可播放的媒体。")
                        q.put(("status", "没抓到请求，等你确认…"))
                        if not ask_user("我已确认能播放，重试", 180):
                            break
                        log("  · 重试播放…")
                        media_url = core.wait_for_media(page, cap, None, 0.5, stopped)
                        if not media_url:
                            media_url = core.start_playback(
                                page, cap, cfg["media_wait"], is_album, should_stop=stopped
                            )
                    if not media_url:
                        if stopped():
                            q.put(("done", {"stopped": True, "ok": 0, "fail": 0}))
                        else:
                            q.put(("error",
                                   "没捕获到音频请求。\n\n"
                                   "依次检查：\n"
                                   "1) 浏览器窗口里已经登录，点播放器能真的出声；\n"
                                   "2) 当前账号具有这一首的播放权限；\n"
                                   "3) 网络慢的话把「等待请求(秒)」调大；\n"
                                   "4) 自动检测登录不准的话，点【我已完成登录】手动放行。"))
                        return

                    index = cfg["start"]
                    last_index = cfg["start"] + cfg["count"]
                    q.put(("status", "抓取中…"))

                    while media_url and index < last_index:
                        if stopped():
                            break
                        title = core.current_title(page) or f"track_{index:03d}"
                        dest = cfg["out"] / f"{index:03d}_{core.safe_name(title)}{core.media_suffix(media_url)}"
                        q.put(("track", {"idx": index, "title": title, "status": "下载中"}))
                        q.put(("status", f"第 {index} 首：{title}"))

                        if dest.exists() and not cfg["force"]:
                            q.put(("track", {
                                "idx": index, "title": title, "status": "已存在",
                                "size": human(dest.stat().st_size),
                            }))
                            ok += 1
                        else:
                            last_push = [0.0]

                            def on_progress(got, total, _idx=index, _last=last_push):
                                if stopped():
                                    raise StopRequested
                                now = time.time()
                                if now - _last[0] >= 0.25 or (total and got >= total):
                                    _last[0] = now
                                    q.put(("fileprogress", (_idx, got, total)))

                            try:
                                headers = core.build_headers(ctx, page, media_url)
                                size = core.download(
                                    media_url, dest, headers, cfg["download_timeout"],
                                    on_progress=on_progress,
                                    should_stop=stopped,
                                )
                                q.put(("track", {
                                    "idx": index, "title": title,
                                    "status": "完成", "size": human(size),
                                }))
                                ok += 1
                                if cfg["mp3"]:
                                    q.put(("status", f"转 MP3：{title}"))
                                    if not core.convert_to_mp3(dest, should_stop=stopped):
                                        conversion_bad += 1
                            except StopRequested:
                                log("  · 已停止；未完成文件已清理，已完成的原文件保留")
                                break
                            except Exception as exc:  # noqa: BLE001
                                log(f"        ✗ 下载失败：{exc}")
                                q.put(("track", {
                                    "idx": index, "title": title, "status": "失败",
                                    "size": str(exc)[:40],
                                }))
                                bad += 1

                        index += 1
                        if index >= last_index or stopped():
                            break
                        cap.reset()      # ← 每首清空缓冲，媒体列表不堆积
                        q.put(("status", f"第 {index} 首…"))
                        media_url = core.goto_next(
                            page, cap, media_url, cfg["media_wait"], should_stop=stopped
                        )

                    q.put(("done", {
                        "stopped": stopped(), "ok": ok, "fail": bad,
                        "out": str(cfg["out"]),
                        "early_end": not media_url and index < last_index,
                        "conversion_fail": conversion_bad,
                    }))
                finally:
                    ctx.close()
        except StopRequested:
            q.put(("done", {"stopped": True, "ok": ok, "fail": bad}))
        except Exception as exc:  # noqa: BLE001
            log("")
            log(traceback.format_exc(limit=4))
            q.put(("error", f"运行出错：{exc}"))
        finally:
            core.log = previous_log


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    GrabApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
