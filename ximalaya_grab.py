#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
喜马拉雅网页版音频抓取（媒体请求捕获法）

原理和你手动做的一模一样：
    网页播放 -> 监听网络请求 -> 只看 Media 类型 -> 拿到 m4a 地址 -> 存文件
区别只是把「点下一首、翻网络面板、复制地址、另存为」这套动作自动化了。

关于轨道堆积：
    DevTools 里如果不按 Ctrl+E 停掉录制，Media 面板会越积越多（同一首歌
    的 range 分片请求会重复出现）。本脚本用三重限制保证永远不堆积：
      1. 只接收 resource_type == "media" 的响应，等价于 DevTools 的 Media 筛选；
      2. 按去掉 query 的 URL 路径去重，分片/续传的重复请求只算一条；
      3. 每首开始前 reset() 清空缓冲，列表里最多同时存在 1~8 条，下完即弃。

边界说明：
    本工具不做任何解密或破解。它只能捕获当前账号在网页上能正常播放的媒体。
    请仅保存你有权下载的内容；工具不提供解密或访问权限绕过。

依赖：
    pip install playwright
    python -m playwright install chromium

用法：
    # 抓整张专辑，从头连抓 30 首
    python ximalaya_grab.py --url https://www.ximalaya.com/album/3867773 --count 30

    # 从某一首单曲开始，抓 5 首
    python ximalaya_grab.py --url https://www.ximalaya.com/sound/1012119826 --count 5

    # 顺带转成 MP3（需要带 MP3 编码器的完整 FFmpeg）
    python ximalaya_grab.py --url <专辑或单曲地址> --mp3
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

SEL_NEXT = ".xm-player-btns a.next"          # 「下一首」按钮
SEL_PREV = ".xm-player-btns a.prev"
SEL_PLAY = ".xm-player-btns a.play"          # 播放/暂停
SEL_TITLE = ".xm-player-body-title a.fm-title"   # 当前曲目标题
SEL_LOCK = ".xm-player.is-locked"            # 未登录时播放器带这个标记
SEL_TRACK_LINK = "a[href*='/sound/']"        # 专辑页里的曲目链接

MEDIA_EXT = re.compile(r"\.(m4a|mp3|aac|flac|mp4)(?:[?#]|$)", re.I)
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
MAX_BUFFER = 8


class StopRequested(Exception):
    """取消任务；由下载和转换共用。"""


def check_stop(should_stop=None) -> None:
    if should_stop and should_stop():
        raise StopRequested


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port
    except ValueError as exc:
        raise ValueError("地址格式不正确") from exc
    if (parts.scheme not in ("http", "https") or parts.username or parts.password
            or not (host == "ximalaya.com" or host.endswith(".ximalaya.com"))):
        raise ValueError("请填写喜马拉雅的 HTTP/HTTPS 专辑或单曲地址")
    return url


def media_suffix(url: str) -> str:
    match = MEDIA_EXT.search(urlsplit(url).path)
    if not match:
        raise ValueError("不支持的媒体格式（不支持分片拼接）")
    return "." + match.group(1).lower()


def log(*args) -> None:
    print(*args, flush=True)


class MediaCapture:
    """只保留当前这一首的媒体地址，抓到就清，防止轨道堆积。"""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self._urls: list[str] = []
        self._seen: set[str] = set()

    def reset(self) -> None:
        self._urls.clear()
        self._seen.clear()

    def on_response(self, resp) -> None:
        try:
            if resp.request.resource_type != "media":   # 等价 DevTools 的 Media 筛选
                return
            if resp.status not in (200, 206):
                return
            url = resp.url
            if not MEDIA_EXT.search(url):
                return
        except Exception:      # noqa: BLE001  - 请求已被丢弃等情况
            return

        key = url.split("?")[0].split("#")[0]
        if key in self._seen or len(self._urls) >= MAX_BUFFER:
            return
        self._seen.add(key)
        self._urls.append(url)
        if self.verbose:
            log(f"        · 捕获 media: …{key[-46:]}  (去重后 {len(self._urls)} 条)")

    @property
    def urls(self) -> list[str]:
        return list(self._urls)


