# 量禾混剪：GitHub 下载与 WorkBuddy 接收说明

仓库：https://github.com/gq741/lianghehunjian
发布页：https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.24

## 一、最简单的下载使用方式
1. 打开发布页，只需先下载 `Download-Release.cmd` 和 `Download-Release.ps1` 这两个小入口到同一短目录，例如 `D:\LiangHeDownload`。
2. 双击 `Download-Release.cmd`。它匿名访问本次公开Release，自动下载全部37个100MB以内的小分块、校验文件和还原工具。
3. 下载中断时再次运行，会复用已经校验通过的小分块；每个未完成附件有限重试。现有同名但错误的正式文件会停止，不擅自覆盖。
4. 下载后自动调用还原工具，逐项校验分块、ZIP和7207项文件清单，恢复为同目录 `LiangHeMix`，并免费自检。
5. 自检全部PASS后，打开 `LiangHeMix\量禾混剪.exe`；填写自己的API Key，选择素材、下载和输出目录。
6. 以后直接运行软件，不用每次打开WorkBuddy、下载或还原。

## 二、手动下载或交给WorkBuddy
- 发布页Assets共有47个附件。可以将全部附件下载到同一目录（其中37个分块）；数量和顺序以 `PARTS.json` 为准。
- 不单独解压part，不修改文件名；GitHub自动的Source code不是完整可运行包。
- 若已经手动下载齐，直接运行 `Restore-Release.cmd`，即可跳过网络下载阶段。
- 不需要预装Python、7-Zip或CUDA；依赖和本地模型随包提供。系统缺组件时按说明使用还原包内微软离线安装器。

## 三、WorkBuddy 操作提示（可直接复制）
```text
量禾混剪发布附件已下载到 D:\LiangHeDownload（请替换为实际目录）。
先读取 WORKBUDDY.md 和 START_HERE.txt，先运行 Download-Release.cmd 自动下载全部软件分块并校验、恢复及免费自检；
如果已经手动下载齐，核对 SHA256SUMS.txt / PARTS.json，再运行 Restore-Release.cmd。
缺系统组件时使用还原文件夹中的官方离线安装器；需要确认/重启时交给用户处理，之后重新自检。
保持锁定依赖和业务代码不变，不重建虚拟环境，不额外下载模型。
完成后启动量禾混剪.exe，由用户自己在界面填写Key。
本次只做免费检测与启动；真实收费出片先向用户确认本次调用范围。
```

## 四、检测结果与常见情况
- 下载入口会写 download-report.json；-DownloadOnly用于只下载和校验，不自动还原。
- 入口默认继续执行还原与免费检测，不会调用收费模型。
- CMD退出0：文件还原与免费自检都通过。
- 退出1：下载/文件恢复等过程未通过。先查看同目录 `download-report.json`；只有已经进入还原阶段，才会生成 `LiangHeMix-restore-report-*.json`。根据报告补齐缺失或损坏的分块，不删除已校验通过的文件。
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
