# 量禾混剪：第三方组件说明

本目录以完整运行环境形式分发。第三方组件的原始版权声明与许可证继续有效；Python 包的 `*.dist-info`、包内 `LICENSE` / `NOTICE` / `COPYING` 文件应原样保留。本文件不能替代这些完整文本。

## Python 运行时与应用依赖

- CPython 3.12.14：Python Software Foundation License；完整文本随运行时 `LICENSE.txt` 提供。此运行时来自 Astral python-build-standalone，由 uv 管理，运行不要求安装 uv。
- 版本清单以软件内的 `requirements-lock.txt` 及随包 Python 包 METADATA 为准。实际构建时应生成组件版本/文件哈希清单。
- 额外补充的 CTranslate2、FlatBuffers、proxy_tools、RapidOCR、Tokenizers 原始许可证应放在 `licenses/`；来源及哈希保存在 `SOURCES.json`。
- NumPy/OpenCV/PyAV/CTranslate2/ONNX Runtime 所带原生库仍适用其原始第三方许可证，不能只保留 Python 顶层包名称或只复制 `.py` 文件。

## 本地语音与 OCR 模型

- ASR：`Systran/faster-whisper-small`，快照 `536b0662742c02347bc0e980a01041f333bce120`，基于 OpenAI Whisper small 转换，模型页标注 MIT。保留原模型文件并随附 Whisper LICENSE。
- OCR：RapidOCR ONNX Runtime 1.4.4 所含中文识别、检测和方向分类 ONNX 权重；其包、模型及来源声明应完整保留。
- 模型文件不包含本机任务、素材、缓存配音、用户音色或 API 密钥。

## FFmpeg / ffprobe

- 发布包选用 BtbN `n9.0.2-35-g9a9b70bb8c-20261023` Windows x64 GPL shared 构建，构建选项含 `--enable-gpl --enable-version3 --enable-shared`，按 GPLv3 及其组件相应条款分发。
- `ffmpeg.exe` / `ffprobe.exe` 与同目录七个 av*/sw* DLL 必须一起保留，不混用其他构建的 DLL。随附原包 `LICENSE.txt`、官方文档和实际构建选项输出；不可用普通 LGPL 描述代替 GPL 构建许可证。
- 构建工作流：https://github.com/BtbN/FFmpeg-Builds/actions/runs/39951798514
- FFmpeg 精确主源码版本：`9a9b70bb8c96c633f6e588c79f769964b10213b5`。
- BtbN 构建脚本版本：`937868360a21d711ec94a33f5591d345ad9b7754`。
- 对应来源归档包括上述两个项目的精确源码快照、同一构建工作流的 `download-cache` 依赖源码归档；三者与文件哈希记录应一起放在 `third-party-sources/btbn/`。仅留网页链接不等于交付源归档。
- 依赖源归档来自 GitHub artifact `12500249833`，官方记录大小 `2210375107` 字节，SHA256 `fd7c398de680e187fd440d18fa808e097e604503bbc158e8bd1d03ac81444251`。发布前应确认 `SOURCE_CACHE_VERIFIED.json` 的完整性结果为通过。
- FFmpeg 由应用作为独立进程调用。本包保留第三方原始源码材料，但没有声称在接收电脑上重新编译或逐字节复现 BtbN 二进制。
- 开发电脑原 Gyan `9.0.1-full_build` 仅用作开发基线，本发布包不分发它。

## PyAV 与其独立 FFmpeg 运行库

- PyAV 18.1.0 是 BSD 许可的 Python 绑定，其原始版权与许可证保留于运行时 `av-18.1.0.dist-info/licenses/`。
- 该 Windows wheel 的 `av.libs` 是另一套 FFmpeg 8.1.2 共享运行库；运行库报告的许可证为 LGPLv3 或更新版本。它与上面的 BtbN FFmpeg 9.0 构建不同，不能混称同一份许可或源码覆盖。
- PyAV 官方 tag `v18.1.0` 的 `scripts/ffmpeg-latest.json` 指向 `PyAV-Org/pyav-ffmpeg` release `8.1.2-1`。独立来源材料保留 PyAV v18.1.0 源码、该 vendor 构建脚本快照、FFmpeg n8.1.2 主源码，以及 LGPLv3/GPLv3 和许可说明文本。
- 每个来源归档的下载地址、版本与 SHA256 见 `third-party-sources/pyav/PYAV_SOURCE_RECORDS.json`；该记录包含实际加载的运行库版本、许可证字符串和构建选项。它是上游来源记录，不宣称本地重新构建过该 wheel，也不宣称 BtbN 的依赖缓存覆盖这一套运行库。

## 中文字体

若附带 Noto Sans CJK SC，应保持字体文件原样，并随附上游 `Sans/LICENSE`（SIL Open Font License 1.1）。不要将 Windows 自带 Microsoft YaHei 字体从开发电脑复制到软件包中。字体来源：https://github.com/notofonts/noto-cjk/tree/main/Sans/OTF/SimplifiedChinese

## Microsoft 系统组件

- Microsoft Edge WebView2 Evergreen Runtime 与 Microsoft Visual C++ v14 x64 Redistributable 应使用微软官方签名的完整安装器，其自身许可文本由安装器提供。
- WebView2 Runtime：https://developer.microsoft.com/en-us/microsoft-edge/webview2/
- Visual C++ Runtime：https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
- .NET Framework 为 Windows 系统组件，启动前检测；不复制开发电脑 Windows 系统目录里的 DLL 代替官方安装。

## 部署要求

目标为 Windows 10 22H2 / Windows 11 x64、支持 SSE4.1 的 Intel/AMD x86-64 CPU。默认 CPU 推理不要求 NVIDIA CUDA。Windows N/KN 版本缺少 Media Foundation 时，须通过 Windows 官方可选功能补齐 Media Feature Pack。完整运行环境并不包含 Windows 操作系统组件本身。

## 云服务

DeepSeek、GLM 与 Fish Audio 是联网服务，不因附带运行时变为离线模型。接收者在自己的 Windows 用户账户下配置自己的密钥，开发者的账号信息不随包分发。
