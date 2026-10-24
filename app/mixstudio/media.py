"""Local, source-bound FFmpeg rendering. No cloud calls or model downloads.

Source files are read-only. A render is published only after probing the staged
result. Blur/delogo are conventional filters, not AI inpainting.
"""

from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time


class MediaError(RuntimeError):
    """A user-actionable validation or media processing failure."""


class MediaCancelled(MediaError):
    """The caller cancelled a local media operation."""


@contextmanager
def _render_workspace(parent, warnings):
    """Only post-publication temporary cleanup may degrade to a warning."""
    workspace = {'published': False}
    try:
        with tempfile.TemporaryDirectory(prefix=".mixstudio-", dir=parent) as name:
            workspace['path'] = Path(name)
            yield workspace
    except OSError:
        if not workspace['published']:
            raise
        warnings.append('成片已发布；部分临时文件清理失败，请稍后清理临时目录。')


def _number(value, name, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"{name}需要有效数字") from exc
    if isinstance(value, bool) or not math.isfinite(result) or not low <= result <= high:
        raise MediaError(f"{name}需在 {low}～{high} 范围内")
    return result


def _path(value, *, exists=True):
    raw = os.fspath(value)
    if any(c in raw for c in "\x00\r\n") or "://" in raw:
        raise MediaError("仅接受本地文件路径")
    path = Path(raw).expanduser().resolve()
    if exists and not path.is_file():
        raise MediaError(f"找不到文件：{path.name}")
    return path


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise MediaCancelled("任务已取消")


def _terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _run(args, *, cancel=None, progress=None, duration=1, cwd=None, timeout=3600):
    """No shell; bounded FFmpeg output; cancellation also terminates children."""
    _cancelled(cancel)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {
        "start_new_session": True
    }
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(
                [os.fspath(a) for a in args], stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, cwd=cwd, **options,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"缺少处理工具：{Path(args[0]).name}") from exc
        started = time.monotonic()
        try:
            while process.poll() is None:
                _cancelled(cancel)
                if time.monotonic() - started > timeout:
                    raise MediaError("媒体处理超时，请缩小任务后重试")
                if progress:
                    out.seek(0)
                    text = out.read().decode("utf-8", "replace")
                    values = [line.split("=", 1)[1] for line in text.splitlines()
                              if line.startswith("out_time_us=")]
                    if values:
                        try:
                            progress(min(.99, max(0, float(values[-1]) / 1e6 / duration)))
                        except ValueError:
                            pass
                time.sleep(.1)
            _cancelled(cancel)
        except BaseException:
            _terminate(process)
            raise
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")
        if process.returncode:
            raise MediaError(f"媒体处理失败（{process.returncode}）：{stderr[-2400:].strip()}")
        if progress:
            progress(1.0)
        return stdout


def _finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _stream_span(source, stream, selector, origin, ffprobe):
    """Absolute presentation span; a container's audio tail is not video.

    MP4 normally supplies a stream duration. Matroska/VFR streams may not;
    in that case demux packet PTS/duration rather than inferring from frame
    count, average FPS, or the unrelated container duration.
    """
    if not stream:
        return None, None, None
    start, duration = _finite(stream.get('start_time')), _finite(stream.get('duration'))
    if duration is not None and duration > 0:
        start = origin if start is None else start
        return start, start + duration, duration
    packets = json.loads(_run([
        ffprobe, '-v', 'error', '-select_streams', selector, '-show_packets',
        '-show_entries', 'packet=pts_time,dts_time,duration_time', '-of', 'json', source,
    ], timeout=60)).get('packets', [])
    bounds = []
    for packet in packets:
        pts = _finite(packet.get('pts_time'))
        if pts is None:
            pts = _finite(packet.get('dts_time'))
        span = _finite(packet.get('duration_time'))
        if pts is not None:
            bounds.append((pts, pts + max(0., span or 0.)))
    if not bounds or max(b for _, b in bounds) <= min(a for a, _ in bounds):
        raise MediaError('媒体流的有效时间范围不完整，请重新导出素材')
    start, end = min(a for a, _ in bounds), max(b for _, b in bounds)
    return start, end, end - start