def safe_name(name: str, limit: int = 70) -> str:
    name = BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip()
    return name or "track"


def url_key(url: str) -> str:
    """去掉签名/range 等 query，只留资源路径，用来判断「是不是同一首」。"""
    return url.split("?")[0].split("#")[0]


def click_sel(page, sel: str, timeout: int = 5000) -> bool:
    """先真点，失败再用 JS 直接派发，提升对浮层遮挡的容错。"""
    try:
        page.click(sel, timeout=timeout)
        return True
    except Exception:      # noqa: BLE001
        pass
    try:
        page.eval_on_selector(sel, "el => el.click()")
        return True
    except Exception:      # noqa: BLE001
        return False


def current_title(page) -> str:
    try:
        el = page.query_selector(SEL_TITLE)
        if el:
            text = (el.get_attribute("title") or el.inner_text() or "").strip()
            if text:
                return text
    except Exception:      # noqa: BLE001
        pass
    return ""


def wait_for_media(
    page,
    cap: MediaCapture,
    prev: str | None,
    wait_s: float,
    should_stop=None,
) -> str | None:
    """等一个新的媒体地址。

    和 prev 比较时用的是资源路径而不是整条 URL：同一首歌后续的 range 分片
    请求 query 会变，如果比整串就会被误判成「下一首来了」。

    should_stop 是一个返回 bool 的可调用对象，交给 GUI 用来随时打断等待。
    """
    prev_key = url_key(prev) if prev else None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if should_stop and should_stop():
            return None
        page.wait_for_timeout(300)
        for url in cap.urls:
            if prev_key is None or url_key(url) != prev_key:
                return url
    return None


def ensure_login(page, timeout_s: float, should_stop=None) -> bool:
    if not page.query_selector(SEL_LOCK):
        return True
    log("")
    log("  ⚠ 播放器当前是未登录状态（页面元素带 is-locked），点播放不会发媒体请求。")
    log("    请在弹出的浏览器窗口里完成登录（扫码或手机号都行），脚本会自动往下走。")
    log("")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if should_stop and should_stop():
            return False
        page.wait_for_timeout(1500)
        if not page.query_selector(SEL_LOCK):
            log("  ✓ 已登录。")
            page.wait_for_timeout(2000)
            return True
    log("  ✗ 等待登录超时。")
    return False


def build_headers(ctx, page, media_url: str) -> dict[str, str]:
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies([media_url]))
    return {
        "User-Agent": UA,
        "Referer": page.url,
        "Origin": "https://www.ximalaya.com",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }


def download(
    url: str,
    dest: Path,
    headers: dict[str, str],
    timeout: int,
    on_progress=None,
    should_stop=None,
) -> int:
    """下载到 .part 再改名，中途出错不留半个文件。

    on_progress(got_bytes, total_bytes) 每读一块调一次，GUI 用它更新进度。
    """
    tmp = dest.with_name(dest.name + ".part")
    req = Request(url, headers=headers)
    check_stop(should_stop)
    deadline = time.monotonic() + timeout
    # GUI 的每次网络阻塞最多 5 秒，以便停止请求能及时生效。
    io_timeout = min(timeout, 5) if should_stop else timeout
    try:
        with urlopen(req, timeout=io_timeout) as resp, open(tmp, "wb") as fh:
            check_stop(should_stop)
            total = int(resp.headers.get("Content-Length") or 0)
            if resp.headers.get_content_type() == "text/html":
                raise ValueError("媒体地址返回了网页，未保存为音频")
            content_range = resp.headers.get("Content-Range")
            if content_range:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not match or int(match[1]) != 0 or int(match[2]) + 1 != int(match[3]):
                    raise ValueError("服务器仅返回部分媒体，未保存为完整文件")
                total = int(match[3])
            got = 0
            while True:
                check_stop(should_stop)
                if time.monotonic() >= deadline:
                    raise TimeoutError("超过单文件下载总时限")
                chunk = resp.read1(1 << 16)
                check_stop(should_stop)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
            if got == 0 or (total and got != total):
                raise ValueError(f"媒体下载不完整：收到 {got} 字节，预期 {total} 字节")
        check_stop(should_stop)
        tmp.replace(dest)
        return got
    except BaseException:
        tmp.unlink(missing_ok=True)
        check_stop(should_stop)
        raise


