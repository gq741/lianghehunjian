# WorkBuddy：量禾混剪程序更新

仅更新既有的官方 2026.10.24 / 2026.10.29 Windows x64 文件夹版；首次安装需要先下载完整 10.29 包。

1. 让用户保存工作、结束任务并退出软件，定位量禾混剪.exe及其同级 RELEASE.json、runtime、app。
2. 下载本次发布的 LiangHeMix-Update-2026.11.04.zip；用同页 UPDATE_FEED.json核对 SHA256，完整解压到新的文件夹。
3. 运行更新包 Update.ps1 -Target "实际安装目录" -NonInteractive，或双击更新量禾混剪.cmd后选择安装目录。
4. 工具检查旧版本及程序哈希，备份后替换程序文件，免费自检失败自动恢复。不要跳过检查、修改清单或安装新依赖。
5. 读取安装目录 update-result.json，确认 updated / already_current；rolled_back 表示只完成恢复，尚未安装新版。failed 表示未完成。
6. 确认旧素材、任务、音频及设置仍在；不读取或展示任何 API Key。不调用收费生成。模型与运行时不需要重下。
7. 更新后用原 Windows 账号和原快捷方式启动，在设置里检查更新。自定义 JDMIX_DATA_DIR保持原值。
8. 回退使用同包 Update.ps1 -Target "实际安装目录" -Rollback；仅恢复程序，不恢复/覆盖用户数据库或配置。

用户数据目录默认 %LOCALAPPDATA%\JDMixStudio\v1；不移动、不清空。原素材/音乐/输出目录不动。程序备份位于安装目录 .lianghe-updates，保留供回退。
网络问题可以改为朋友传送同一完整 ZIP，离线运行本更新，不要求 WorkBuddy 修改程序。

给 WorkBuddy 的可复制提示词：
```text
请更新我电脑上的量禾混剪。先定位现有完整安装目录并让我保存退出。
下载 https://github.com/gq741/lianghehunjian/releases/tag/v2026.11.04 中的更新 ZIP 和 UPDATE_FEED.json，校验后完整解压，按本说明运行 Update.ps1。
保留我的 API Key、设置、任务、素材和缓存；不安装新依赖，不调用收费模型。
读取 update-result.json 和免费自检报告，确认成功后启动软件；失败时保留日志与备份，不强制覆盖。
```
