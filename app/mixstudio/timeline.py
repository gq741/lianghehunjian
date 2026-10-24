"""One local estimated/measured timeline planner; no network or file writes.

Candidate ordering is semantic evidence, not a claim that nearby material fits.
A bounded multi-order allocation searches contention alternatives. It is not a
mathematical optimality proof; failures describe this search, never all footage.
"""
from collections import defaultdict
import math
import os

EPS = .001
MAX_WINDOWS = 512
MIN_VIDEO_SPEED = .85
MAX_VOICE_SPEED = 1.5


def _number(value, name, low=0, high=86400):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}无效')
    return float(value)


def _identity(window):
    source = window.get('source_sha256') or os.path.normcase(os.path.abspath(window.get('source_path', '')))
    a = _number(window.get('source_start', window.get('start')), '原片起点')
    b = _number(window.get('source_end', window.get('end')), '原片终点')
    if not source or b <= a:
        raise ValueError('原片窗口身份无效')
    return source, a, b


def reuse_stats(windows):
    """Count original-range reuse, including aliases and overlapping crops.

    Extra appearances = N minus the maximum disjoint interval set, per source.
    Peak overlap counts a third use even when every clip ID/crop differs.
    """
    grouped = defaultdict(list)
    for window in windows:
        source, a, b = _identity(window)
        grouped[source].append((a, b))
    repeats, peak = 0, 0
    for ranges in grouped.values():
        last, independent = -1.0, 0
        for a, b in sorted(ranges, key=lambda w: (w[1], w[0])):
            if a >= last - EPS:
                independent += 1
                last = b
        repeats += len(ranges) - independent
        # Shrinking only the right edge by EPS makes touching ranges distinct.
        events = [(a, 1) for a, b in ranges] + [(b-EPS, -1) for a, b in ranges]
        active = 0
        for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
            active += delta
            peak = max(peak, active)
    return {'repeat_count': repeats, 'max_uses': peak}


def _timing_reason(raw, low, high, phase, issue):
    label = '原始配音时长' if phase == 'actual' else '口播预估时长'
    if issue == 'too_short':
        return f'{label}{raw:.2f}秒，低于当前参考时长允许的最低{low:g}秒；加速不会延长音频'
    return (f'{label}{raw:.2f}秒，即使按最高1.5倍加速仍为{raw/1.5:.2f}秒，'
            f'超过当前允许的最高{high:g}秒')


def shortage_message(plan):
    detail = '；'.join(f"第{i+1}段缺{s['missing_seconds']:.2f}秒" for i, s in
                      enumerate(plan['segments']) if s['missing_seconds'] > EPS)
    constraint = plan.get('timing_constraint', {})
    if constraint.get('status') in {'too_short', 'too_long'}:
        reason = _timing_reason(plan['total_raw_seconds'], constraint['min_seconds'],
                                constraint['max_seconds'], plan['phase'], constraint['status'])
        message = reason + '。' + ('已有音频保留；' if plan['phase'] == 'actual' else '')
        message += '请检查有效音频或缩短文案；参考秒数本身不作为时长门槛。'
        if detail:
            message += '另有画面覆盖缺口：' + detail + '；请补充相关素材或重新自动匹配。'
        return message
    timing = '；'.join(w for w in plan.get('warnings',[]) if '配音时长' in w)
    return ('自动排片暂未找到满足全部约束的方案' + ('：'+detail if detail else '') +
            ('；'+timing+'，请调整文案' if timing else '') +
            '。已尝试相关镜头裁切、转场调整、适度画面慢放及允许复用；当前真实画面仍不足，请补充相关素材或调整镜头选择。')


