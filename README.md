# Ximalaya Web Capture · 喜马拉雅网页音频抓取

使用 Python、Playwright 和 Tkinter，将网页播放、媒体请求捕获、保存文件和切换下一首串联起来。提供图形界面与命令行两种入口。

本项目是独立工具，与喜马拉雅官方无关联。仅保存你有权下载且当前账号可正常播放的内容，不提供解密或访问权限绕过。MIT 许可证适用于项目代码，不代表获得网页音频的使用授权。

## 功能

- 专辑或单曲起始地址、抓取数量和文件编号设置。
- 图形界面显示文件进度、任务列表和日志。
- 自动检测播放器是否解锁，并提供“我已完成登录”手动确认。
- 媒体地址按资源路径去重；每首切换前清空缓存，两种缓存均限制为最多 8 条。
- 按媒体地址保留 `.m4a`、`.mp3`、`.aac`、`.flac` 或 `.mp4` 扩展名。
- 下载先写 `.part`，完整后改名；出错或取消时清理未完成文件。
- 可选 MP3 转换，保留原文件；转换失败会明确记录原因。
- 图形界面的停止与退出会等待后台清理，不在界面线程中阻塞等待。

## 环境

- Windows；Python 3.10 或以上，需带 Tkinter。
- Playwright 和它安装的 Chromium。
- 可选：PATH 中带 `libmp3lame` 编码器的完整 FFmpeg。

开发时本地验证环境为 Windows、Python 3.11.15、Playwright 1.63.0。其他操作系统尚未验证；“打开文件夹”功能使用 Windows 接口。

## 安装与启动

在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe ximalaya_grab_gui.py
```

粘贴专辑或单曲地址，选择目录和数量，点击“开始抓取”。在打开的浏览器中完成登录。检测未识别登录时点击“我已完成登录”；未捕获媒体时可以确认播放后重试。

浏览器默认保留声音，便于确认播放是否正常。GUI 的登录等待是软超时，超过设定时间仍继续等待确认或停止；播放确认按钮等待 180 秒后结束本次尝试。

GUI 登录缓存保存在脚本旁的 `.xm_profile`；默认下载目录为启动时当前目录下的 `downloads`。缓存中可能包含账号会话，请勿分享或提交。CLI 的 `--profile` 默认相对于当前目录，若需要共享 GUI 登录态，请显式指定同一个目录，并避免两者同时使用它。

## 命令行

```powershell
python ximalaya_grab.py --url https://www.ximalaya.com/album/3867773 --count 30 --out D:\audio
python ximalaya_grab.py --url https://www.ximalaya.com/sound/1012119826 --count 5
python ximalaya_grab.py --url https://www.ximalaya.com/album/3867773 --count 3 --mp3
python ximalaya_grab.py --help
```

| 参数 | 说明 |
| --- | --- |
| `--url` | 必填，专辑或单曲 HTTP/HTTPS 地址 |
| `--out` | 保存目录，默认 `downloads` |
| `--count` | 最多抓取数量，默认 9999，遇到无法捕获下一首时结束 |
| `--start-index` | 文件编号起点，默认 1；仅影响文件编号，不定位网页曲目 |
| `--profile` | 浏览器缓存目录，默认 `.xm_profile` |
| `--login-timeout` | CLI 登录等待上限，默认 300 秒；GUI 为软超时 |
| `--media-wait` | 每次等待媒体请求的秒数，默认 15 |
| `--download-timeout` | 单文件下载总时限，默认 180 秒 |
| `--mp3` | 下载后尝试转 MP3，原文件保留 |
| `--force` | 重新下载已存在的原文件 |
| `--headless` | 无头运行；需事先保存可用登录态 |
| `--quiet` | 隐藏逐条媒体请求日志 |

文件名示例：`001_曲目标题.m4a`。扩展名依据媒体 URL 路径，不进行格式转码识别。CLI 退出码：0 表示本次无下载错误，1 表示有下载或 MP3 转换失败，2 表示登录超时，3 表示没有捕获第一首媒体；输入错误也使用 argparse 的退出码 2。提前结束会在日志提示，不能仅以退出码判断整张专辑已下载完毕。

## MP3 转换

先确认完整 FFmpeg 可用：

```powershell
ffmpeg -encoders | Select-String libmp3lame
```

程序优先使用 PATH 中的 FFmpeg。Playwright 附带的 FFmpeg 也会被尝试发现，但通常不含 MP3 编码器，不能保证可用；缺少编码器时保留原文件并报告转换失败。已经是 MP3 的输入直接保留，避免覆盖自身。

## 停止、错误与限制

- 下载停止在网络调用返回后生效。GUI 单次连接或读取的超时最多 5 秒；无数据到达 5 秒也可能导致下载失败。页面导航最长等待 60 秒，后台退出会等待其返回。界面保持响应。
- 下载拒绝空文件、长度不足、HTML 返回和已知不完整的 `Content-Range`。
- 下一首未捕获时会尝试恢复当前曲目播放，不再重复点下一首而跳过曲目。网站改版、权限不足和专辑结束都可能导致提前结束。
- 只捕获 `media` 类型且带受支持扩展名的响应；不支持 DASH/HLS 分片拼接、DRM、无扩展名地址或通过其他请求类型加载的媒体。
- 目前是实验性项目。真实账号登录后的连续抓取及完整专辑下载尚未完成端到端验收；本地测试不能证明当前网站适配正常。

## 测试与贡献

```powershell
python -m unittest discover -s tests -v
```

测试使用本地 HTTP 服务和临时目录，不访问账号或线上音频。GitHub Actions 配置为 Windows 自动运行测试。

提交问题时说明 Python/Playwright 版本、复现步骤和经脱敏的错误信息。请不要上传登录缓存、Cookie、签名媒体 URL、下载音频或包含账号信息的截图。网页选择器集中在 `ximalaya_grab.py`，界面逻辑在 `ximalaya_grab_gui.py`。

## 许可证

[MIT](LICENSE)
