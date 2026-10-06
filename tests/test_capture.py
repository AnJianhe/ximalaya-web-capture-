import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ximalaya_grab as core


BODY = b"local-media-fixture" * 8192


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = BODY
        self.send_response(206 if self.path == "/range.m4a" else 200)
        self.send_header("Content-Type", "text/html" if self.path == "/html.m4a" else "audio/mp4")
        self.send_header("Content-Length", len(payload) + (1 if self.path == "/short.m4a" else 0))
        if self.path == "/range.m4a":
            self.send_header("Content-Range", "bytes 1-9/20")
        self.end_headers()
        if self.path == "/stall.m4a":
            self.wfile.write(b"x")
            self.wfile.flush()
            self.server.started.set()
            self.server.release.wait(8)
        else:
            self.wfile.write(payload)
        self.close_connection = True


class CaptureTests(unittest.TestCase):
    def test_media_deduplication_and_both_buffers_bounded(self):
        cap = core.MediaCapture(verbose=False)
        for i in range(100):
            cap.on_response(SimpleNamespace(
                request=SimpleNamespace(resource_type="media"), status=200,
                url=f"https://cdn.example/{i % 20}.m4a?range={i}"))
        self.assertEqual(len(cap.urls), 8)
        self.assertEqual(len(cap._seen), 8)
        cap.reset()
        self.assertEqual(cap.urls, [])
        self.assertEqual(cap._seen, set())

    def test_failed_responses_and_fragments_are_ignored(self):
        cap = core.MediaCapture(verbose=False)
        for status, kind, suffix in ((403, "media", "m4a"), (200, "xhr", "mp3"), (200, "media", "m4s")):
            cap.on_response(SimpleNamespace(status=status, request=SimpleNamespace(resource_type=kind),
                                           url=f"https://cdn.example/a.{suffix}"))
        self.assertEqual(cap.urls, [])

    def test_url_domain_validation(self):
        for url in ("https://www.ximalaya.com/album/1", "https://ximalaya.com/sound/1"):
            self.assertEqual(core.validate_url(url), url)
        for url in ("https://ximalaya.com.evil.example/", "https://evil.example/?ximalaya.com",
                    "file://ximalaya.com/a", "https://user:pass@www.ximalaya.com/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                core.validate_url(url)

    def test_filename_and_actual_media_suffix(self):
        self.assertEqual(core.safe_name('a/b:c?d'), "a_b_c_d")
        self.assertEqual(core.media_suffix("https://cdn.example/song.MP3?token=secret"), ".mp3")
        self.assertEqual(core.url_key("https://cdn.example/song.mp3?token=secret#x"),
                         "https://cdn.example/song.mp3")
        with self.assertRaises(ValueError):
            core.media_suffix("https://cdn.example/segment.m4s")

    def test_cookie_scope_is_media_host(self):
        ctx = Mock()
        ctx.cookies.return_value = [{"name": "media_cookie", "value": "fixture"}]
        url = "https://cdn.example/song.m4a"
        headers = core.build_headers(ctx, SimpleNamespace(url="https://www.ximalaya.com/album/1"), url)
        ctx.cookies.assert_called_once_with([url])
        self.assertEqual(headers["Cookie"], "media_cookie=fixture")

    def test_next_retry_does_not_skip_another_track(self):
        cap = core.MediaCapture(verbose=False)
        with patch.object(core, "click_sel", return_value=True) as click, \
                patch.object(core, "wait_for_media", side_effect=[None, "https://cdn.example/next.m4a"]), \
                patch.object(core, "log"):
            self.assertEqual(core.goto_next(Mock(), cap, "https://cdn.example/prev.m4a", 1),
                             "https://cdn.example/next.m4a")
        self.assertEqual([call.args[1] for call in click.call_args_list], [core.SEL_NEXT, core.SEL_PLAY])

    def test_invalid_cli_quantity(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            core.parse_args(["--url", "https://www.ximalaya.com/album/1", "--count", "0"])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_conversion_failure_has_nonzero_exit(self):
        page = Mock()
        ctx = Mock(pages=[page])
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = ctx
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(core, "sync_playwright", return_value=contextlib.nullcontext(pw)), \
                patch.object(core, "ensure_login", return_value=True), \
                patch.object(core, "start_playback", return_value="https://cdn.example/song.m4a"), \
                patch.object(core, "current_title", return_value="fixture"), \
                patch.object(core, "build_headers", return_value={}), \
                patch.object(core, "download", return_value=3), \
                patch.object(core, "convert_to_mp3", return_value=False), \
                patch.object(core, "log"), contextlib.redirect_stdout(io.StringIO()):
            result = core.main(["--url", "https://www.ximalaya.com/album/1", "--count", "1",
                                "--out", temp, "--profile", temp, "--mp3"])
        self.assertEqual(result, 1)
        ctx.close.assert_called_once()


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.started = threading.Event()
        cls.server.release = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "saved.m4a"

    def test_complete_download_and_atomic_rename(self):
        size = core.download(self.base + "/ok.m4a", self.dest, {}, 10)
        self.assertEqual(size, len(BODY))
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertFalse(self.dest.with_name("saved.m4a.part").exists())

    def test_truncated_download_preserves_existing_file(self):
        self.dest.write_bytes(b"existing")
        with self.assertRaises(ValueError):
            core.download(self.base + "/short.m4a", self.dest, {}, 10)
        self.assertEqual(self.dest.read_bytes(), b"existing")
        self.assertFalse(self.dest.with_name("saved.m4a.part").exists())

    def test_html_and_partial_range_rejected(self):
        for endpoint in ("/html.m4a", "/range.m4a"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                core.download(self.base + endpoint, self.dest, {}, 10)
            self.assertFalse(self.dest.exists())
            self.assertFalse(self.dest.with_name("saved.m4a.part").exists())

    def test_cancel_during_transfer_removes_partial(self):
        stop = threading.Event()
        def progress(got, total):
            stop.set()
        with self.assertRaises(core.StopRequested):
            core.download(self.base + "/ok.m4a", self.dest, {}, 10, progress, stop.is_set)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name("saved.m4a.part").exists())

    def test_cancel_before_network_request(self):
        with self.assertRaises(core.StopRequested):
            core.download("http://invalid.invalid/a.m4a", self.dest, {}, 10, should_stop=lambda: True)
        self.assertFalse(self.dest.exists())

    def test_stop_while_server_stalls_is_bounded(self):
        self.server.started.clear()
        self.server.release.clear()
        stop = threading.Event()
        def request_stop():
            self.server.started.wait(2)
            time.sleep(0.15)
            stop.set()
        stopper = threading.Thread(target=request_stop)
        stopper.start()
        started = time.monotonic()
        try:
            with self.assertRaises(core.StopRequested):
                core.download(self.base + "/stall.m4a", self.dest, {}, 20, should_stop=stop.is_set)
            self.assertLess(time.monotonic() - started, 6.5)
        finally:
            self.server.release.set()
            stopper.join(timeout=2)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name("saved.m4a.part").exists())


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.src = Path(self.temp.name) / "source.m4a"
        self.src.write_bytes(b"original")

    def test_mp3_source_is_never_overwritten(self):
        src = self.src.with_suffix(".mp3")
        src.write_bytes(b"original-mp3")
        with patch.object(core.subprocess, "Popen") as start:
            self.assertTrue(core.convert_to_mp3(src))
        start.assert_not_called()
        self.assertEqual(src.read_bytes(), b"original-mp3")

    def test_conversion_failure_preserves_original_and_existing_mp3(self):
        dst = self.src.with_suffix(".mp3")
        dst.write_bytes(b"previous-mp3")
        proc = Mock(returncode=1)
        proc.communicate.return_value = (b"", b"Unknown encoder libmp3lame")
        proc.poll.return_value = 1
        with patch.object(core, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(core.subprocess, "Popen", return_value=proc), patch.object(core, "log"):
            self.assertFalse(core.convert_to_mp3(self.src))
        self.assertEqual(self.src.read_bytes(), b"original")
        self.assertEqual(dst.read_bytes(), b"previous-mp3")

    def test_success_atomically_publishes_mp3(self):
        proc = Mock(returncode=0)
        proc.poll.return_value = 0
        def communicate(timeout):
            self.src.with_suffix(".mp3.part").write_bytes(b"encoded-fixture")
            return b"", b""
        proc.communicate.side_effect = communicate
        with patch.object(core, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(core.subprocess, "Popen", return_value=proc), patch.object(core, "log"):
            self.assertTrue(core.convert_to_mp3(self.src))
        self.assertEqual(self.src.with_suffix(".mp3").read_bytes(), b"encoded-fixture")
        self.assertEqual(self.src.read_bytes(), b"original")

    def test_cancel_terminates_real_conversion_process(self):
        stop = threading.Event()
        launched = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            # 模拟一个耗时编码进程，验证真实进程的取消与回收。
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            launched.append(proc)
            stop.set()
            return proc
        with patch.object(core, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(core.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(core.StopRequested):
                core.convert_to_mp3(self.src, stop.is_set)
        self.assertIsNotNone(launched[0].poll())
        self.assertEqual(self.src.read_bytes(), b"original")
        self.assertFalse(self.src.with_suffix(".mp3.part").exists())


if __name__ == "__main__":
    unittest.main()