def plan_timeline(segments, scenes, candidates, *, target_seconds, transition_seconds,
                  voice_seconds=None, phase='estimated', max_voice_speed=MAX_VOICE_SPEED):
    """Return one contract for preview, manual saves, preflight and real voice.

    candidates = [{segment_id, clip_ids:[semantically ranked IDs], ...}].
    No scene absent from a segment's explicit candidate list can enter it.
    """
    from .script_engine import estimate_seconds
    target = _number(target_seconds, '参考时长', 5, 60)
    transition = _number(transition_seconds, '转场时长', 0, .8)
    if phase not in {'estimated', 'actual'} or not isinstance(segments, list) or not 1 <= len(segments) <= 64:
        raise ValueError('时间线段落或阶段无效')
    if not isinstance(scenes, list) or len(scenes) > 4500 or not isinstance(candidates, list):
        raise ValueError('时间线候选数量无效')
    sids = [s['segment_id'] for s in segments]
    if len(set(sids)) != len(sids):
        raise ValueError('时间线段落重复')
    if voice_seconds is None:
        raw = [max(.05, estimate_seconds(s['text'])) for s in segments]
    else:
        if not isinstance(voice_seconds, list) or len(voice_seconds) != len(segments):
            raise ValueError('逐句配音时长不完整')
        raw = [_number(v, '逐句配音时长', .05, 300) for v in voice_seconds]
    index = {}
    for scene in scenes:
        cid = scene.get('clip_id')
        if not isinstance(cid, str) or cid in index:
            raise ValueError('时间线候选ID无效或重复')
        if scene.get('usable') is not True:
            continue
        a, b = _number(scene.get('start'), '镜头起点'), _number(scene.get('end'), '镜头终点')
        if b <= a or not isinstance(scene.get('source_path'), str) or not scene['source_path']:
            raise ValueError('时间线候选窗口无效')
        window = {**scene, 'source_start': a, 'source_end': b}
        _identity(window)
        index[cid] = window
    pools = {sid: [] for sid in sids}
    seen = set()
    for item in candidates:
        sid, ids = item.get('segment_id'), item.get('clip_ids')
        if sid not in pools or sid in seen or not isinstance(ids, list) or len(ids) > 4500:
            raise ValueError('语义候选段落无效或重复')
        seen.add(sid)
        if len(set(ids)) != len(ids) or any(cid not in index for cid in ids):
            raise ValueError('语义候选引用无效、重复或已排除的镜头')
        pools[sid] = [index[cid] for cid in ids]
    total = sum(raw)
    # Reference seconds guide writing/pacing, not a pass/fail duration window.
    # Keep the product's 60-second delivery ceiling and natural voice first.
    low, high = .1, 60.
    minimum_speed = max(1., total/high)
    maximum_speed = _number(max_voice_speed, '配音最高倍速', 1, MAX_VOICE_SPEED)
    shortest = min((c['end']-c['start'] for p in pools.values() for c in p), default=1.)
    n = len(sids)
    # Stable fade while searching speeds makes capacity feasibility monotone.
    fade = min(transition, min(raw)/max(1., min(1.5, maximum_speed))/4, shortest/4)
    rarity = defaultdict(int)
    for pool in pools.values():
        for c in pool:
            rarity[_identity(c)] += 1
    base = list(range(n))
    orders = [sorted(base, key=lambda i: (len(pools[sids[i]]), -raw[i])),
              sorted(base, key=lambda i: (sum(c['end']-c['start'] for c in pools[sids[i]])/raw[i], len(pools[sids[i]]))),
              base, list(reversed(base)), sorted(base, key=lambda i: -raw[i])]
    orders = list(dict.fromkeys(tuple(o) for o in orders))

    def attempt(speed, repeat_budget, video_speed=1.):
        required = [r/speed + fade*((i>0)+(i<n-1))/2 for i,r in enumerate(raw)]
        best = None
        for order in orders:
            for strategy in (0, 1):
                chosen = [[] for _ in base]
                selected = []
                def coverage(i):
                    return sum((c['end']-c['start'])/video_speed for c in chosen[i])-fade*max(0,len(chosen[i])-1)
                # Globally exhaust non-overlapping choices before any reuse.
                for budget in (0, repeat_budget):
                    for i in order:
                        if coverage(i)+1e-8 >= required[i]:
                            continue
                        pool = pools[sids[i]]
                        if strategy:
                            pool = sorted(enumerate(pool), key=lambda it: (rarity[_identity(it[1])], it[0]))
                            pool = [c for _,c in pool]
                        # A window may appear twice, never an unbounded loop.
                        for _ in range(2 if budget else 1):
                            for c in pool:
                                if coverage(i)+1e-8 >= required[i] or len(selected) >= MAX_WINDOWS:
                                    break
                                stats = reuse_stats(selected+[c])
                                if stats['max_uses'] > (2 if budget else 1) or stats['repeat_count'] > budget:
                                    continue
                                chosen[i].append(c)
                                selected.append(c)
                deficits = [max(0., required[i]-coverage(i)) for i in base]
                score = (sum(d>1e-7 for d in deficits), sum(deficits), reuse_stats(selected)['repeat_count'], len(selected))
                if best is None or score < best[0]:
                    best = score, chosen, required
                if score[0] == 0 and score[2] == 0:
                    return best
        return best

    speed = min(maximum_speed, minimum_speed)
    video_speed = 1.
    feasible_range = minimum_speed <= maximum_speed + 1e-9 and total >= low
    result = attempt(speed, 0)
    if result[0][0] != 0:
        result = attempt(speed, 2)
    # Cosmetic dissolve overlap must not force new material or faster speech.
    # Search the same semantic pool without overlap before slowing pictures.
    if feasible_range and result[0][0] and fade > 0:
        fade = 0.
        result = attempt(speed, 0)
        if result[0][0]:
            result = attempt(speed, 2)
    # Only small picture-rate changes; source intervals/reuse identity do not
    # change. Find the least slowdown that covers the existing narration.
    if feasible_range and result[0][0]:
        slow_result = attempt(speed, 2, MIN_VIDEO_SPEED)
        if slow_result[0][0] == 0:
            left, right = MIN_VIDEO_SPEED, 1.
            for _ in range(20):
                mid = (left+right)/2
                found = attempt(speed, 2, mid)
                if found[0][0] == 0:
                    left, slow_result = mid, found
                else:
                    right = mid
            video_speed = max(MIN_VIDEO_SPEED, math.floor(left*10000000)/10000000)
            result = attempt(speed, 2, video_speed)
        else:
            video_speed, result = MIN_VIDEO_SPEED, slow_result
    if feasible_range and result[0][0] and maximum_speed > speed:
        high_result = attempt(maximum_speed, 2, video_speed)
        if high_result[0][0] == 0:
            left, right = speed, maximum_speed
            result = high_result
            for _ in range(24):
                mid = (left+right)/2
                found = attempt(mid, 2, video_speed)
                if found[0][0] == 0:
                    right, result = mid, found
                else:
                    left = mid
            # Round upwards so serializer precision never leaves a short frame.
            speed = math.ceil(right*10000000)/10000000
            result = attempt(speed, 2, video_speed)
        else:
            speed, result = maximum_speed, high_result
    chosen, required = result[1:]
    rows, windows, shortages = [], [], []
    used = []
    for i, sid in enumerate(sids):
        clips = chosen[i]
        original_lengths = [c['end']-c['start'] for c in clips]
        needed = required[i]+fade*max(0,len(clips)-1)
        # Long picture pools are simply trimmed at normal speed. Slow only the
        # sentence whose own source coverage needs it, not the whole movie.
        local_video_speed = min(1., max(MIN_VIDEO_SPEED,
            sum(original_lengths)/needed if needed > 0 else 1.))
        available = sum(original_lengths)/local_video_speed-fade*max(0,len(clips)-1)
        missing = max(0., required[i]-available)
        row = {'segment_id':sid, 'raw_seconds':raw[i], 'voice_seconds':raw[i]/speed,
               'required_seconds':required[i], 'available_seconds':max(0.,available),
               'missing_seconds':missing, 'clip_ids':[c['clip_id'] for c in clips],
               'video_speed':local_video_speed}
        rows.append(row)
        if missing > 1e-7:
            shortages.append({**row, 'reason':'相关候选不足' if pools[sid] else '本句没有已关联的相关候选，请重新自动匹配'})
        lengths = [length/local_video_speed for length in original_lengths]
        wanted = min(sum(lengths), required[i]+fade*max(0,len(clips)-1))
        # Proportional slack reduction keeps every selected transition window viable.
        floor = min(2*fade+.001, min(lengths, default=.001))
        slack = sum(max(0.,length-floor) for length in lengths)
        reduction = sum(lengths)-wanted
        for c,length in zip(clips,lengths):
            take = length - (reduction*(length-floor)/slack if slack>0 else 0)
            if take <= 0:
                continue
            before = reuse_stats(used)['repeat_count']
            after = reuse_stats(used+[c])['repeat_count']
            window = {k:c[k] for k in ('clip_id','source_path','source_start','source_end')}
            window.update(source_sha256=c.get('source_sha256',''), segment_id=sid, unit_index=i,
                          start=c['start'], end=c['start']+take*local_video_speed,
                          video_speed=local_video_speed, playback_seconds=take,
                          reuse_index=2 if after>before else 1)
            windows.append(window)
            used.append(c)
    status = 'ready' if not shortages and feasible_range else 'shortage'
    timing_issue = ('ready' if feasible_range else
                    'too_short' if total < low else 'too_long')
    warnings = []
    if not feasible_range:
        warnings.append(_timing_reason(total, low, high, phase, timing_issue) + '；请调整参考时长或文案')
    if shortages:
        warnings.append('已完成有界多顺序排片搜索；这不是所有候选组合的数学最优证明，可重新匹配或补充相关素材')
    if abs(total/speed-target) > 5:
        warnings.append(f'参考{target:g}秒，当前计划约{total/speed:.2f}秒；参考时长仅作节奏目标，不因偏差阻止后续处理。')
    plan = {'version':1,'policy':'flexible_reference_v1','status':status,'phase':phase,'speed':speed,
            'video_speed':min((w['video_speed'] for w in windows),default=1.),
            'reference_seconds':target,'reference_is_soft':True,
            'repeat_count':reuse_stats(windows)['repeat_count'], 'total_raw_seconds':total,
            'total_seconds':total/speed,'transition_seconds':fade,'segments':rows,
            'windows':windows,'shortages':shortages,'warnings':warnings,
            'timing_constraint':{'status':timing_issue,'min_seconds':low,'max_seconds':high},
            'search_strategy':'bounded_multi_order','candidate_count':sum(map(len,pools.values()))}
    if status == 'ready' and transition > 0 and fade == 0:
        plan['transition_adjustment'] = {
            'requested_seconds':transition, 'applied_seconds':0.,
            'reason':'transition_overlap_shortage'}
        warnings.append('淡化转场的重叠使相关画面覆盖不足，已自动改为直切。')
    if status == 'ready' and plan['video_speed'] < 1-1e-7:
        warnings.append(f"部分镜头按最低约{plan['video_speed']:.3f}倍适度慢放，覆盖口播；原片范围与复用计数保持不变。")
    return plan
