# WorkBuddy：直接从 GitHub 同步量禾混剪源码

本说明用于已经安装量禾混剪的电脑，尤其是已增加 MiniMax、但 11.04 更新包提示“安装版本不匹配”的旧安装。**本轮不下载、不运行 Release 更新包，不要求旧发行清单整文件哈希相同。** 保留现有运行时、模型和个人数据，只同步程序源码。

仓库：https://github.com/gq741/lianghehunjian
分支：main。每次先获取远端最新提交，记录完整 commit，并在本次操作中固定该提交，避免文件来自不同版本。

## 操作步骤

### 1. 定位、退出、备份

- 根据快捷方式定位用户实际使用的安装根目录（下称 INSTALL），其中应有量禾混剪.exe、runtime、app、models、bin。存在多个目录先让用户确认。不要把更新 ZIP 的解压目录或新克隆目录当成 INSTALL。
- 让用户保存修改，结束任务并退出所有使用该安装目录的量禾混剪进程；不强行终止正在生成的任务。更新期间不启动软件。
- 确认实际用户数据路径：默认 `%LOCALAPPDATA%\JDMixStudio\v1`，自定义 `JDMIX_DATA_DIR` 和原快捷方式参数保持。用户可先做本地数据备份；不上传，不显示密钥内容。
- 在 INSTALL 下新建独立的 `.source-update-backups/时间戳`，按即将覆盖的文件逐个备份原字节和 SHA256，记录原先不存在的新文件。不要使用整个目录删除、镜像同步、git clean 或 git reset --hard。

### 2. 获取并校验同一提交的源码

在新的临时工作目录克隆/获取仓库，不在安装目录内拉取：
```powershell
git clone --branch main --single-branch https://github.com/gq741/lianghehunjian.git <新的临时目录>
git -C <新的临时目录> rev-parse HEAD
```
未装 Git 时可下载 GitHub 对应提交的源码归档，先查清 main 的完整提交号；源码归档只用于取程序文件，不是重新安装软件。

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
- 如果已有 `.lianghe-update.json` 中断标记，先查看对应程序更新日志与备份、恢复未完成事务；不要直接删除标记绕过恢复。本次截图中的“版本不匹配”发生在替换前，通常没有该标记。

### 4. 免费验证，成功才记为完成

使用安装包自带 Python（不要调用系统 Python）：
```powershell
& "$INSTALL\runtime\python.exe" -I -B -X utf8 "$INSTALL\app\tools\portable_check.py" --output "$BACKUP\source-selfcheck.json"
```
`$INSTALL`、`$BACKUP`替换为本次实际安装根目录、备份目录。检查进程退出码为0、报告 `ok=true`。随后用原快捷方式/EXE启动，确认素材库、旧文案/任务、音乐、Fish/MiniMax选项正常；密钥仅核对“已配置”状态，不回显。不要点收费生成来验收。

成功后在 INSTALL 写入 `SOURCE_UPDATE.json`，记录：`mode=workbuddy-source`、仓库、分支、完整 commit、更新时间、实际安装路径、新源码清单 SHA256、逐文件校验结果、备份路径、自检报告及退出码。报告不含个人密钥。

如任何阶段失败：停止更新，按本次备份逐项恢复原程序；原先不存在的新文件，仅在字节仍匹配本次新源码时移除。恢复后校验原哈希，保留报告，不用旧数据库覆盖当前数据。不执行仓库 `app/ROLLBACK.sh`，它是历史开发工具，不是本次回退入口。

## 后续更新

用户选择的是 WorkBuddy 源码同步路线。以后按同样流程获取 main，逐文件同步并记录新的 commit。不要再运行旧 11.04 更新包，也不要为通过它的检查而改写原发行清单。软件内发行包更新提示不等于 SOURCE_UPDATE.json 中记录的源码状态；以固定提交和源码校验为准。

## 可直接复制给 WorkBuddy

```text
请按 https://github.com/gq741/lianghehunjian/blob/main/WORKBUDDY.md 给我现有的量禾混剪做源码更新，不使用 Release 安装包或11.04更新包。
先定位正在使用的安装目录，让我保存退出，备份将覆盖的程序文件。把仓库 main 获取到新的临时目录并固定完整commit，按 SOURCE_FILES.sha256.json 校验并同步 app/ 下的源码。
保留现有 runtime/models/bin/fonts、API Key、设置、任务、音乐、素材、音频缓存和成品。不要伪造旧发行清单，不要清空安装目录或重建依赖环境。
用随包 Python 运行免费自检，并检查原EXE启动；不调用收费模型。失败恢复旧代码；成功记录 SOURCE_UPDATE.json，并告诉我提交号、备份位置和自检结果。
```
