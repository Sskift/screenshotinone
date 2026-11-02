# screenshotinone

把 Steam 截图自动同步到手机能访问的地方。在电脑上按下截图键，后台程序会把原图保存到 **Netclip Drive**，或复制到已有网盘客户端的同步目录。

Windows 优先，Python 3.11+，运行时不依赖第三方库。不需要 Steam 密码，不操作鼠标、键盘或前台窗口。

## 第一版功能

- 自动发现 Steam 主客户端的截图目录，支持多个 Steam 账号和游戏。
- 扫描 `userdata/<账号>/760/remote/<游戏 AppID>/screenshots`，仅处理原图，不处理缩略图。
- Netclip Drive：使用永久文件区，按 `Steam Screenshots/账号/AppID/` 分目录保存。
- 本地文件夹：可以接 OneDrive、Nextcloud 等客户端已配置的同步目录，云传输由对应客户端负责。
- 内容 SHA-256 + SQLite 同步记录，重复扫描不重复上传；源文件更新后保存新版本。
- 等待文件稳定，使用独立快照上传；失败在下一轮重试，不修改或删除 Steam 原文件。
- Windows 登录后静默运行，轮询默认 15 秒，日志自动轮转。

## 快速开始

```powershell
git clone https://github.com/Sskift/screenshotinone.git
cd screenshotinone
python -m pip install -e .
python -m screenshotinone scan
```

配置 Netclip（替换示例地址）：

```powershell
python -m screenshotinone init --target netclip --url http://YOUR-NETCLIP-HOST:3210 --folder "Steam Screenshots"
python -m screenshotinone sync --dry-run
python -m screenshotinone sync
```

首次同步会包含已有截图。成功后输出的 `folder_url` 可直接在手机浏览器中打开。若 Netclip 只在 Tailscale 网络内提供服务，手机需连接同一 Tailnet。Drive 支持访问、下载原图；它目前不是专用相册界面。

默认配置和本地状态位于 `%LOCALAPPDATA%\screenshotinone\`。`init` 不会覆盖已有配置。修改目标时直接编辑配置，或通过 `--config` 使用另一份配置；全局选项须放在子命令前：

```powershell
python -m screenshotinone --config C:\path\config.json sync
```

要使用已有网盘的本地目录，可以在首次配置时改用：

```powershell
python -m screenshotinone init --target folder --destination "$env:USERPROFILE\OneDrive\Steam Screenshots"
```

更多配置见 [Netclip 示例](examples/netclip.example.json) 和 [文件夹示例](examples/folder.example.json)。`steam_roots` 为空时自动发现；也可指定 Steam 安装目录，或在 `init` 时传入 `--steam-root`。路径相对于配置文件解析，支持 `~` 和环境变量。Linux 默认使用 `$XDG_CONFIG_HOME/screenshotinone`，否则使用 `~/.config/screenshotinone`。

## Windows 后台自动运行

完成一次同步后，在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-startup.ps1 -ConfigPath "$env:LOCALAPPDATA\screenshotinone\config.json" -StartNow
```

脚本创建当前用户的 `ScreenshotInOne` 登录计划任务，使用 `pythonw.exe`，没有控制台窗口，不抢焦点。项目路径和 Python 安装需保持可用。日志默认在配置目录的 `sync.log`。任务在当前用户登录期间运行；关机或退出登录后停止，下次登录继续扫描并补传。

```powershell
python -m screenshotinone status
Get-Content "$env:LOCALAPPDATA\screenshotinone\sync.log" -Tail 20
```

卸载自动启动（保留配置、状态与已同步文件）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/remove-startup.ps1
```

其他系统可运行 `python -m screenshotinone watch`，并用自己的进程管理器维护。配置修改后需重启 worker。

## 同步语义与边界

这是单向、追加式同步。删除本地截图不会删除云端截图；本地记录已成功的截图也不会在每轮重新检查云端是否被手动删除。要重新核对/补齐远端，可停掉 worker，备份并移开配置目录中的 `state.sqlite3`，再运行同步：目标端会按确定性文件名和内容校验复用已存在文件。

远端文件名包含完整 SHA-256，因此同名截图的新内容不会覆盖旧内容。Netclip 上传接口自身没有幂等键，程序通过上传前核对已有文件、下载比对相同名称的内容来恢复不确定的上传结果。本机共用一个状态文件的同步进程受锁保护；多台电脑同时上传相同截图仍可能遇到服务端重名竞态，请避免多个独立 worker 同时处理同一份图库。

Netclip 地址应指向你已配置好的受信服务，网络权限由该服务负责。项目不会将个人服务地址、同步记录或截图提交到 Git。公开仓库中的配置仅为示例。

首版逐轮读取内容计算校验值，适合个人截图收藏；非常大的图库会产生额外磁盘读取，可调大 `poll_interval`。Steam 自定义保存位置、Steam 游戏录制视频和自动发布 Steam 社区截图暂未支持。

文件夹目标使用硬链接原子发布完整文件，要求目标文件系统支持硬链接（例如 Windows NTFS）；不支持的文件系统会明确报错，不会降级为可能留下半文件的复制。

Steam 官方 [ISteamScreenshots](https://partner.steamgames.com/doc/api/ISteamScreenshots) 主要提供向本地截图库添加截图的接口，[社区上传流程](https://partner.steamgames.com/doc/features/screenshots)由截图上传器处理。本项目首版选择可直接验证的 Netclip Drive 同步，不依赖非公开 Steam 登录或发布接口。Netclip 对接协议来自 [Sskift/netclip 的 Drive 实现](https://github.com/Sskift/netclip/blob/main/server/src/drive.js)；其 WebDAV 当前为只读，上传走 Drive API。

## 参与开发

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试使用临时目录与本地模拟 HTTP 服务，不连接个人 Steam/Netclip 数据。GitHub Actions 在 Windows、Linux 的 Python 3.11–3.13 上运行测试。

模块职责：`source.py` 发现原图，`engine.py` 调度一次同步，`state.py` 记录成功结果并互斥，`targets.py` 对接存储，`cli.py` 提供命令与后台轮询。

欢迎提交 Issue/PR。优先方向：增量扫描性能、可选游戏名称、打包为独立 Windows 程序、移动端相册预览。修改同步行为时请覆盖重试、去重与不触碰源文件的测试。

MIT License.
