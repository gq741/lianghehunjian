# 量禾混剪：GitHub 下载与 WorkBuddy 接收说明

仓库：https://github.com/gq741/lianghehunjian
发布页：https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.24

## 一、朋友怎样下载
1. 打开发布页，展开 Assets。下载4个 `LiangHeMix-20261024.zip.part01`～`.part04`，以及 `PARTS.json`、`Restore-Release.ps1`、`Restore-Release.cmd` 到同一文件夹。
2. 同时下载本说明书、`START_HERE.txt`、`SHA256SUMS.txt`、`LICENSE.txt` 和 `ReleaseNotes.md`，便于检测和查看授权。
3. 使用本地短路径，例如 `D:\LiangHeDownload`。四个part必须齐全、名称不变；浏览器若加上“(1)”等后缀，请保留一套原名文件。
4. GitHub 自动提供的“Source code (zip/tar.gz)”只有源代码，不是完整可运行软件；不要把它当成成品包。

## 二、安装与启动
1. 将下载目录交给 WorkBuddy，或直接双击 `Restore-Release.cmd`。
2. 工具先检查4个分块的大小/SHA256，合并并核验ZIP，再还原为同目录的 `LiangHeMix` 文件夹，并逐文件校验。
3. 文件校验通过后会免费自检。此过程不需要预装Python/7-Zip/CUDA，也不调用 DeepSeek/GLM/Fish。
4. 自检全部PASS后，打开 `LiangHeMix\量禾混剪.exe`。
5. 用户自己在软件设置中填写自己的API Key和音色，选择素材、下载和输出目录。
6. 以后直接运行软件；不需要每次重新下载、还原或启动WorkBuddy。

## 三、WorkBuddy 操作提示（可直接复制）
```text
量禾混剪发布附件已下载到 D:\LiangHeDownload（请替换为实际目录）。
先读取 WORKBUDDY.md 和 START_HERE.txt，核对 SHA256SUMS.txt / PARTS.json，
运行 Restore-Release.cmd 完成分块校验、恢复文件和免费自检。
缺系统组件时使用还原文件夹中的官方离线安装器；需要确认/重启时交给用户处理，之后重新自检。
保持锁定依赖和业务代码不变，不重建虚拟环境，不额外下载模型。
完成后启动量禾混剪.exe，由用户自己在界面填写Key。
本次只做免费检测与启动；真实收费出片先向用户确认本次调用范围。
```

## 四、检测结果与常见情况
- CMD退出0：文件还原与免费自检都通过。
- 退出1：下载/文件恢复等过程未通过。查看同目录 `LiangHeMix-restore-report-*.json`；缺哪一块、哪块SHA不符就补下该块。
- 退出2：文件已经恢复，环境尚待补齐。查看 `LiangHeMix/logs/selfcheck.json`，运行软件内 `安装系统组件.cmd`，需要时重启，再运行 `免费自检.cmd`，不必重新还原。
- 存在同名 `LiangHeMix` 时工具停止，不覆盖旧软件；请在新的空父目录恢复新版本。已有用户数据不删除。
- 还原时下载、临时ZIP、展开文件会同时占用空间，建议预留至少12GB可用空间；成功后临时ZIP自动清理。失败遗留临时路径记录在报告，先核实后处理。
- 请使用短目录；过深目录可能触发Windows传统路径长度限制。

## 五、适配范围和数据
Windows10 22H2（19045）/Windows11，Intel/AMD x64，CPU默认。随包有Python、锁定依赖、模型、FFmpeg、字体及微软离线组件安装器。
.NET/WebView2/Windows媒体功能属于系统组件；N/KN版缺媒体功能时用Windows官方可选功能补齐Media Feature Pack。
用户数据仍在当前用户 `%LOCALAPPDATA%/JDMixStudio/v1`，不会随软件文件夹移动；更新包不要删除它。
本包不包含维护者的API Key、运行数据库、历史素材、缓存音频或浏览器会话。
云服务需要用户自己的网络和账号额度；本地自检通过不等于本次已验收云服务收费出片。
第三方源码资料已放在还原目录 `third-party-sources`，保留原样，无需解压安装。

## 六、授权
项目自有代码允许商业带货视频及其他成片制作，禁止售卖软件、白标转售、收费部署及收费托管软件服务；需要额外授权请联系维护者。
第三方组件不受这项自有代码限制，继续适用其原许可证。完整文本见 LICENSE.txt 和随包 THIRD_PARTY_NOTICES.md。
