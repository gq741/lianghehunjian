# 量禾混剪

允许商业带货成片的 Windows 本地混剪工作台。自有代码允许免费使用、修改与免费分享，禁止软件售卖和收费部署；第三方组件按原许可证。详见 [LICENSE.txt](LICENSE.txt)。这是自定义许可的公开源码项目，不是MIT/Apache式自由商用授权。

## 朋友下载使用

请进入 **[Releases软件下载页](https://github.com/gq741/lianghehunjian/releases/tag/v2026.10.24)**，下载全部附件并按 [WorkBuddy说明书](WORKBUDDY.md) 操作。
GitHub的“Code → Download ZIP”和Release自动的“Source code”只有源码，不能代替完整运行包。

下载4个part与恢复工具等附件到同一短路径，例如 `D:\LiangHeDownload`，双击 `Restore-Release.cmd`；校验并自检通过后运行恢复目录的 `量禾混剪.exe`。
第一次可以让WorkBuddy负责检测，以后直接运行软件。不需要用户预装Python、CUDA或7-Zip。系统缺组件时使用随包官方安装器。

目标为Windows10 22H2/Windows11 Intel/AMD x64，默认CPU。用户在软件界面填写自己的DeepSeek/GLM/Fish Key，云服务费用由自己的账号承担。

## 仓库结构
- `app/`：本次发布对应的应用源码、界面、提示词与离线回归测试。
- `release-tools/`：仅用于分包/还原的源代码及测试。
- `WORKBUDDY.md`：接收电脑的操作顺序和可直接复制的任务提示。
- 运行时、模型、系统安装器、第三方来源归档放在Releases附件中，不塞进Git历史。

## 验证说明
本机换目录/隔离配置测试，本地模型与合成视频导出已验证。没有声称在所有新电脑或所有云接口环境通过。接收端先做免费自检；收费操作由用户当前确认。
原开发电脑的API Key、AppData、素材、配音缓存与登录状态未上传。

## 第三方
FFmpeg等原始许可证和精确来源资料随完整发布包提供；[第三方声明](THIRD_PARTY_NOTICES.md)。项目自有许可的售卖/部署限制不限制第三方原许可证已经赋予的权利。