def probe(path: Path, ffprobe="ffprobe") -> dict:
    source = _path(path)
    data = json.loads(_run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", source,
    ], timeout=30))
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = _number(data.get("format", {}).get("duration") or video.get("duration")
                       or audio.get("duration"), "媒体时长", .001, 86400)
    origin = _finite(data.get('format', {}).get('start_time')) or 0.
    video_start, video_end, video_duration = _stream_span(source, video, 'v:0', origin, ffprobe)
    audio_start, audio_end, audio_duration = _stream_span(source, audio, 'a:0', origin, ffprobe)
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        num, den = (float(x) for x in rate.split("/"))
        fps = num / den if den else 0
    except (ValueError, ZeroDivisionError):
        fps = 0
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    rotation = next((float(s.get("rotation", 0)) for s in video.get("side_data_list", [])
                     if "rotation" in s), 0)
    if round(rotation) % 180:
        width, height = height, width
    try:
        sar_a, sar_b = (float(x) for x in video.get("sample_aspect_ratio", "1:1").split(":"))
        sar = sar_a / sar_b if sar_b else 1
    except (ValueError, ZeroDivisionError):
        sar = 1
    if round(rotation) % 180:
        # Autorotation exchanges pixel axes as well as width/height. Carry the
        # reciprocal SAR into the square-pixel normalization below.
        sar = 1 / (sar or 1)
    return {
        "duration": duration, "width": width, "height": height, "fps": fps,
        "format_start_time": origin,
        "video_duration": video_duration, "video_start_time": video_start, "video_end_time": video_end,
        "audio_duration": audio_duration, "audio_start_time": audio_start, "audio_end_time": audio_end,
        "has_audio": bool(audio), "sample_aspect_ratio": sar or 1,
        "hdr": (video.get("color_transfer") in {"smpte2084", "arib-std-b67"}
                or any("DOVI" in s.get("side_data_type", "").upper() for s in video.get("side_data_list", []))),
        "color_transfer": video.get("color_transfer"), "color_primaries": video.get("color_primaries"),
        "color_space": video.get("color_space"), "pix_fmt": video.get("pix_fmt"),
        "dolby_vision_profile": next((s.get("dv_profile") for s in video.get("side_data_list", [])
                                      if "DOVI" in s.get("side_data_type", "").upper()), None),
    }


def thumbnail(source: Path, dest: Path, ffmpeg="ffmpeg") -> None:
    source, dest = _path(source), _path(dest, exists=False)
    if source == dest or dest.exists():
        raise MediaError("缩略图目标已存在，原文件保持不变")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n",
          "-i", source, "-frames:v", "1", "-vf", "scale=320:-2",
          "-threads", "1", dest], timeout=30)
    if not dest.is_file() or not dest.stat().st_size:
        raise MediaError("缩略图生成结果为空")


def _window(clip, infos, *, cancel=None, identity_cache=None):
    _cancelled(cancel)
    path = _path(clip["source_path"])
    if path not in infos:
        infos[path] = probe(path)
        if infos[path]["hdr"] and clip.get("prepared_media"):
            from .preprocess import validated
            record = clip["prepared_media"]
            prepared, converted, offset = validated(record, clip.get("source_sha256", record.get("source_sha256")),
                                                    cancel=cancel,identity_cache=identity_cache)
            infos[path] = {**infos[path], "hdr": False, "render_path": prepared, "render_offset": offset,
                           "width": converted["width"], "height": converted["height"],
                           "sample_aspect_ratio": converted["sample_aspect_ratio"]}
    info = infos[path]
    if not info["width"] or not info["height"]:
        raise MediaError(f"{path.name}没有视频画面")
    if info["hdr"]:
        raise MediaError(f"{path.name}为 HDR，请先生成 SDR 副本后导入，避免颜色失真")
    # Clip timestamps use normal FFmpeg -ss coordinates relative to the input
    # format origin, not absolute packet PTS. Old injected probe fixtures omit
    # stream fields; real probe always supplies them for video.
    origin = info.get('format_start_time', 0.)
    video_start = max(0., info.get('video_start_time', origin) - origin)
    video_end = info.get('video_end_time', origin + info['duration']) - origin
    start = _number(clip.get("start"), "片段起点", video_start, video_end)
    end = _number(clip.get("end"), "片段终点", video_start, video_end + .02)
    end = min(end, video_end)
    if end - start < .08:
        raise MediaError("源视频片段需至少 0.08 秒")
    return {"source_path": path, "start": start, "end": end}


def _subtract(window, exclusions):
    """Remove previously used/reserved windows rather than repeating frames."""
    pieces = [(window["start"], window["end"])]
    for item in exclusions:
        if item["source_path"] != window["source_path"]:
            continue
        next_pieces = []
        for start, end in pieces:
            if item["start"] > start:
                next_pieces.append((start, min(end, item["start"])))
            if item["end"] < end:
                next_pieces.append((max(start, item["end"]), end))
        pieces = [(a, b) for a, b in next_pieces if b - a >= .08]
    return [{**window, "start": a, "end": b} for a, b in pieces]


def _voice_units(clips, settings, duration, warnings):
    supplied = settings.get("subtitle_segments")
    explicit_units = settings.get("visual_unit_seconds")
    if supplied:
        cues = []
        last = 0.0
        for item in supplied:
            start = _number(item.get("start"), "字幕起点", 0, duration)
            end = _number(item.get("end"), "字幕终点", 0, duration + .04)
            if start < last - .02 or end <= start:
                raise MediaError("字幕时间需递增且互不重叠")
            cues.append({"start": start, "end": min(end, duration),
                         "text": str(item.get("text", ""))})
            last = end
        if explicit_units is not None:
            units = [_number(v, "画面单元时长", .001, 60) for v in explicit_units]
            if len(units) != len(clips) or abs(sum(units)-duration) > .002:
                raise MediaError("画面单元与整条配音时间轴不一致")
            return cues, units
        if len(cues) == len(clips):
            # Include short pauses in the previous visual unit, not in repeated frames.
            boundaries = [0.0] + [c["start"] for c in cues[1:]] + [duration]
            return cues, [b - a for a, b in zip(boundaries, boundaries[1:])]
        warnings.append("字幕数量与文案单元不同；画面按文案字数估算分配，请预览核对。")
    else:
        cues = []
        warnings.append("字幕时间按文案字数估算，未进行逐字强制对齐，请预览校对。")
    weights = [max(1, len(str(c.get("text", "")).strip())) for c in clips]
    units = [duration * weight / sum(weights) for weight in weights]
    if not cues:
        cursor = 0
        for clip, unit in zip(clips, units):
            cues.append({"start": cursor, "end": cursor + unit, "text": str(clip.get("text", ""))})
            cursor += unit
    return cues, units


