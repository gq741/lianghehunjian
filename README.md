# 量禾混剪

Windows 本地带货视频混剪工作台。允许制作商业带货成片；自有软件允许免费使用、修改、免费分享，禁止软件销售、白标转售、收费部署和收费托管。第三方组件适用原许可证，见 [LICENSE.txt](LICENSE.txt) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 已装软件：下载小更新包

当前更新 **v2026.11.04**，约 **0.55 MB**，支持官方 2026.10.24 GitHub 分包版、2026.10.29 文件夹版。

1. 保存工作、结束任务并退出软件。
2. [下载更新 ZIP](https://github.com/gq741/lianghehunjian/releases/download/v2026.11.04/LiangHeMix-Update-2026.11.04.zip)，完整解压到新文件夹。
3. 双击“更新量禾混剪.cmd”，按需选择旧版 EXE 所在文件夹。
4. 更新与免费自检通过后，重新打开原来的软件。个人密钥、设置、任务、素材、音乐和缓存保留。

更新包包含 MiniMax 与已确认的画面空匹配、音乐清单、历史索引修复。无需重下模型、Python 或 FFmpeg。

之后在“设置 → 软件更新”中检查、下载，保存退出后安装；不后台静默更新。网络不通时也可由朋友传送同一个 ZIP，离线更新。
程序备份位于安装目录 `.lianghe-updates`；回退用更新包“回退上次更新.cmd”。如果中断，再运行入口先恢复旧程序，恢复完成后可重新更新。

[本次发布说明](https://github.com/gq741/lianghehunjian/releases/tag/v2026.11.04) · [WorkBuddy 更新说明](WORKBUDDY.md)

## 首次安装：完整包 + 更新包

小更新包不是首次安装包。新电脑请先使用 [v2026.10.29 完整包](https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.29)：

1. 下载该页面的 `Download-Release.cmd` 和 `Download-Release.ps1` 到同一个新文件夹。
2. 双击 CMD 自动下载分块、校验、还原并免费自检；中断可重跑，已验证附件可复用。
3. 然后按上方步骤应用 v2026.11.04 更新。不要单独解压 `.part`。

**GitHub 的 Source code / Code ZIP 是源码，不是可运行完整软件。** 旧 Download 入口固定 10.29，不会自动取得新补丁。

## 使用条件与数据

- Windows 10 22H2（19045）/ Windows 11，Intel/AMD x64，默认 CPU。
- 完整包包含 Python、模型、FFmpeg；无需新装 Python、7-Zip 或 CUDA。系统组件按免费自检提示使用随包官方安装器。
- 数据默认在 `%LOCALAPPDATA%\JDMixStudio\v1`；自定义 `JDMIX_DATA_DIR` 保持原值。更新不迁移素材/成品目录。
- 原 Windows 账号下保留 DPAPI 密钥；换电脑或 Windows 账号需重新填自己的 API Key。Fish 和 MiniMax 的密钥独立，不自动跨服务重试。
- 测试区分本地自检、模拟云响应和真实 API；本次更新不调用收费模型，不代表朋友的云账号与网络已验收。

## 源码与更新协议

`app/` 是源码；`release-tools/` 是下载、还原及更新发布工具。大型运行时与模型放在完整 Release；个人数据、API Key、素材和登录状态不入仓库。
更新格式见 [UPDATE_PROTOCOL.md](release-tools/UPDATE_PROTOCOL.md)。
