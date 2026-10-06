import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ximalaya_grab as core
import ximalaya_grab_gui as gui


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app = gui.GrabApp(self.root)

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_manual_confirmation_and_stop(self):
        self.app._handle("await_user", "我已完成登录")
        self.assertEqual(str(self.app.btn_confirm.cget("state")), "normal")
        self.app._confirm()
        self.assertTrue(self.app.handshake.is_set())
        self.assertEqual(str(self.app.btn_confirm.cget("state")), "disabled")
        self.app._stop()
        self.assertTrue(self.app.stop_flag.is_set())

    def test_invalid_hostname_is_rejected_before_worker(self):
        self.app.var_url.set("https://ximalaya.com.evil.example/album/1")
        with patch.object(gui.messagebox, "showwarning") as warning:
            self.app._start()
        warning.assert_called_once()
        self.assertIsNone(self.app.worker)

    def test_terminal_event_waits_for_cleanup(self):
        worker = Mock()
        worker.is_alive.return_value = True
        self.app.worker = worker
        self.app._set_running(True)
        self.app._handle("done", {"ok": 1, "fail": 0})
        self.assertEqual(str(self.app.btn_start.cget("state")), "disabled")
        worker.is_alive.return_value = False
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(str(self.app.btn_start.cget("state")), "normal")

    def test_close_keeps_event_loop_responsive(self):
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(1))
        worker.start()
        self.app.worker = worker
        ticks = []
        with patch.object(gui.messagebox, "askokcancel", return_value=True):
            started = time.monotonic()
            self.app._on_close()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.stop_flag.is_set())
        self.root.after(10, lambda: ticks.append(True))
        end = time.monotonic() + 0.1
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.005)
        self.assertTrue(ticks)
        release.set()
        worker.join(timeout=2)

    def test_worker_closes_browser_before_driver_and_restores_log(self):
        page = Mock()
        page.query_selector.return_value = None
        ctx = Mock(pages=[page])
        manager = Mock()
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = ctx
        manager.__enter__ = Mock(return_value=pw)
        def exit_manager(*args):
            ctx.close.assert_called_once()
        manager.__exit__ = Mock(side_effect=exit_manager)
        previous = core.log
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"url": "https://www.ximalaya.com/album/1", "profile": Path(temp) / "profile",
                   "out": Path(temp), "count": 2, "start": 1, "force": False, "verbose": False,
                   "login_timeout": 1, "media_wait": 1, "download_timeout": 1, "mp3": False}
            def download(url, dest, *args, **kwargs):
                dest.write_bytes(b"fixture")
                return 7
            with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                    patch.object(core, "start_playback", return_value="https://cdn.example/song.mp3"), \
                    patch.object(core, "current_title", return_value="fixture"), \
                    patch.object(core, "build_headers", return_value={}), \
                    patch.object(core, "download", side_effect=download), \
                    patch.object(core, "goto_next", return_value=None):
                self.app._run(cfg)
            self.assertTrue((Path(temp) / "001_fixture.mp3").exists())
        self.assertIs(core.log, previous)
        events = []
        while True:
            try:
                events.append(self.app.q.get_nowait())
            except queue.Empty:
                break
        done = [payload for kind, payload in events if kind == "done"]
        self.assertEqual(done[0]["ok"], 1)
        self.assertTrue(done[0]["early_end"])
        self.assertFalse(any(kind == "error" for kind, _ in events))


if __name__ == "__main__":
    unittest.main()