def _plan(clips, units, transition, infos, *, cancel=None, identity_cache=None):
    primary = [_window(c, infos,cancel=cancel,identity_cache=identity_cache) for c in clips]
    shortest = min(min(w["end"] - w["start"] for w in primary), min(units))
    fade = min(transition, shortest / 4)
    planned, used = [], []
    for index, (clip, first, unit) in enumerate(zip(clips, primary, units)):
        if any(w["source_path"] == first["source_path"]
               and min(w["end"], first["end"]) - max(w["start"], first["start"]) > .001
               for w in used):
            raise MediaError("文案绑定的源片段发生重叠复用，请重新选择片段")
        target = unit + (fade / 2 if index else 0) + (fade / 2 if index < len(clips)-1 else 0)
        candidates = [first]
        for extra in clip.get("additional_clips", []):
            window = _window(extra, infos,cancel=cancel,identity_cache=identity_cache)
            candidates.extend(_subtract(window, primary + used + candidates[1:]))
        chosen, coverage = [], 0.0
        for window in candidates:
            length = window["end"] - window["start"]
            if length <= 2 * fade + .02:
                continue
            chosen.append(dict(window))
            coverage += length - (fade if len(chosen) > 1 else 0)
            if coverage >= target - .001:
                break
        if coverage < target - .001:
            raise MediaError(
                f"第 {index+1} 段配音需要 {target:.2f} 秒相关画面，"
                f"可用源片段仅 {coverage:.2f} 秒；请补充同商品片段或精简该段文案"
            )
        # Trim proportionally; keep every supplied source within its original window.
        needed = target + fade * (len(chosen) - 1)
        minimum = min(2 * fade + .02, needed / len(chosen))
        capacities = [w["end"] - w["start"] for w in chosen]
        lengths = [minimum] * len(chosen)
        remaining = needed - sum(lengths)
        for _ in range(len(chosen) + 1):
            free = [max(0, cap - length) for cap, length in zip(capacities, lengths)]
            if remaining <= .000001 or sum(free) <= .000001:
                break
            additions = [remaining * f / sum(free) for f in free]
            lengths = [a + b for a, b in zip(lengths, additions)]
            remaining = needed - sum(lengths)
        for window, length in zip(chosen, lengths):
            window["end"] = window["start"] + length
            window["unit_index"] = index
            planned.append(window)
            used.append(window)
    if len(planned) > 64:
        raise MediaError("第一版单条视频最多使用 64 个画面片段，请减少片段数")
    return planned, fade


