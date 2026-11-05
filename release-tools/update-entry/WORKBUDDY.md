# WorkBuddy：量禾混剪 2026.11.05 排片修复

请定位用户已安装的量禾混剪，保存工作、结束任务并退出。
从本 Release 下载 LiangHeMix-Update-2026.11.05.zip 和 UPDATE_FEED.json，核对大小及 SHA256，完整解压到新的目录。
使用更新包 Update.ps1 -Target "实际安装目录" -NonInteractive，读取 update-result.json 和 selfcheck.json。
只有 updated / already_current 表示已更新；rolled_back 表示恢复了旧版，需要重新执行更新。
保留 API Key、设置、任务、原视频、音乐和音频/分析缓存。不要重装依赖、修改清单或绕过校验。
运行原 EXE 做免费启动检查，不自动调用模型，不自动重试付费任务。
如需回退，用同包 Update.ps1 -Target "实际安装目录" -Rollback。

更新后请用户在原失败成片任务点一次“重试”，保持原文案和音色以复用缓存。
如果仍出现画面缺口，打开软件日志文件夹，发最新 tasks-日期.jsonl 给维护者；新版小规模排片失败会写入匿名纯数字复现数据。
此包改善已复现的排片分配问题，不声明远端任务或真实素材已完成验收。
