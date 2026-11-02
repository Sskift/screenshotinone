# screenshotinone

把 Steam 截图自动同步到手机能访问的地方。在电脑上按下截图键，后台程序会把原图保存到 **Netclip Drive**，或复制到已有网盘客户端的同步目录。

Windows 优先，Python 3.11+。Netclip/文件夹同步不依赖第三方库；可选的 Steam 社区发布使用 `websocket-client`。不需要保存 Steam 密码，后台同步不操作鼠标、键盘或前台窗口。

## 第一版功能

- 自动发现 Steam 主客户端的截图目录，支持多个 Steam 账号和游戏。
- 扫描 `userdata/<账号>/760/remote/<游戏 AppID>/screenshots`，仅处理原图，不处理缩略图。
- Netclip Drive：使用永久文件区，按 `Steam Screenshots/账号/AppID/` 分目录保存。
- 本地文件夹：可以接 OneDrive、Nextcloud 等客户端已配置的同步目录，云传输由对应客户端负责。
- 内容 SHA-256 + SQLite 同步记录，重复扫描不重复上传；源文件更新后保存新版本。
- 等待文件稳定，使用独立快照上传；失败在下一轮重试，不修改或删除 Steam 原文件。
- Windows 登录后静默运行，轮询默认 15 秒，日志自动轮转。
- 实验性 Steam 社区发布：复用本机已登录客户端，支持私密、好友、公开、不列出；独立队列与 Netclip 同步并行记账。

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

## Steam 社区发布（实验功能）

此功能调用 Steam 客户端内部接口，可能随 Steam 更新变化。先安装可选依赖并运行只读诊断：

```powershell
python -m pip install -e ".[steam]"
python -m screenshotinone steam doctor
```

Steam 需要以 `-cef-enable-debugging` 启动。诊断不会重启 Steam、修改设置或发布图片。项目提供一个手动启动脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-steam-debugging.ps1 -Restart
```

**运行该脚本会请求退出并重启 Steam，请先结束游戏。** 它使用 Steam 自身的退出指令，最多等待 45 秒，不强杀进程；以 `-silent` 启动仍可能让 Steam 显示窗口。脚本不会修改快捷方式或系统自启动项，因此以后启动 Steam 也需要保留调试参数。CDP 地址仅接受数值回环地址 `127.0.0.1` / `[::1]`，使用前应确保调试端口不对其他设备开放。

诊断成功后，把以下 `steam` 对象添加到已有配置中，保留原来的 `target`。用诊断输出的 `account_id` 替换示例（它是 `userdata` 下的账号目录名，不是 17 位 SteamID）：

```json
"steam": {
  "enabled": false,
  "endpoint": "http://127.0.0.1:8080",
  "account_id": "123456789",
  "visibility": "private",
  "include_existing": false,
  "timeout": 10,
  "confirm_timeout": 10
}
```

完整配置见 [Steam 示例](examples/steam.example.json)。先指定一张 Steam 原图验证；`publish` 只处理所选文件，最多发起一次上传，不启用后台批量发布：

```powershell
python -m screenshotinone steam publish --file "C:\path\Steam\userdata\123456789\760\remote\123\screenshots\capture.jpg" --dry-run
python -m screenshotinone steam publish --file "C:\path\Steam\userdata\123456789\760\remote\123\screenshots\capture.jpg" --visibility private
python -m screenshotinone steam status
```

`--dry-run` 只列出本地计划，不连接 Steam，不写状态，也不会调用上传；实际接口、账号可用性由 `steam doctor` 检查。单张验证通过后将 `steam.enabled` 改为 `true`，重启 screenshotinone worker。此后普通的 `sync` / `watch` 会分别运行原目标和 Steam 队列，一方失败不阻止另一方的本轮尝试。

Steam 队列的首次普通扫描只登记已有截图，之后的新截图才会自动发布。单张 `publish` 不改变这个基准。显式传入 `steam sync --include-existing --limit 1` 可逐步补传历史截图；省略 `--limit` 会处理全部符合条件的历史截图。`include_existing: true` 会让每轮都纳入历史文件，建议保留默认 `false`。

可见性使用 `private`（默认）、`friends`、`public`、`unlisted`。程序核对当前登录账号、原生截图路径、游戏和 handle；确认 `bUploaded`、有效内容句柄以及实际可见性后才记为成功。已经上传但可见性不同的截图会停止处理，不会自动更改其隐私设置。客户端内容句柄与发布 ID 分开保存：只有明确的发布 ID 才生成单张社区链接，否则返回该账号的截图页。

Steam 队列保存在配置目录的 `steam-state.sqlite3`，与原目标同步记录独立。收到明确失败会退避重试，最多三次；发生超时、断线或进程在上传中退出时记为 `uncertain`，后续只查询原生上传状态，**不会自动重新提交不确定的请求**。可用 `steam status` 查看状态并在 Steam 中核对；不要通过删除队列文件绕过不确定状态。Steam 关闭、未登录或调试入口不可用时，待发布记录会保留。

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

当前逐轮读取内容计算校验值，适合个人截图收藏；非常大的图库会产生额外磁盘读取，可调大 `poll_interval`。Steam 自定义保存位置和 Steam 游戏录制视频暂未支持。

文件夹目标使用硬链接原子发布完整文件，要求目标文件系统支持硬链接（例如 Windows NTFS）；不支持的文件系统会明确报错，不会降级为可能留下半文件的复制。

Steam 官方 [ISteamScreenshots](https://partner.steamgames.com/doc/api/ISteamScreenshots) 主要提供向本地截图库添加截图的接口，[社区上传流程](https://partner.steamgames.com/doc/features/screenshots)由截图上传器处理。实验性发布适配器使用本机 Steam 的 `SteamClient.Screenshots.UploadLocalScreenshot`，并按当前客户端探测 Count/Range 或旧版枚举接口；参考 [ScreenshotUploader](https://github.com/codehz/DeckScreenshotManager/blob/main/src/index.tsx) 与 [steam-engine 的 CDP 说明](https://github.com/6E6B/steam-engine)。这不是官方承诺稳定的跨游戏 Web API。Netclip 对接协议来自 [Sskift/netclip 的 Drive 实现](https://github.com/Sskift/netclip/blob/main/server/src/drive.js)；其 WebDAV 当前为只读，上传走 Drive API。

## 参与开发

```powershell
python -m pip install -e ".[steam]"
python -m unittest discover -s tests -v
```

测试使用临时目录与本地模拟 HTTP 服务，不连接个人 Steam/Netclip 数据。GitHub Actions 在 Windows、Linux 的 Python 3.11–3.13 上运行测试。

模块职责：`source.py` 发现原图，`engine.py` 调度一次存储同步，`state.py` 记录成功结果并互斥，`targets.py` 对接存储，`cli.py` 提供命令与后台轮询。可选 Steam 功能分为 `cdp.py` 本机传输、`steam_bridge.py` 原生接口校验、`steam_sync.py` 持久化发布队列。

欢迎提交 Issue/PR。优先方向：增量扫描性能、可选游戏名称、打包为独立 Windows 程序、移动端相册预览。修改同步行为时请覆盖重试、去重与不触碰源文件的测试。

MIT License.
