# WorkBuddy：直接从 GitHub 同步量禾混剪源码

此为用户主动选择的**手动源码同步**路线，适用于已核实来源的文件夹版。常规更新优先使用 [2026.11.23 小更新包](WORKBUDDY.md)，两种方式选其一。若旧安装来源或现有程序改动不明，先对照正式旧版或已验证的同步备份核查；不要用手工覆盖绕过更新器的保护。

仓库：https://github.com/gq741/lianghehunjian
固定标签：v2026.11.23。先核对来源、记录它的完整 commit，并在整个操作中固定该提交，不混用 main 和其他版本。

## 操作步骤

### 1. 定位、退出、备份

- 根据快捷方式定位用户实际使用的安装根目录（下称 INSTALL），其中应有量禾混剪.exe、runtime、app、models、bin。存在多个目录先让用户确认。不要把更新 ZIP 的解压目录或新克隆目录当成 INSTALL。
- 让用户保存修改，结束任务并退出所有使用该安装目录的量禾混剪进程；不强行终止正在生成的任务。更新期间不启动软件。
- 确认实际用户数据路径：默认 `%LOCALAPPDATA%\JDMixStudio\v1`，自定义 `JDMIX_DATA_DIR` 和原快捷方式参数保持。用户可先做本地数据备份；不上传，不显示密钥内容。
- 在 INSTALL 下新建独立的 `.source-update-backups/时间戳`，按即将覆盖的文件逐个备份原字节和 SHA256，记录原先不存在的新文件。不要使用整个目录删除、镜像同步、git clean 或 git reset --hard。

### 2. 获取并校验同一提交的源码

将 $tempRepo 设为新的临时目录路径，不在安装目录内拉取：
```powershell
git clone --branch v2026.11.23 --single-branch https://github.com/gq741/lianghehunjian.git "$tempRepo"
git -C "$tempRepo" rev-parse HEAD
```
未装 Git 时可下载 GitHub 对应提交的源码归档，先查清固定标签所指向的完整提交号；源码归档只用于取程序文件，不是重新安装软件。

- 读取仓库根目录 `SOURCE_FILES.sha256.json`。`files[].path` 相对于仓库 `app/`，本次为47个运行所需源码/静态资源/启动辅助文件。
- 对同一提交下每个文件检查字节数和 SHA256。路径必须留在仓库 `app/` 和目标 `INSTALL/app/` 中，拒绝 `..`、绝对路径、重定向目录；不跟随链接跨目录写入。
- 源码清单与旧 `FILES.sha256.json` 用途不同：前者验证要同步的新源码，后者是原完整发行包记录。不要修改旧清单来冒充兼容，也不要把新源码清单覆盖成旧清单。
- 对比新旧 `requirements-lock.txt`。本次依赖没有新增，继续使用旧包内 Python、模型、FFmpeg、字体和系统组件；不重建环境、不批量升级 pip 依赖。以后锁定依赖确有变化时，先报告所需调整，不带病复制后声称完成。
- 仓库 `app/mixstudio/media.py` 已保留随包字体适配，不从开发机其他目录另找同名文件覆盖。

### 3. 仅同步清单中的文件

- 在全部原文件备份完成后，把清单列出的 `仓库/app/<path>` 逐文件更新到 `INSTALL/app/<path>`，使用同目录临时文件后替换，并逐项校验落盘 SHA256。不要先删掉整个 app 文件夹。
- 不替换 INSTALL 根目录的 EXE、快捷方式、runtime、models、bin、fonts、系统安装器；不移动素材、音乐、输出和缓存。
- 不删除未列入清单的文件，不读取或展示 API Key；保留原 Windows 账号的 DPAPI 凭据。
- 原 `RELEASE.json`、`FILES.sha256.json` 原样保留。它们表示最初的完整包，不再单独用于判定源码是否最新。
- 如果已有 `.lianghe-update.json` 中断标记，先查看对应程序更新日志与备份、恢复未完成事务；不要直接删除标记绕过恢复。中断恢复要有备份和事务记录作为依据。

### 4. 免费验证，成功才记为完成

使用安装包自带 Python（不要调用系统 Python）：
```powershell
& "$INSTALL\runtime\python.exe" -I -B -X utf8 "$INSTALL\app\tools\portable_check.py" --output "$BACKUP\source-selfcheck.json"
```
`$INSTALL`、`$BACKUP`替换为本次实际安装根目录、备份目录。检查进程退出码为0、报告 `ok=true`。随后用原快捷方式/EXE启动，确认素材库、旧文案/任务、音乐、Fish/MiniMax选项正常；密钥仅核对“已配置”状态，不回显。不要点收费生成来验收。

成功后在 INSTALL 写入 `SOURCE_UPDATE.json`，记录：`mode=workbuddy-source`、仓库、分支、完整 commit、更新时间、实际安装路径、新源码清单 SHA256、逐文件校验结果、备份路径、自检报告及退出码。报告不含个人密钥。

如任何阶段失败：停止更新，按本次备份逐项恢复原程序；原先不存在的新文件，仅在字节仍匹配本次新源码时移除。恢复后校验原哈希，保留报告，不用旧数据库覆盖当前数据。不执行仓库 `app/ROLLBACK.sh`，它是历史开发工具，不是本次回退入口。


## 后续更新

优先按下一正式 Release 的说明更新；若继续手工同步，重新固定对应发布标签并核验该标签的清单。不要运行旧 11.04 或 11.05 更新包覆盖较新源码。保留旧发行清单和 SOURCE_UPDATE.json 两份记录；手工同步不改写旧发行版本号。

## 可直接复制给 WorkBuddy

```text
请按 https://github.com/gq741/lianghehunjian/blob/v2026.11.23/WORKBUDDY_SOURCE.md 给我的量禾混剪做手动源码同步。核实官方旧安装和实际目录，保存退出、备份，固定 v2026.11.23 标签和完整提交，再校验并同步 SOURCE_FILES.sha256.json 中的文件。
保留原有依赖、字体、API Key、个人设置、素材、文案、任务、音乐、音频和分析缓存。不要改旧发行清单或覆盖未知程序改动；只做免费自检和原 EXE 启动验证，失败回退本次已改文件，报告结果与备份位置。
```