def _validated_timeline(clips, units, requested_fade, infos, plan, *, cancel=None, identity_cache=None):
    """Execute a measured V2 plan, independently checking source/range/timing.

    Hashes are recomputed once per file, so renamed byte-identical imports share
    reuse identity. Internal trims retain original interval identity throughout.
    """
    from .timeline import reuse_stats
    if not isinstance(plan, dict) or plan.get('version') != 1 or plan.get('phase') != 'actual' or plan.get('status') != 'ready':
        raise MediaError('请先完成实测配音时间线')
    speed = _number(plan.get('speed'), '配音倍速', 1, 1.5)
    fade = _number(plan.get('transition_seconds'), '时间线转场', 0, requested_fade + .000001)
    if abs(_number(plan.get('total_seconds'), '时间线总时长', .1, 60)-sum(units)) > .02:
        raise MediaError('时间线总时长与实测音频不同')
    rows, windows = plan.get('segments'), plan.get('windows')
    if not isinstance(rows, list) or len(rows) != len(units) or not isinstance(windows, list) or not 1 <= len(windows) <= 512:
        raise MediaError('时间线段落或片段数量无效')
    hashes, allowed = {}, []
    for clip in clips:
        choices = []
        for candidate in [clip] + list(clip.get('additional_clips', [])):
            checked = _window(candidate, infos,cancel=cancel,identity_cache=identity_cache)
            path = checked['source_path']
            if path not in hashes:
                def signature(source):
                    digest = hashlib.sha256()
                    with source.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024*1024), b''):
                            _cancelled(cancel)
                            digest.update(block)
                    return digest.hexdigest()
                hashes[path] = identity_cache.signature(path,signature) if identity_cache is not None else signature(path)
            if candidate.get('source_sha256') and candidate['source_sha256'] != hashes[path]:
                raise MediaError('源视频已改变，请重新读取并匹配画面')
            a = candidate.get('source_start', checked['start'])
            b = candidate.get('source_end', checked['end'])
            choices.append((path, a, b))
        allowed.append(choices)
    checked_windows, last = [], -1
    for window in windows:
        i = window.get('unit_index')
        if type(i) is not int or i < last or not 0 <= i < len(units):
            raise MediaError('时间线段落顺序无效')
        last = i
        path = _path(window.get('source_path'))
        a = _number(window.get('source_start'), '原片起点', 0, 86400)
        b = _number(window.get('source_end'), '原片终点', a+.001, 86400)
        if not any(p == path and abs(x-a) < .000001 and abs(y-b) < .000001 for p,x,y in allowed[i]):
            raise MediaError('时间线引用了本段未关联的原片范围')
        if window.get('source_sha256') and window['source_sha256'] != hashes[path]:
            raise MediaError('时间线源视频身份不符')
        start = _number(window.get('start'), '剪辑起点', a, b)
        end = _number(window.get('end'), '剪辑终点', start+.001, b+.000001)
        video_speed = _number(window.get('video_speed', 1.), '画面倍速', .85, 1.)
        playback_seconds = (end-start)/video_speed
        if 'playback_seconds' in window and abs(
                _number(window['playback_seconds'], '画面播放时长', .001, 86400)-playback_seconds) > .00001:
            raise MediaError('画面播放时长与原片范围及倍速不一致')
        checked_windows.append({**window, 'source_path':path, 'source_sha256':hashes[path],
                                'start':start, 'end':end, 'video_speed':video_speed,
                                'playback_seconds':playback_seconds})
    stats = reuse_stats(checked_windows)
    if stats['repeat_count'] > 2 or stats['max_uses'] > 2 or plan.get('repeat_count') != stats['repeat_count']:
        raise MediaError('时间线超过原片范围复用额度')
    for i, unit in enumerate(units):
        row = rows[i]
        if abs(_number(row.get('voice_seconds'), '逐句时间线', .001, 300)-unit) > .02 or abs(_number(row.get('raw_seconds'), '原始配音时长', .001, 300)/speed-unit) > .02:
            raise MediaError('逐句时间线与实测配音不一致')
        group = [w for w in checked_windows if w['unit_index'] == i]
        expected = unit + fade*((i>0)+(i<len(units)-1))/2
        coverage = sum(w['playback_seconds'] for w in group)-fade*max(0,len(group)-1)
        if not group or abs(coverage-expected) > .02:
            raise MediaError('逐句画面覆盖与时间线不一致，请重新排片')
    return checked_windows, fade