def cli_progress(got: int, total: int) -> None:
    """命令行版的下载进度（整行刷新）。"""
    if total:
        print(f"\r        ↓ {got * 100 // total:3d}%  {got / 1048576:6.1f}/{total / 1048576:.1f} MB",
              end="", flush=True)
    else:
        print(f"\r        ↓ {got / 1048576:6.1f} MB", end="", flush=True)


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if base.exists():
        for cand in sorted(base.glob("ffmpeg-*/ffmpeg*")):
            if cand.is_file() and cand.suffix.lower() in (".exe", ""):
                return str(cand)
    return None


def convert_to_mp3(src: Path, should_stop=None) -> bool:
    check_stop(should_stop)
    if src.suffix.lower() == ".mp3":
        return True
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("        ! 没找到 FFmpeg，跳过 MP3 转换（原文件已保留）")
        return False
    dst = src.with_suffix(".mp3")
    tmp = dst.with_name(dst.name + ".part")
    proc = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
         "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-f", "mp3", str(tmp)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        while True:
            check_stop(should_stop)
            try:
                _, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        check_stop(should_stop)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size:
            tmp.replace(dst)
            log(f"        ✓ 已转 MP3：{dst.name}（原文件保留）")
            return True
        err = stderr.decode("utf-8", "replace").strip()[-500:]
        log(f"        ! MP3 转换失败：{err or 'FFmpeg 返回非 0'}")
        log("          请在 PATH 中提供带 libmp3lame 编码器的完整 FFmpeg；原文件已保留。")
        return False
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        tmp.unlink(missing_ok=True)


def start_playback(
    page,
    cap: MediaCapture,
    wait_s: float,
    is_album: bool,
    should_stop=None,
) -> str | None:
    """点开第一首，等第一次媒体请求。"""
    cap.reset()

    if is_album:
        first_href = None
        try:
            first_href = page.get_attribute(SEL_TRACK_LINK, "href")
        except Exception:      # noqa: BLE001
            pass
        for sel in ("li .player-box", "li a[href*='/sound/']", SEL_TRACK_LINK):
            check_stop(should_stop)
            if not click_sel(page, sel):
                continue
            log(f"        点击列表控件：{sel}")
            url = wait_for_media(page, cap, None, wait_s, should_stop)
            if url:
                return url
        if first_href:
            check_stop(should_stop)
            target = first_href if first_href.startswith("http") else "https://www.ximalaya.com" + first_href
            log(f"        列表点击没触发播放，直接打开曲目页：{target}")
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            cap.reset()
            if click_sel(page, SEL_PLAY):
                return wait_for_media(page, cap, None, wait_s, should_stop)
        return None

    for sel in (SEL_PLAY, ".play-btn", "button[class*=play]"):
        check_stop(should_stop)
        if click_sel(page, sel):
            url = wait_for_media(page, cap, None, wait_s, should_stop)
            if url:
                return url
    return None


