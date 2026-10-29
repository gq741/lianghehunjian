# 量禾混剪

面向 Windows 的本地带货视频混剪工作台。允许制作和使用商业带货成片；项目自有软件允许免费使用、修改与免费分享，禁止软件销售、白标转售、收费部署及收费托管。第三方组件独立适用原许可证。详见 [LICENSE.txt](LICENSE.txt) 与 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 下载完整软件

进入 **[最新软件下载页](https://github.com/gq741/lianghehunjian/releases/latest)**。当前版是 **v2026.10.29**，最简入口：

1. 下载 [Download-Release.cmd](https://github.com/gq741/lianghehunjian/releases/download/v2026.10.29/Download-Release.cmd) 和 [Download-Release.ps1](https://github.com/gq741/lianghehunjian/releases/download/v2026.10.29/Download-Release.ps1)，放进同一个新的短目录，例如 `D:\LiangHeDownload29`。
2. 双击 `Download-Release.cmd`，自动下载该版分块、校验、还原到同目录的 `LiangHeMix`，并运行免费自检。每块至多 100,000,000 字节，数量以 `PARTS.json` 为准；中断后重跑会复用已校验附件。
3. 自检通过后打开 `LiangHeMix\量禾混剪.exe`。首次填写自己的 API Key；收费生成仍由用户当前操作确认。

**“Code → Download ZIP”和 GitHub 自动生成的“Source code”是源码，不是完整可运行软件。** 不要单独解压 `.part`。完整步骤及报告说明见 [WORKBUDDY.md](WORKBUDDY.md) 和 [START_HERE.txt](START_HERE.txt)。

## 更新前先备份

先保存任务并退出旧软件；确认并备份实际用户数据目录，默认是 `%LOCALAPPDATA%\JDMixStudio\v1`。源素材和成品保持原位置。把新版还原到新目录，不覆盖旧软件、不删除旧包和用户数据。相同 Windows 账号下原 DPAPI Key 可延续；换账号/换电脑需重新填写自己的 Key。更新后先检查旧任务、音频与 Fish 设置是否保留，再做新任务。详细步骤见 [更新流程](WORKBUDDY.md#更新流程)。

没有自动更新。旧 `Download-Release` 固定旧版本，不会升级到 29；更新时必须从最新发布页重新下载新版两个入口。

## 使用条件

- Windows 10 22H2 / Windows 11，Intel/AMD x64，默认 CPU。
- Python、锁定依赖、本地模型、FFmpeg 随完整包提供，无需新装 Python、7-Zip 或 CUDA。缺少系统组件时仅按检测提示使用随包官方安装器；安装确认和重启由用户处理，之后重新免费自检。
- 默认配音为 Fish；MiniMax 使用独立的国内开放平台 Key。切换前在设置中选定 HD 或 Turbo 模型、音色并保存；换服务后新建生成任务，不跨服务重试旧任务。
- 本地免费自检及启动门禁只说明当前电脑的本地运行条件，不等于新电脑或真实云 API 已通过。验证记录以该版发布证据为准。

## 仓库与许可

`app/` 是应用源码；`release-tools/` 是分包、下载、还原工具及离线测试。运行时、模型、系统安装器及第三方来源资料在 Releases 完整附件中，不在源码 ZIP 中。用户 Key、AppData、素材、历史音频和登录状态不属于公开包内容。