def _band(height, top_pct, height_pct):
    band_h = max(2, int(height * height_pct / 100) // 2 * 2)
    band_h = min(height - 4, band_h)
    top = max(2, min(height - band_h - 2, int(height * top_pct / 100) // 2 * 2))
    return top, band_h


def _caption_text(text, max_chars):
    # ASS override syntax is not accepted from model/user text.
    text = str(text).replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    text = "".join(c if ord(c) >= 32 else " " for c in text)
    return r"\N".join(text[i:i + max_chars] for i in range(0, len(text), max_chars))


def _ass_time(value):
    hundredths = max(0, round(value * 100))
    return f"{hundredths//360000}:{hundredths//6000%60:02}:{hundredths//100%60:02}.{hundredths%100:02}"


def _caption_layouts(timeline, mapped_bands, mapped_bounds, duration, height,
                     font_size, margin, clean_mode):
    """Place text over each repaired source area, independent of its backdrop.

    Letterboxing maps different source bands to different output coordinates.
    Switch layout at the visual midpoint of each fade; never union those bands
    into one global subtitle position. Text stays within the actual picture.
    """
    default_center = int(height * (1 - margin / 100)) - font_size
    if clean_mode == "none":
        # Preserve the original margin layout, including its clipping behavior
        # for large fonts on short output canvases.
        caption_top = max(0, default_center - int(font_size * 1.35))
        caption_bottom = min(height, default_center + int(font_size * 1.35))
        return [dict(start=0., end=duration, center_y=(caption_top+caption_bottom)//2,
                     font_size=font_size)]
    layouts = []
    for index, item in enumerate(timeline):
        start = 0. if not index else (timeline[index-1]["output_end"] + item["output_start"]) / 2
        end = duration if index == len(timeline)-1 else (item["output_end"] + timeline[index+1]["output_start"]) / 2
        top, bottom = mapped_bounds[index]
        band_top, band_bottom = mapped_bands[index]
        # Preserve configured sizing unless the entire visible source picture
        # is too short for two lines. A narrow repair band need not shrink text.
        effective_font = min(font_size, max(1, int((bottom-top-4) / 2.7)))
        half_height = min((bottom-top) / 2, effective_font * 1.35 + 2)
        low, high = math.ceil(top + half_height), math.floor(bottom - half_height)
        center = (band_top + band_bottom) // 2
        center = max(low, min(high, center)) if low <= high else round((top+bottom)/2)
        if end > start:
            layouts.append(dict(start=start, end=end, center_y=center, font_size=effective_font))
    return layouts


def _portable_font():
    """Only the folder launcher enables a bundled, fixed-name Chinese font."""
    value = os.environ.get('JDMIX_FONT_FILE')
    if not value:
        return None
    font = Path(value)
    if font.name != 'NotoSansCJKsc-Regular.otf' or not font.is_file():
        raise MediaError('随包中文字幕字体缺失，请完整复制软件 fonts 文件夹')
    return font


def _write_captions(path, cues, width, height, font_size, center_y, *, layouts=None):
    max_chars = max(4, int(width * .88 / font_size))
    font_name = 'Noto Sans CJK SC' if _portable_font() else 'Microsoft YaHei'
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00141414,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,20,20,20,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for cue in cues:
        text = str(cue["text"]).strip()
        chunks = [text[i:i + max_chars * 2] for i in range(0, len(text), max_chars * 2)]
        cursor = cue["start"]
        for chunk in chunks:
            end = cursor + (cue["end"] - cue["start"]) * len(chunk) / max(1, len(text))
            # Paginate the original cue exactly once. A shot boundary changes
            # position only, not the text or its reading interval.
            for layout in layouts or [dict(start=cursor, end=end, center_y=center_y,
                                           font_size=font_size)]:
                shown_start, shown_end = max(cursor, layout["start"]), min(end, layout["end"])
                if shown_end <= shown_start or _ass_time(shown_end) == _ass_time(shown_start):
                    continue
                position = f"\\pos({width//2},{layout['center_y']})"
                if layout["font_size"] != font_size:
                    position += f"\\fs{layout['font_size']}"
                lines.append(f"Dialogue: 0,{_ass_time(shown_start)},{_ass_time(shown_end)},Default,,0,0,0,,"
                             f"{{{position}}}{_caption_text(chunk, max_chars)}")
            cursor = end
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _encoder_args(encoder, preset):
    presets = {
        "cpu": ["-c:v", "libx264", "-preset", {"fast": "veryfast", "balanced": "medium",
                                             "quality": "slow"}[preset], "-crf", "21"],
        "nvenc": ["-c:v", "h264_nvenc", "-preset", {"fast": "p2", "balanced": "p4",
                                                  "quality": "p6"}[preset],
                  "-rc", "vbr", "-cq", "23", "-b:v", "0"],
        "qsv": ["-c:v", "h264_qsv", "-preset", {"fast": "veryfast", "balanced": "medium",
                                              "quality": "slow"}[preset],
                "-global_quality", "23"],
    }
    if encoder not in presets:
        raise MediaError("编码器需为 cpu、nvenc 或 qsv")
    return presets[encoder] + ["-pix_fmt", "yuv420p", "-threads", "2"]


def _publish(source, destination):
    """Publish complete bytes only; an existing destination always wins.

    Windows rename is atomic and fails if the target exists. Copy fallback
    therefore completes on the destination volume before touching the final
    filename, and an interrupted copy owns only its uniquely named temp file.
    """
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise MediaError("输出文件已存在，请选择新的文件名") from exc
    except OSError:
        temporary = None
        try:
            with source.open('rb') as incoming, tempfile.NamedTemporaryFile(
                    'wb', dir=destination.parent, prefix='.mixstudio-publish-',
                    suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            if os.name == 'nt':
                # Unlike os.replace (and POSIX rename), this never clobbers.
                os.rename(temporary, destination)
                temporary = None
            else:
                # Never use POSIX rename here: it may replace a racing target.
                os.link(temporary, destination)
        except FileExistsError as exc:
            raise MediaError("输出文件已存在，请选择新的文件名") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _frame_timeline(lengths, fade, fps, duration, max_output=None):
    """Quantize cumulative endpoints, never round every clip independently.

    Shortening the fade to a whole frame cannot require extra source content.
    Use the same ceiling rule for every cumulative endpoint. Mixing nearest
    rounding with a final ceiling can push the last clip beyond one extra frame.
    Each clip needs at most one frame of rounding extension; the normalizer's
    bounded tpad supplies just that frame, not arbitrary shortage filler.
    """
    fade_frames = max(0, math.floor(fade * fps + 1e-8))
    final_frame = math.ceil(duration * fps - 1e-8)
    if max_output is not None:
        final_frame = min(final_frame, math.floor(float(max_output) * fps + 1e-8))
    frames, cumulative, previous_end = [], 0., 0
    for index, length in enumerate(lengths):
        cumulative += length - (fade if index else 0.)
        endpoint = final_frame if index == len(lengths)-1 else math.ceil(cumulative * fps - 1e-8)
        count = endpoint - previous_end + (fade_frames if index else 0)
        if count < 1 or (fade_frames and count <= fade_frames):
            raise MediaError('镜头片段短于输出帧或转场，请调整片段后再生成')
        if count / fps - length > 1 / fps + 1e-7:
            raise MediaError('镜头帧量化超出一帧补齐额度，请重新匹配画面')
        frames.append(count)
        previous_end = endpoint
    return frames, fade_frames / fps


def _validate_stream_coverage(info, duration, fps):
    """An intact audio track must not conceal a truncated/late video track."""
    origin = info.get('format_start_time', 0.)
    tolerance = 1 / fps + .001
    for name, label in (('video', '视频轨'), ('audio', '音轨')):
        start = info.get(name + '_start_time', origin)
        end = info.get(name + '_end_time', origin + info['duration'])
        if start is None or end is None or abs(start-origin) > tolerance or abs(end-origin-duration) > tolerance:
            raise MediaError(f'成片{label}覆盖与配音不符，未发布输出')


def render_video(clips: list[dict], voice_path: Path, output_path: Path, settings: dict,
                 *, cancel=None, progress=None) -> dict:
    """Render measured narration with explicitly bound source windows.

    Each clip may include ``additional_clips`` containing source_path/start/end.
    Only those explicit, nonoverlapping windows extend its voice unit. The caller
    is responsible for same-product/semantic selection. No looping or voice trim.
    """
    _cancelled(cancel)
    if not clips or len(clips) > 64:
        raise MediaError("请提供 1～64 个已绑定源视频的文案单元")
    voice, output = _path(voice_path), _path(output_path, exists=False)
    if output.exists() or output == voice or output.suffix.lower() != ".mp4":
        raise MediaError("请选择尚不存在的 MP4 输出文件，原文件保持不变")
    voice_info = probe(voice)
    if not voice_info["has_audio"]:
        raise MediaError("配音文件没有音轨")
    duration = voice_info["duration"]
    _number(duration, "配音时长", .1, 600)
    supplied_duration = settings.get("duration_seconds")
    if supplied_duration is not None:
        measured = _number(supplied_duration, "提供的配音时长", .1, 600)
        if abs(measured - duration) > .08:
            raise MediaError("提供的配音时长与实际文件不符，请重新测量")
    width = int(_number(settings.get("width", 720), "宽度", 160, 3840))
    height = int(_number(settings.get("height", 1280), "高度", 160, 3840))
    if width % 2 or height % 2:
        raise MediaError("输出画面宽高需为偶数")
    fps = int(_number(settings.get("fps", 30), "帧率", 10, 60))
    transition = settings.get("transition", "fade")
    if transition not in {"fade", "none"}:
        raise MediaError("转场需为 fade 或 none")
    fade_request = _number(settings.get("transition_seconds", .2), "转场时长", 0, 1)
    mode = settings.get("clean_mode", "none")
    if mode not in {"none", "blur", "delogo"}:
        raise MediaError("清理方式需为 none、blur 或 delogo")
    top_pct = _number(settings.get("clean_top_pct", 75), "清理区域顶部", 0, 95)
    height_pct = _number(settings.get("clean_height_pct", 18), "清理区域高度", 3, 45)
    font_size = int(_number(settings.get("subtitle_font_size", 42), "字幕字号", 12, 120))
    margin = _number(settings.get("subtitle_margin_pct", 12), "字幕底部边距", 2, 40)
    encoder, preset = settings.get("encoder", "cpu"), settings.get("preset", "balanced")
    if preset not in {"fast", "balanced", "quality"}:
        raise MediaError("编码速度需为 fast、balanced 或 quality")
    _encoder_args(encoder, preset)
    bgm = _path(settings["bgm_path"]) if settings.get("bgm_path") else None
    gain = _number(settings.get("bgm_gain", .15), "背景音乐音量", 0, 1)
    if bgm and not probe(bgm)["has_audio"]:
        raise MediaError("背景音乐文件没有音轨")
    warnings, infos = [], {}
    from .media_identity import MediaIdentityCache
    identity_cache=MediaIdentityCache()
    cues, units = _voice_units(clips, settings, duration, warnings)
    max_caption_chars = max(4, int(width * .88 / font_size)) * 2
    if any(len(cue["text"].strip()) > max_caption_chars for cue in cues):
        warnings.append("长句字幕已分屏显示，句内分页时间按字数估算，请预览校对。")
    effective_fade = fade_request if transition == 'fade' else 0
    if settings.get('timeline_plan') is not None:
        planned, fade = _validated_timeline(clips, units, effective_fade, infos, settings['timeline_plan'],cancel=cancel,identity_cache=identity_cache)
    else:
        planned, fade = _plan(clips, units, effective_fade, infos,cancel=cancel,identity_cache=identity_cache)
    frame_counts, fade = _frame_timeline(
        [(w['end']-w['start'])/w.get('video_speed', 1.) for w in planned], fade, fps, duration,
        settings.get('max_output_seconds'))
    picture_duration = (sum(frame_counts) - round(fade*fps)*(len(frame_counts)-1)) / fps
    if output in infos or output == bgm:
        raise MediaError("输出目标与输入文件相同")
    if fade < fade_request - .001 and transition == "fade":
        warnings.append("短片段已自动缩短转场，防止转场覆盖完整镜头。")
    if mode != "none":
        warnings.append("当前画面清理使用局部模糊/插值滤镜，并非 AI 修复；请检查商品细节。")
    report = progress or (lambda value, message: None)
    output.parent.mkdir(parents=True, exist_ok=True)
    base = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n"]
    timeline, mapped_bands, mapped_bounds = [], [], []
    with _render_workspace(output.parent, warnings) as workspace:
        temp = workspace['path']
        normalized = []
        for index, window in enumerate(planned):
            _cancelled(cancel)
            info = infos[window["source_path"]]
            normalized_path = temp / f"clip{index:03}.mp4"
            length = window["end"] - window["start"]
            video_speed = window.get('video_speed', 1.)
            playback_length = length/video_speed
            source_w = max(2, int(info["width"] * info["sample_aspect_ratio"]) // 2 * 2)
            source_h = info["height"]
            local_mode = mode
            band_y, band_h = _band(source_h, top_pct, height_pct)
            band_x, band_w = (2,source_w-4) if mode=='delogo' else (0,source_w)
            if 'cleanup_band' in window:
                band = window['cleanup_band']
                if band is None:
                    local_mode = 'none'
                else:
                    top = _number(band.get('top_pct'), '旧字幕顶部', 0, 99)
                    height_band = _number(band.get('height_pct'), '旧字幕高度', .1, 38)
                    if top+height_band > 99.9:
                        raise MediaError('旧字幕区域超出源画面')
                    band_y = max(1, int(source_h*top/100))
                    band_h = max(2, min(source_h-band_y-1, math.ceil(source_h*height_band/100)))
                    if 'left_pct' in band or 'width_pct' in band:
                        left=_number(band.get('left_pct'),'旧字幕左侧',0,99.9)
                        width_band=_number(band.get('width_pct'),'旧字幕宽度',.1,100)
                        if left+width_band>100:raise MediaError('旧字幕区域超出源画面')
                        band_x=max(1,int(source_w*left/100))
                        band_w=max(2,min(source_w-band_x-1,math.ceil(source_w*width_band/100)))
            # Trim on the original source clock before changing timestamps.
            # Slowing a picture never authorizes frames beyond its bound range.
            chain = [f"trim=duration={length:.6f}", f"setpts=(PTS-STARTPTS)/{video_speed:.12g}",
                     f"scale={source_w}:{source_h}", "setsar=1"]
            if local_mode == "delogo":
                chain.append(f"delogo=x={band_x}:y={band_y}:w={band_w}:h={band_h}")
            cleanup = ",".join(chain)
            if local_mode == "blur":
                # yuv420p chroma planes are half-sized. Use a bounded radius
                # separately for each plane, including minimum-height bands.
                radius = min(8, max(0, min(band_w, band_h)//2-1))
                chroma_radius = min(radius, max(0, min(band_w//2, band_h//2)//2-1))
                graph = (f"[0:v]{cleanup},split=2[original][crop];"
                         f"[crop]crop={band_w}:{band_h}:{band_x}:{band_y},"
                          f"boxblur=luma_radius={radius}:luma_power=2:chroma_radius={chroma_radius}[blur];"
                         f"[original][blur]overlay={band_x}:{band_y}[clean];[clean]")
            else:
                graph = f"[0:v]{cleanup},"
            graph += (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                      f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                      f"setsar=1,fps={fps},format=yuv420p,"
                       f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={1/fps:.9f},"
                       f"trim=end_frame={frame_counts[index]},setpts=N/({fps}*TB)[v]")
            input_start = window["start"] - info.get("render_offset", 0.)
            _run(base + ["-ss", f"{input_start:.6f}", "-i", info.get("render_path", window["source_path"]),
                         "-filter_complex_threads", "1", "-filter_complex", graph,
                          "-map", "[v]", "-an", "-frames:v", str(frame_counts[index]), "-c:v", "libx264",
                         "-preset", "ultrafast", "-crf", "18", "-threads", "2",
                         "-progress", "pipe:1", normalized_path],
                 cancel=cancel, duration=playback_length,
                 progress=lambda p, i=index: report(.05 + .5 * (i + p) / len(planned),
                                                    f"准备画面 {i+1}/{len(planned)}"))
            normalized_info = probe(normalized_path)
            if abs(normalized_info['video_duration'] - frame_counts[index]/fps) > .001:
                raise MediaError('源片实际画面不足以覆盖计划镜头，未扩展冻结画面')
            normalized.append(normalized_path)
            scale = min(width / source_w, height / source_h)
            pad_y = (height - source_h * scale) / 2
            mapped_bands.append((int(pad_y + band_y * scale),
                                 int(pad_y + (band_y + band_h) * scale)))
            mapped_bounds.append((math.ceil(pad_y), math.floor(pad_y + source_h * scale)))
            timeline.append({**window, "source_path": str(window["source_path"]),
                              "source_duration": length, "video_speed":video_speed,
                              "playback_duration":playback_length, "frame_count": frame_counts[index],
                              "duration": frame_counts[index]/fps})
        output_cursor = 0.0
        for index, item in enumerate(timeline):
            item["output_start"] = output_cursor
            item["output_end"] = output_cursor + item["duration"]
            output_cursor = item["output_end"] - (fade if index < len(timeline) - 1 else 0)
        if settings.get("caption_plate", False) and mode != "none":
            plate_top = max(0, min(b[0] for b in mapped_bands) - 4)
            plate_end = min(height, max(b[1] for b in mapped_bands) + 4)
            if plate_end - plate_top > height * .25:
                warnings.append("不同源画幅的修复区域映射后较宽，字幕底板覆盖超过画面四分之一，请预览调整区域。")
        else:
            center = int(height * (1 - margin / 100)) - font_size
            plate_top = max(0, center - int(font_size * 1.35))
            plate_end = min(height, center + int(font_size * 1.35))
        center_y = (plate_top + plate_end) // 2
        caption_layouts = _caption_layouts(timeline, mapped_bands, mapped_bounds,
            duration, height, font_size, margin, mode)
        _write_captions(temp / "captions.ass", cues, width, height, font_size, center_y,
                        layouts=caption_layouts)
        bundled_font = _portable_font()
        if bundled_font:
            (temp / 'fonts').mkdir()
            shutil.copyfile(bundled_font, temp / 'fonts' / bundled_font.name)
        inputs = []
        filters = []
        for index, path in enumerate(normalized):
            inputs.extend(["-i", path])
            filters.append(f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]")
        current, cumulative = "v0", timeline[0]["duration"]
        for index in range(1, len(normalized)):
            label = f"joined{index}"
            if fade:
                filters.append(f"[{current}][v{index}]xfade=transition=fade:duration={fade:.6f}:"
                               f"offset={cumulative-fade:.6f}[{label}]")
            else:
                filters.append(f"[{current}][v{index}]concat=n=2:v=1:a=0[{label}]")
            cumulative += timeline[index]["duration"] - fade
            current = label
        # Plate and new subtitles come AFTER the entire picture transition graph.
        decorations = f"[{current}]"
        if settings.get("caption_plate", False):
            decorations += (f"drawbox=x=0:y={plate_top}:w=iw:h={plate_end-plate_top}:"
                            "color=0x15171b@0.94:t=fill,")
        ass_filter = 'ass=filename=captions.ass' + (':fontsdir=fonts' if bundled_font else '')
        decorations += f"{ass_filter},trim=duration={duration:.6f},format=yuv420p[vout]"
        filters.append(decorations)
        voice_index = len(normalized)
        inputs.extend(["-i", voice])
        filters.append(f"[{voice_index}:a]aresample=48000,"
                       f"aformat=channel_layouts=stereo,apad,atrim=duration={duration:.6f}[voice]")
        if bgm:
            inputs.extend(["-stream_loop", "-1", "-i", bgm])
            audio_fade = min(.3, duration / 4)
            filters.extend([
                f"[{voice_index+1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume={gain},atrim=duration={duration:.6f},"
                f"afade=t=in:d={audio_fade},afade=t=out:st={duration-audio_fade:.6f}:"
                f"d={audio_fade}[music]",
                "[voice]asplit=2[speech][side]",
                "[music][side]sidechaincompress=threshold=0.03:ratio=6:"
                "attack=15:release=250[duck]",
                "[speech][duck]amix=inputs=2:duration=first:dropout_transition=0:"
                "normalize=0,alimiter=limit=0.95:latency=1[aout]",
            ])
        else:
            filters.append("[voice]alimiter=limit=0.95:latency=1[aout]")
        staged = temp / "result.mp4"
        for candidate in ([encoder, "cpu"] if encoder != "cpu" else ["cpu"]):
            _cancelled(cancel)
            try:
                args = base + inputs + ["-filter_complex_threads", "1", "-filter_complex",
                    ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
                args += _encoder_args(candidate, preset)
                args += ["-c:a", "aac", "-b:a", "192k", "-r", str(fps),
                          "-t", f"{max(duration,picture_duration):.6f}", "-movflags", "+faststart",
                         "-progress", "pipe:1", staged]
                _run(args, cancel=cancel, duration=duration, cwd=temp,
                     progress=lambda p: report(.55 + .4 * p, "合成转场、字幕和配音"))
                break
            except MediaCancelled:
                raise
            except MediaError:
                if candidate == "cpu":
                    raise
                # Staged file belongs to this invocation, never to the user.
                staged.unlink(missing_ok=True)
                warnings.append(f"{candidate} 编码未成功，已重新使用 CPU 编码。")
        _cancelled(cancel)
        result = probe(staged)
        _validate_stream_coverage(result, duration, fps)
        if abs(result["duration"] - duration) > max(.08, 2 / fps):
            raise MediaError("成片时长与配音不符，未发布输出")
        if result["width"] != width or result["height"] != height or not result["has_audio"]:
            raise MediaError("成片画幅或音轨验证失败，未发布输出")
        if settings.get('max_output_seconds') is not None and result['duration'] > settings['max_output_seconds'] + .001:
            raise MediaError('成片超过目标时长允许上限（且最高60秒），请缩短文案；未发布超长输出')
        if settings.get('min_output_seconds') is not None and result['duration'] < settings['min_output_seconds'] - .001:
            raise MediaError('成片短于目标时长允许下限，请调整文案；未发布输出')
        _run(["ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-i", staged,
              "-f", "null", "-"], cancel=cancel, timeout=600)
        report(1.0, "媒体检查通过，正在发布成片")
        _cancelled(cancel)
        _publish(staged, output)
        workspace['published'] = True
    # Publication is the commit point. A progress callback may raise Cancelled;
    # no cancellable callback runs after the durable output has been committed.
    return {"path": str(output), "duration": result["duration"], "width": width,
            "height": height, "warnings": warnings, "timeline": timeline,
            "transition_seconds": fade, "encoder_used": candidate, "published": True}
