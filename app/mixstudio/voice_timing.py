"""Local ASR-assisted timing of known copy. Never synthesizes or edits speech."""
from difflib import SequenceMatcher
import math
import unicodedata


MIN_VISUAL_SECONDS = .05
MIN_COVERAGE = .55
LOCAL_FALLBACK_COVERAGE = .75


def units(text):
    # Case, compatibility forms and spacing are safe equivalences. Do not
    # pretend that arbitrary Chinese numerals / English phonetics are identical.
    return ''.join(c for c in unicodedata.normalize('NFKC', text).lower() if c.isalnum())


def _groups(segments, bounds):
    """Merge display/visual units, never shift a measured sentence boundary."""
    groups, pending = [], []
    for index in range(len(segments)):
        pending.append(index)
        if bounds[index+1] - bounds[pending[0]] >= MIN_VISUAL_SECONDS:
            groups.append(pending)
            pending = []
    if pending:
        if groups:
            groups[-1].extend(pending)
        else:
            groups.append(pending)
    cues, sentence_groups, warnings = [], [], []
    for group in groups:
        members = [segments[i] for i in group]
        ids = [part['segment_id'] for part in members]
        cue = {'segment_id': ids[0], 'text': ''.join(part['text'] for part in members),
               'start': bounds[group[0]], 'end': bounds[group[-1]+1]}
        if len(group) > 1:
            cue['sentence_ids'] = ids
            warnings.append('第'+'、'.join(str(i+1) for i in group)+
                            '句包含极短或重叠边界，字幕与画面合并显示；原口播和音频不变')
        cues.append(cue)
        sentence_groups.append(ids)
    return cues, sentence_groups, warnings


def align_segments(segments, words, duration):
    """Use measured word onsets for sentence cuts, not character-rate estimates.

    Recognition/word timing is approximate, not phoneme forced alignment. Text
    always comes from confirmed copy. A high-coverage local gap can be estimated
    between recognized word edges; every such boundary carries a warning.
    Sub-frame sentences are grouped for captions/visual planning, not stretched.
    """
    if not isinstance(words, list) or not words or len(words) > 20000:
        raise ValueError('本地对齐没有取得有效语音时间；整条配音已保留')
    if type(duration) not in (int, float) or not math.isfinite(duration) or not .1 <= duration <= 60:
        raise ValueError('整条配音须在0.1～60秒内；已有音频保留')
    if (not isinstance(segments, list) or not 1 <= len(segments) <= 64
            or any(not isinstance(s, dict) or not isinstance(s.get('text'), str)
                   or not isinstance(s.get('segment_id'), str) for s in segments)):
        raise ValueError('本地对齐文案段落格式无效；整条音频已保留')
    reference = [units(s['text']) for s in segments]
    if not reference or not all(reference):
        raise ValueError('文案包含没有读音内容的段落，请合并或删除后保存')
    heard, times, last = '', [], -1.
    for word in words:
        if not isinstance(word, dict):
            raise ValueError('本地语音时间码异常；音频已保留，请重试本地对齐')
        a, b = word.get('start'), word.get('end')
        text = word.get('word')
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or not isinstance(text, str)
                or not 0 <= a <= b <= duration+.04 or a < last-.02):
            raise ValueError('本地语音时间码异常；音频已保留，请重试本地对齐')
        last = a
        normalized = units(text)
        heard += normalized
        # ASR may put several characters, or even two sentences, in one word.
        # Only that word's start is directly observed. Its internal locations
        # use the measured word interval, explicitly labelled as estimates.
        size = len(normalized)
        times.extend((min(a+(b-a)*i/size, duration),
                      min(a+(b-a)*(i+1)/size, duration), i == 0)
                     for i in range(size))
    text = ''.join(reference)
    anchors = {}
    for block in SequenceMatcher(None, text, heard, autojunk=False).get_matching_blocks():
        for i in range(block.size):
            anchors[block.a+i] = times[block.b+i]
    coverage = len(anchors)/max(1, len(text))
    if coverage < MIN_COVERAGE:
        raise ValueError('本地识别与确认文案对应不足；整条音频已缓存，尚未调用画面模型，请检查音频或重试本地对齐')
    starts, cursor, warnings, estimated = [0.], 0, [], []
    positions = sorted(anchors)
    for index, part in enumerate(reference):
        if index:
            hits = [p for p in range(cursor, cursor+len(part)) if p in anchors]
            if not hits and coverage < LOCAL_FALLBACK_COVERAGE:
                raise ValueError(f'第{index+1}句没有取得可靠语音定位；音频已保留，尚未调用画面模型')
            method = None
            if cursor in anchors:
                onset, _, measured = anchors[cursor]
                if not measured:
                    method = 'word_interval'
                    warnings.append(f'第{index+1}句起点位于同一识别词内，按该词实测区间估算，请预览校对')
            elif coverage >= LOCAL_FALLBACK_COVERAGE:
                left = next((p for p in reversed(positions) if p < cursor), None)
                right = next((p for p in positions if p > cursor), None)
                low = anchors[left][1] if left is not None else 0.
                high = anchors[right][0] if right is not None else duration
                # Overlapping ASR words cannot define a negative interpolation
                # interval. Preserve their onsets and group a zero-width result.
                low = min(low, high)
                begin = left+1 if left is not None else 0
                finish = right if right is not None else len(text)
                onset = low+(high-low)*(cursor-begin)/max(1, finish-begin)
                method = 'neighboring_anchors'
                edge = '相邻识别词与实测音频边界' if left is None or right is None else '相邻识别词边界'
                warnings.append(f'第{index+1}句起点未完整识别，按{edge}局部估算，请预览校对')
            else:
                onset = anchors[hits[0]][0]
                method = 'first_recognized_word'
                warnings.append(f'第{index+1}句起点采用首个可识别词的位置估算，请预览校对')
            bounded = max(starts[-1], min(duration, onset))
            if abs(bounded-onset) > 1e-9:
                warnings.append(f'第{index+1}句识别时间存在重叠，按相邻边界合并显示，请预览校对')
                method = 'overlapping_word_boundary'
            onset = bounded
            starts.append(onset)
            if method:
                estimated.append({'segment_id': segments[index]['segment_id'], 'method': method})
        cursor += len(part)
    bounds = starts+[duration]
    cues, sentence_groups, group_warnings = _groups(segments, bounds)
    return {'method': 'local_asr_word_onsets_v2', 'coverage': coverage,
            'captions': cues, 'segment_seconds': [b-a for a, b in zip(bounds, bounds[1:])],
            'sentence_groups': sentence_groups, 'estimated_boundaries': estimated,
            'warnings': warnings+group_warnings+
                        ['句内字幕分页仍按字数分配；本地ASR时间需预览校对，非逐字强制对齐']}
