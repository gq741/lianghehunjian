# 量禾混剪

Windows 本地带货视频混剪工作台。允许制作商业带货成片；自有软件允许免费使用、修改、免费分享，禁止软件销售、白标转售、收费部署和收费托管。第三方组件适用原许可证，见 [LICENSE.txt](LICENSE.txt) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 已装软件：2026.11.05 排片修复更新

[下载更新包（约 1 MB）](https://github.com/gq741/lianghehunjian/releases/download/v2026.11.05/LiangHeMix-Update-2026.11.05.zip) · [发布记录](https://github.com/gq741/lianghehunjian/releases/tag/v2026.11.05) · [WorkBuddy 操作说明](WORKBUDDY.md)

保存退出软件 → 完整解压更新 ZIP → 双击“更新量禾混剪.cmd” → 按需选择旧版安装文件夹 → 免费自检通过后用原快捷方式启动。

- 修复有可行分配却报画面不足的一类问题：增加有界回溯，重新分配被先前段落占用的相关候选；仍遵守原片范围、重复额度和最低画面速度。
- 分开显示口播时长与已配净画面；统一分组编号。失败日志附带受限的匿名数字排片案例，便于离线复现，不含文案、素材路径或密钥。
- 兼容已知 MiniMax 10.29 元数据不同的旧包、11.04 和 WorkBuddy 源码同步；校验实际程序字节，不强行覆盖未知代码。
- 保留 API Key、任务、素材、音乐、缓存和已有依赖；更新失败回退。真实素材不足不靠无关画面填充，朋友原任务仍需更新后验收。

更新后在**原失败成片任务**重试一次，不要重新生成文案或清空音频/分析缓存。安装和自检不调用收费模型。
若仍失败，发送新的普通任务日志；无需先导出整个数据库。

[手动源码同步说明](WORKBUDDY_SOURCE.md) 继续保留；源代码清单为 [SOURCE_FILES.sha256.json](SOURCE_FILES.sha256.json)。不要使用旧 11.04 更新包更新新版。

## 首次安装：完整包后同步源码

小更新包不是首次安装包。新电脑请先使用 [v2026.10.29 完整包](https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.29)：

1. 下载该页面的 `Download-Release.cmd` 和 `Download-Release.ps1` 到同一个新文件夹。
2. 双击 CMD 自动下载分块、校验、还原并免费自检；中断可重跑，已验证附件可复用。
3. 然后应用上方 2026.11.05 更新包，或按源码同步说明更新。不要单独解压 `.part`。

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
