# 量禾混剪

Windows 本地带货视频混剪工作台。允许制作商业带货成片；自有软件允许免费使用、修改、免费分享，禁止软件销售、白标转售、收费部署和收费托管。第三方组件适用原许可证，见 [LICENSE.txt](LICENSE.txt) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 已装软件：WorkBuddy 源码更新

**目前推荐直接让 WorkBuddy 从本仓库 main 同步源码，不再使用 11.04 小更新包。** 适用于已装量禾混剪、包括带 MiniMax 但旧清单不匹配的安装。无需重新下载几个 GB 的运行时和模型。

把这段发给 WorkBuddy：
```text
请读取 https://github.com/gq741/lianghehunjian/blob/main/WORKBUDDY.md，按源码同步路线更新现有量禾混剪。
先确认实际安装目录并让我保存退出，备份旧程序；获取main并固定提交，按SOURCE_FILES.sha256.json校验后逐文件同步app源码。
保留我的API Key、设置、任务、素材、音乐、缓存与原有依赖；不运行11.04更新包，不修改旧发行清单来绕过校验。
完成免费自检和原EXE启动检查，不调用收费模型。失败恢复旧程序；成功记录SOURCE_UPDATE.json并报告提交号和备份路径。
```

完整步骤见 [WORKBUDDY.md](WORKBUDDY.md)，新源码清单见 [SOURCE_FILES.sha256.json](SOURCE_FILES.sha256.json)。不要把仓库源码 ZIP 当成完整安装包，也不要把整个仓库直接覆盖到安装根目录。

11.04 小更新包作为历史发布保留；它只识别两份固定发行清单，并不覆盖所有同日期旧包。此次源码更新不要求旧清单整体指纹相同，仍检查新代码字节、依赖、备份及运行结果。

## 首次安装：完整包后同步源码

小更新包不是首次安装包。新电脑请先使用 [v2026.10.29 完整包](https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.29)：

1. 下载该页面的 `Download-Release.cmd` 和 `Download-Release.ps1` 到同一个新文件夹。
2. 双击 CMD 自动下载分块、校验、还原并免费自检；中断可重跑，已验证附件可复用。
3. 然后按上方 WorkBuddy 源码更新步骤同步当前 main。不要单独解压 `.part`。

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
