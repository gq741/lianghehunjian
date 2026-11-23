# WorkBuddy：更新量禾混剪至 2026.11.23

> 发给 WorkBuddy：请按本说明判断我是首次安装还是更新，使用官方 v2026.11.23 发布页核验并安装。不要读取、展示或上传我的 Key，不重跑文案、配音或付费分析。结束后告诉我版本、实际安装位置、自检结果、备份位置；失败说明停在哪一步，不要强行覆盖。

**旧电脑首选小更新包**，不重装运行时与模型。**从未安装**则先照[完整包说明](https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.29)将该版的 `Download-Release.cmd` 和 `Download-Release.ps1` 放进同一空目录，运行 CMD，等待校验、还原和免费自检，然后再更新。

1. 定位原快捷方式实际使用的安装目录，确认包含 `量禾混剪.exe`、`runtime/python.exe`、`app`、`RELEASE.json`。多处安装先向使用者确认。让用户保存修改、结束在跑的任务并退出，不强制终止生成过程。
2. 从[固定版本发布页](https://github.com/gq741/lianghehunjian/releases/tag/v2026.11.23)获取 [LiangHeMix-Update-2026.11.23.zip](https://github.com/gq741/lianghehunjian/releases/download/v2026.11.23/LiangHeMix-Update-2026.11.23.zip) 和 [UPDATE_FEED.json](https://github.com/gq741/lianghehunjian/releases/download/v2026.11.23/UPDATE_FEED.json)。核对 feed 中的 `version` 为 `2026.11.23`、`archive.name`、`archive.bytes`、`archive.sha256`；用 PowerShell `Get-FileHash -Algorithm SHA256 -LiteralPath $zip` 核对 ZIP。校验失败立即停止。
3. 将 ZIP 完整解压到独立新目录，不在安装目录里解压。普通用户双击“更新量禾混剪.cmd”。WorkBuddy 可在含 `Update.ps1` 的解压目录调用：
   ```powershell
   # $install 先设为步骤 1 核实的实际安装目录
   & .\Update.ps1 -Target $install -NonInteractive
   ```
4. 检查退出码和安装目录下本次 `update-result.json`。仅 `updated` 或 `already_current` 且版本为 `2026.11.23` 表示完成更新；前者的备份目录里有 `selfcheck.json`。用原快捷方式免费启动一次，查看素材库和设置页，不查看密钥、不点收费生成。报告出现 `rolled_back` 表示已恢复旧程序，查原因后才能再更；`failed`、异常退出或未生成新报告都不能报成功。
5. 需要回退时先保存退出，用同包“回退上次更新.cmd”，或在解压目录运行 `& .\Update.ps1 -Target $install -Rollback -NonInteractive`。核对 `rolled_back`、原版本和启动结果。保留 `.lianghe-updates` 备份、journal 及报告；遇到未完成事务不要删除 `.lianghe-update.json`，再次运行同包先按事务恢复。
6. 保留原 `JDMIX_DATA_DIR`、个人数据库、API Key、缓存、素材、音乐、成片、runtime/models/bin/fonts。不修改源目录检测结果来绕过不匹配。若官方旧安装不能识别，先汇报版本、错误和修改过的程序文件；不要发送个人数据或整份日志。若用户明确要按源码更新，另见 [WORKBUDDY_SOURCE.md](https://github.com/gq741/lianghehunjian/blob/v2026.11.23/WORKBUDDY_SOURCE.md)。

安装验收结束后，由用户自行决定是否重试原任务。已缓存的步骤应复用；显示新增收费步骤时，必须由用户重新确认。不要将安装成功描述成已完成用户素材的出片验收。
