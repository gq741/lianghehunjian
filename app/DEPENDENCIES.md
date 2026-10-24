# 依赖和实现依据

运行依赖的确切版本见 requirements-lock.txt；开发测试使用 Python unittest，无单独 pytest 安装要求。

- [pywebview](https://pywebview.flowrl.com/guide/)：原生桌面容器，Windows采用WebView2。
- [Flask](https://flask.palletsprojects.com/)：仅监听127.0.0.1的本地服务，不作为外网服务器。
- [SQLite](https://www.sqlite.org/docs.html)：Python内置sqlite3，WAL任务和素材记录。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：本地时间码识别；需用户准备兼容CTranslate2模型。模型不随包分发。
- 本地转写兼容性：faster-whisper 1.2.1 搭配 PyAV 18.1.0；约束 `av<19`，避免 PyAV 19 移除 `metadata_errors` 参数导致真实解码失败。依据：[上游问题 #1589](https://github.com/SYSTRAN/faster-whisper/issues/1589)。回归测试实际解码本地 WAV，不只检查依赖能否导入。
- [DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/)：Chat Completions，JSON结构改写；模型名可在设置更换。
- [Fish Audio TTS API](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)：/v1/tts，reference_id，s2.1-pro，WAV。
- [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html)：xfade/subtitles/boxblur/delogo/amix/sidechaincompress。
- 素材准备：点击“下一步”后在本地后台处理所选素材；普通 SDR 直通，PQ/HLG HDR 使用 FFmpeg `zscale + tonemap`（libzimg）及 `libx264` 生成 BT.709/yuv420p 的 MP4 缓存。需要构建包含 zscale、tonemap、sidedata、libx264；不新增 Python 包，不依赖 Vulkan，也不调用收费接口。原视频身份与原时间范围保持不变，识别、预览和渲染共用准备结果；缓存存放于运行目录的 `sdr-cache`。杜比视界 Profile 5 不按普通 PQ 硬转，会在收费流程前提示专用处理要求。
- 抖音下载：Python 标准库 HTTPS + FFmpeg/ffprobe。移动端 Feed 请求形态参考 [ucmao/media-parser 的抖音适配器](https://github.com/ucmao/media-parser/blob/36e57ff489d23e1037050c9de7f0a39598f8caee/src/parsers/douyin_parser.py)，本项目独立实现精确作品匹配、逐跳验证、限流取消和完整性检查，未引入该项目的 Cookie/签名分支或关闭 TLS 校验。该平台接口不是稳定开放 API，变化后需复测。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 暂留在原有依赖锁定文件中以保持现有安装环境；本轮抖音下载链路不再调用。

本版源码独立实现；此前分析包用于理解媒体参数和适配，不直接捆绑原程序的EXE、ASAR或还原模块。
未来发布安装包时须按实际FFmpeg构建选项及各依赖的许可证保留对应声明和履行分发条件。



## Whole-voice desktop production
The existing faster-whisper local model folder is now required for word-onset timing of the one complete utterance. No automatic model download occurs. Install the pinned requirements with the project Python. `rapidocr-onnxruntime==1.4.4` includes the local Chinese/English OCR ONNX weights; OpenCV is pinned to 4.11.0.86 in the lock file. It locates likely lower-frame subtitle bands on only selected windows, without paid OCR calls. Sparse OCR and delogo do not guarantee perfect removal; preview moving captions/complex backgrounds. Raw audio, alignment and subtitle-location checkpoints live under the user runtime, never inside the source package.