def goto_next(
    page,
    cap: MediaCapture,
    prev: str,
    wait_s: float,
    should_stop=None,
) -> str | None:
    check_stop(should_stop)
    cap.reset()
    if not click_sel(page, SEL_NEXT):
        log("        ! 找不到「下一首」按钮")
        return None
    url = wait_for_media(page, cap, prev, wait_s, should_stop)
    if url:
        return url
    check_stop(should_stop)
    log("        · 没等到新的媒体请求，尝试恢复当前曲目播放…")
    click_sel(page, SEL_PLAY, timeout=3000)
    return wait_for_media(page, cap, prev, wait_s, should_stop)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="喜马拉雅网页版音频抓取（捕获你浏览器里本来就在播的 m4a）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", required=True, type=validate_url, help="专辑页或单曲页地址")
    ap.add_argument("--out", default="downloads", help="保存目录（默认 ./downloads）")
    ap.add_argument("--count", type=int, default=9999, help="最多抓多少首（默认全部，抓到不能下一首为止）")
    ap.add_argument("--start-index", type=int, default=1, help="文件编号起始值")
    ap.add_argument("--profile", default=".xm_profile", help="浏览器登录态缓存目录")
    ap.add_argument("--login-timeout", type=float, default=300, help="等待你完成登录的秒数")
    ap.add_argument("--media-wait", type=float, default=15, help="每首等待媒体请求的秒数")
    ap.add_argument("--download-timeout", type=int, default=180, help="单文件下载超时秒数")
    ap.add_argument("--mp3", action="store_true", help="下载后转成 MP3")
    ap.add_argument("--force", action="store_true", help="已存在的文件也重新下载")
    ap.add_argument("--headless", action="store_true", help="无头运行（登录需要先跑一次有头）")
    ap.add_argument("--quiet", action="store_true", help="不打印每条媒体请求")
    args = ap.parse_args(argv)
    for name in ("count", "start_index", "login_timeout", "media_wait", "download_timeout"):
        if getattr(args, name) <= 0:
            ap.error(f"--{name.replace('_', '-')} 必须大于 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    is_album = "/album/" in args.url

    log(f"目标：{args.url}")
    log(f"保存到：{out_dir}")
    log(f"登录态目录：{profile}")

    cap = MediaCapture(verbose=not args.quiet)
    done: list[Path] = []
    failed: list[tuple[int, str, str]] = []
    skipped = 0
    conversion_failed = 0

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile),
            headless=args.headless,
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", cap.on_response)

            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)

            if not ensure_login(page, args.login_timeout):
                return 2

            log("\n开始播放第一首…")
            media_url = start_playback(page, cap, args.media_wait, is_album)
            if not media_url:
                log("\n✗ 没捕获到任何音频请求。排查顺序：")
                log("  1) 窗口里确认已登录，且点一下播放器能真的出声；")
                log("  2) 当前账号是否具有这一首的播放权限；")
                log("  3) 把 --media-wait 调大，比如 --media-wait 30；")
                log("  4) 去掉 --quiet 看每次点了什么、等到了什么。")
                return 3

            index = args.start_index
            while media_url and index < args.start_index + args.count:
                title = current_title(page) or f"track_{index:03d}"
                dest = out_dir / f"{index:03d}_{safe_name(title)}{media_suffix(media_url)}"
                log(f"\n[{index}] {title}")
                log(f"        媒体路径: {url_key(media_url)}")

                if dest.exists() and not args.force:
                    log(f"        = 已存在，跳过：{dest.name}")
                    skipped += 1
                else:
                    try:
                        headers = build_headers(ctx, page, media_url)
                        size = download(
                            media_url, dest, headers, args.download_timeout,
                            on_progress=cli_progress,
                        )
                        print()
                        log(f"        ✓ {dest.name}  ({size / 1048576:.1f} MB)")
                        done.append(dest)
                        if args.mp3:
                            if not convert_to_mp3(dest):
                                conversion_failed += 1
                    except Exception as exc:      # noqa: BLE001
                        log(f"        ✗ 下载失败：{exc}")
                        failed.append((index, title, str(exc)[:120]))

                index += 1
                if index >= args.start_index + args.count:
                    break

                cap.reset()        # ← 关键：清空缓冲，下一首从零开始，绝不堆积
                prev = media_url
                media_url = goto_next(page, cap, prev, args.media_wait)

        finally:
            ctx.close()

    log("\n" + "=" * 56)
    log(f"本次结束：下载成功 {len(done)} 首，跳过 {skipped} 首，下载失败 {len(failed)} 首")
    if conversion_failed:
        log(f"MP3 转换失败 {conversion_failed} 首，原文件已保留")
    if not media_url and index < args.start_index + args.count:
        log("未捕获下一首，提前结束；请检查专辑是否已结束或网页播放状态。")
    for path in done:
        log(f"  ✓ {path.name}")
    for idx, title, err in failed:
        log(f"  ✗ [{idx}] {title} -> {err}")
    log(f"目录：{out_dir}")
    return 1 if failed or conversion_failed else 0


if __name__ == "__main__":
    sys.exit(main())
