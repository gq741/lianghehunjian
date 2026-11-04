# 程序增量更新协议 v1

客户端只在用户点击后读取官方 GitHub 仓库公开 Releases，不使用或上传模型 Key。筛选非草稿、非预发布版本，按数字版本排序，使用 UPDATE_FEED.json 及 GitHub asset digest 校验下载。

UPDATE_FEED.json 包含 format=1、version、base_manifests（支持的旧 FILES.sha256.json SHA256）、archive{name,bytes,sha256}。ZIP 内含 UPDATE.json、payload、独立 apply_update.py、Update.ps1/CMD。UPDATE.json 包含 files[{path,bytes,sha256}] 和 bases[旧清单SHA256]={version,old:{path:SHA256或null}}。

更新器只接受 allowlist 中的程序与说明文件，不更新运行时、模型、用户数据或任意外部路径。更新前验证旧版本、应用文件、补丁与路径，检测正在运行的安装目录进程。先保存全部程序原文件备份与持久化 journal，再设置启动阻断标记，原子替换文件；自检成功才清除标记。失败自动恢复；中断后再次运行先恢复。回退前检查当前文件仍是本次更新前/后的已知字节，保留后续手工改动。

只做同运行时版本的程序更新。未来依赖发生变化时应发布新的完整包，不伪装成此格式的程序补丁。

发布检查：旧版两个独立副本升级/重复运行/回退；SHA256和个人数据哨兵保留；取消与未保存输入阻止退出；运行中拒绝覆盖；损坏/错误版本/路径越界阻止；本地完整测试及原生启动。禁止上传 AppData、密钥、素材或运行日志。

给维护助手的提示词：
```text
修复量禾混剪后，保持用户数据路径与锁定依赖不变。
基于官方已发布 FILES.sha256.json 的已验证旧安装副本制作更新；保留 portable 字体适配器。
在独立副本测试升级、免费自检、回退和数据保留，运行完整离线测试，禁止模型付费调用。
生成 UPDATE_FEED.json、UPDATE.json 和程序小更新 ZIP，扫描密钥，先创建草稿 Release。
校验远端每个附件 SHA256 后发布，再以匿名下载验证客户端检查/下载入口；不要改动旧 Release 附件。
```
