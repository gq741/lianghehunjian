"""Script-first orchestration. Reuses the V1 jobs, store and measured renderer."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import math
import re
import time
import uuid

from .jobs import Cancelled
from .product import normalize_jd_link, validate_product


@contextmanager
def visual_slot(semaphore, event):
    while not semaphore.acquire(timeout=.2):
        if event.is_set():
            raise Cancelled('画面分析已取消')
    try:
        if event.is_set():
            raise Cancelled('画面分析已取消')
        yield
    finally:
        semaphore.release()


def duration_bounds(target):
    if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or not 5 <= target <= 60:
        raise ValueError('参考时长须为5～60秒')
    # The user reference guides writing, not a +/-5-second production gate.
    return .1, 60


def validate_duration(target, actual):
    low, high = duration_bounds(target)
    if (isinstance(actual, bool) or not isinstance(actual, (int, float))
            or not math.isfinite(actual) or not low <= actual <= high):
        raise ValueError('实际配音时长须为有效正数且成片最多60秒；参考秒数不是硬性区间。'
                         '已有配音保留，请按实测时长调整；未自动重新计费。')


def require_script_timing(draft):
    """Check actual draft text, never trust persisted/model-supplied estimates."""
    from .script_engine import timing_fields
    text = draft.get('voiceover')
    segments_text = ''.join(s['text'] for s in draft['segments'])
    if text is None:
        text = segments_text
    if text != segments_text:
        raise ValueError('文案与内部段落不一致，请保存文案后重新匹配画面')
    timing = timing_fields(text, draft['target_seconds'])
    if timing['timing_status'] != 'ready':
        raise ValueError(timing['timing_message'])
    return timing


class V2Actions:
    def _v2_draft(self, identifier):
        draft = self.store.draft(identifier)
        if not draft or draft.get('workflow') != 'script_first_v2':
            raise ValueError('请选择第二版文案')
        return draft

    def _draft_busy(self, identifier):
        return any(j['kind'] in {'render', 'match', 'alignment'} and j['payload'].get('draft_id') == identifier
                   and j['status'] in {'queued', 'running', 'cancelling'} for j in self.store.active_jobs())

    def read_product(self, url):
        link = normalize_jd_link(url)
        if not callable(getattr(self, 'product_reader', None)):
            raise ValueError('请使用本地桌面窗口读取京东商品；网页测试模式不提供商品登录窗口')
        with self.action_lock:
            if any(j['kind'] == 'product' for j in self.store.active_jobs()):
                raise ValueError('已有商品读取任务，请完成或取消后再试')
            def work(event, progress):
                result = self.product_reader(link, cancel=event, progress=progress)
                if event.is_set():
                    raise Cancelled('商品读取已取消')
                result = validate_product(result, link['sku'])
                self.store.meta('current_product', result)
                return result
            return self.jobs.submit('product', {'url': link['source']}, work)

    def _v2_assets(self, identifiers):
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50 or any(not isinstance(i, str) for i in identifiers):
            raise ValueError('请选择1～50条同商品素材')
        assets, seen = [], set()
        for identifier in identifiers:
            if identifier in getattr(self, '_recycling_asset_ids', set()):
                raise ValueError('素材正在移入回收站，请重新选择素材后继续')
            asset = self.store.asset(identifier)
            if not asset:
                raise ValueError('部分素材已不存在，请重新选择')
            if asset.get('selectable') is False:
                raise ValueError('部分素材已移走或被替换，请刷新素材库并重新选择')
            signature = asset['sha256']
            if signature not in seen:
                assets.append(asset)
                seen.add(signature)
        return assets

    def generate_v2(self, payload):
        # The active-job check and submit are one transaction for double clicks
        # and concurrent HTTP requests, not only a frontend busy-state hint.
        with self.action_lock:
            return self._submit_generate_v2(payload)

    def _submit_generate_v2(self, payload):
        from . import script_engine, semantic_cache
        if payload.get('allow_paid') is not True:
            raise ValueError('请确认本次 DeepSeek 文案生成调用')
        product = self.store.meta('current_product')
        if not product:
            raise ValueError('请先读取京东商品标题')
        product = validate_product(product)
        assets = self._v2_assets(payload.get('asset_ids'))
        settings = self.settings.public()
        target, count = payload.get('target_seconds', 25), payload.get('count', 1)
        duration_bounds(target)
        if type(count) is not int or not 1 <= count <= 5:
            raise ValueError('一次生成数量须为1～5条')
        if not self.settings.secret('deepseek'):
            raise ValueError('请先在设置中保存 DeepSeek API Key')
        params = {'voice_first':payload.get('voice_first') is True, 'asset_ids': [a['id'] for a in assets], 'target_seconds': target, 'count': count, 'product': product}
        params['generation_identity'] = semantic_cache.fingerprint({
            'product': {'sku': product['sku'], 'title': product['title']},
            'sources': sorted({a['sha256'] for a in assets}), 'target_seconds': target,
            'count': count, 'model': settings['deepseek_model'], 'voice_first': params['voice_first']})
        for job in self.store.active_jobs():
            if (job['kind'] == 'script' and job['status'] in {'queued', 'running', 'cancelling'}
                    and job['payload'].get('generation_identity') == params['generation_identity']):
                return job
        def work(event, progress):
            if payload.get('voice_first') is True:
                self._free_preflight(params['asset_ids'], event, lambda v, m: progress(.14*v, m))
            else:
                self._prepare_assets(params['asset_ids'], event, lambda v, m: progress(.14*v, m))
            for asset in assets:
                self._unchanged(asset, event)
            progress(.15, '依据商品标题与预设脚本规则生成文案')
            generated = script_engine.generate_scripts(product, api_key=self.settings.secret('deepseek'),
                model=settings['deepseek_model'], target_seconds=target, count=count, cancel=event,
                narration_only=params['voice_first'])
            if event.is_set():
                raise Cancelled('文案生成已取消')
            result, needs_adjustment = [], []
            for item in generated:
                stored = {**item, 'id': uuid.uuid4().hex, 'workflow': 'script_first_v2', 'product': product,
                    'product_name': product['title'], 'asset_ids': params['asset_ids'], 'target_seconds': target,
                    'revision': 1, 'status': 'draft', 'created_at': time.time(), 'clips': [], 'matches': [],
                    'match_status': 'pending', 'analysis_warnings': [],
                    'production_mode': 'whole_voice_v1' if payload.get('voice_first') is True else 'legacy_v2'}
                stored.update(script_engine.timing_fields(stored['voiceover'], target))
                # Copy review is not a production gate. Do not represent this
                # user-selected workflow as a passed factual review.
                stored.update(fact_status='not_required', fact_issues=[])
                self.store.put_draft(stored)
                result.append(stored['id'])
                if stored['timing_status'] == 'needs_adjustment':
                    needs_adjustment.append(stored['id'])
            return {'draft_ids': result, 'needs_adjustment_ids': needs_adjustment,
                    'needs_fact_review_ids': [], 'committed': True}
        return self.jobs.submit('script', params, work)

    def edit_v2(self, identifier, voiceover):
        from .script_engine import timing_fields, split_segments
        with self.action_lock:
            draft = self._v2_draft(identifier)
            if self._draft_busy(identifier):
                raise ValueError('请先取消该文案的匹配或合成任务再编辑')
            if not isinstance(voiceover, str) or not voiceover.strip() or len(voiceover) > 2000 or any(ord(c) < 32 and c not in '\n\t' for c in voiceover):
                raise ValueError('文案须为1～2000个可读字符')
            segments = split_segments(voiceover.strip())
            if any(len(s['text']) > 500 for s in segments):
                raise ValueError('请将文案整理为每句最多500字')
            draft.update(segments=segments, voiceover=''.join(s['text'] for s in segments),
                         revision=draft['revision'] + 1,
                         status='draft', match_status='pending', matches=[], match_candidates=[], timeline_plan=None)
            draft.pop('whole_audio', None)
            draft.pop('whole_voice', None)
            draft.pop('whole_timeline_plan', None)
            draft.update(timing_fields(draft['voiceover'], draft['target_seconds']))
            draft.update(fact_status='not_required', fact_issues=[])
            return self.store.put_draft(draft)

    def match_v2(self, identifier, allow_paid):
        from . import vision, script_engine, providers
        if allow_paid is not True:
            raise ValueError('请确认本次视觉识别及 DeepSeek 镜头匹配调用')
        with self.action_lock:
            draft = self._v2_draft(identifier)
            if self._draft_busy(identifier):
                raise ValueError('该文案已有匹配或合成任务')
            require_script_timing(draft)
            settings = self.settings.public()
            if not self.settings.secret('vision') or not self.settings.secret('deepseek'):
                raise ValueError('请先在设置中保存视觉识别和 DeepSeek API Key')
            providers._key(self.settings.secret('vision'))
            providers._key(self.settings.secret('deepseek'))
            vision.validate_config(settings['vision_endpoint'], settings['vision_model'])
            script_engine._model(settings['deepseek_model'])
            assets = self._v2_assets(draft['asset_ids'])
            planned_count = sum(min(90, math.ceil(a['duration'] / 3)) for a in assets)
            if planned_count > 4500:
                raise ValueError('本次预计超过4500个候选镜头，请减少勾选素材；尚未调用视觉接口')
            revision = draft['revision']
            work = self._match_operation(draft, settings)
            return self.jobs.submit('match', {'draft_id': identifier, 'revision': revision}, work)

    def _match_operation(self, draft, settings):
        from . import vision, script_engine, providers
        identifier, revision = draft['id'], draft['revision']
        def work(event, progress):
            prepared = self._prepare_assets(draft['asset_ids'], event, lambda v, m: progress(.04*v, m))
            assets = prepared
            scenes, warnings = [], []
            summary = {'planned': 0, 'completed': 0, 'pending': 0,
                       'failed_batches': [], 'complete': True}
            pause_new_requests = False
            # Local vision sampling and paid batches are deliberately serial.
            with visual_slot(self.asr_slot, event):
                for i, asset in enumerate(assets):
                    self._unchanged(asset, event)
                    stage = f'画面识别 · 素材 {i+1}/{len(assets)}'
                    progress(.05 + .65 * i / len(assets), stage)
                    def vision_progress(fraction, message, index=i):
                        local = max(0, min(1, float(fraction)))
                        progress(.05 + .65 * (index + local) / len(assets),
                                 f'画面识别 · 素材 {index+1}/{len(assets)} · {message}')
                    try:
                        result = vision.analyze_asset(asset, self.root / 'visual-cache',
                            api_key=self.settings.secret('vision'), endpoint=settings['vision_endpoint'],
                            model=settings['vision_model'], product_title=draft['product_name'],
                            cancel=event, progress=vision_progress,
                            allow_partial=True, cache_only=pause_new_requests)
                    except providers.ProviderError as error:
                        raise vision.stage_error(error, stage) from None
                    scenes.extend(result['scenes'])
                    warnings.extend(result.get('warnings', []))
                    item = result.get('analysis_summary') or {
                        'planned':len(result['scenes']), 'completed':len(result['scenes']),
                        'pending':0, 'complete':True, 'failed_batches':[]}
                    for field in ('planned', 'completed', 'pending'):
                        summary[field] += item[field]
                    summary['failed_batches'].extend(
                        {**failure, 'asset_id':asset['id']} for failure in item.get('failed_batches', []))
                    summary['complete'] = summary['complete'] and item['complete']
                    pause_new_requests = pause_new_requests or item.get('pause_new_requests', False)
                if len(scenes) > 4500:
                    raise ValueError('本次镜头过多，请减少勾选素材后分析')
                if event.is_set():
                    raise Cancelled('画面匹配已取消')
                if summary['pending']:
                    warnings.append(f"已分析 {summary['completed']}/{summary['planned']} 个窗口；"
                                    f"本次使用已完成结果自动匹配，剩余 {summary['pending']} 个可稍后继续。")
                # The recognized pool is useful independently of a later
                # semantic call. Persist it before that call, not only at
                # the end of an all-or-nothing multi-provider transaction.
                with self.action_lock:
                    current = self._v2_draft(identifier)
                    if current['revision'] != revision:
                        raise ValueError('文案已改变，请重新匹配画面')
                    known_ids = {scene['clip_id'] for scene in scenes if scene.get('usable') is True}
                    previous = current.get('match_candidates')
                    if previous is None:
                        previous = current.get('matches', [])
                    previous_ids = {cid for choice in previous
                                    for cid in choice['clip_ids']}
                    if not previous_ids.issubset(known_ids):
                        current.update(matches=[], match_candidates=[], timeline_plan=None,
                                       whole_timeline_plan=None, match_status='pending')
                    current.update(clips=scenes, analysis_summary=summary,
                                   analysis_warnings=list(dict.fromkeys(warnings)))
                    self.store.put_draft(current)
                if not any(scene.get('usable') for scene in scenes):
                    result = {'matches': [], 'unmatched':[s['segment_id'] for s in draft['segments']]}
                else:
                    progress(.8, 'DeepSeek 画面匹配 · 按文案自动选择已分析的相关画面')
                    try:
                        result = script_engine.match_segments(draft['segments'], scenes,
                            api_key=self.settings.secret('deepseek'), model=settings['deepseek_model'],
                            product_title=draft['product_name'], cancel=event,
                            cache_dir=self.root / 'semantic-cache',
                            cache_context={'draft_id':identifier, 'revision':revision,
                                'sources':[{'id':a['id'], 'sha256':a['sha256']} for a in assets]},
                            progress=lambda v,m: progress(.8+.18*v, 'DeepSeek 画面匹配 · '+m))
                    except providers.ProviderError as error:
                        raise vision.stage_error(error, 'DeepSeek 画面匹配') from None
            if event.is_set():
                raise Cancelled('画面匹配已取消')
            with self.action_lock:
                current = self._v2_draft(identifier)
                if current['revision'] != revision:
                    raise ValueError('文案已改变，请重新匹配画面')
                current.update(clips=scenes, matches=result.get('matches', []),
                               analysis_warnings=list(dict.fromkeys(warnings)), analysis_summary=summary)
                ranked = result.get('candidates', result.get('matches', []))
                # _apply_timeline validates and normalizes this pool once,
                # then shares the same request-local records with planning.
                current['match_candidates'] = ranked
                current['matching_metadata'] = {k:result[k] for k in
                    ('evaluated_scene_count','semantic_batch_count','selection_policy') if k in result}
                self._apply_timeline(current, settings)
                self.store.put_draft(current)
            return {'draft_id': identifier, 'match_status': current['match_status'],
                    'usable_scenes': sum(bool(c['usable']) for c in scenes), 'unmatched': current.get('unmatched', []),
                    'timeline_plan': current['timeline_plan'], 'analysis_summary':summary, 'committed': True}
        return work

    def _validate_matches(self, draft, matches, allow_incomplete=False, candidate_limit=64, *, _assets=None):
        scenes = {c['clip_id']: c for c in draft['clips']}
        segments = {s['segment_id']: s for s in draft['segments']}
        if not isinstance(matches, list) or len(matches) > len(segments):
            raise ValueError('镜头匹配数据无效')
        # These memos live for one operation only. A later request must read
        # changed/deleted registration records again, not trust global caches.
        assets = {} if _assets is None else _assets
        selected_assets = set(draft['asset_ids'])
        validated_scenes, resolved_paths = set(), {}
        def resolved(value):
            path = Path(value)
            if path not in resolved_paths:
                resolved_paths[path] = path.resolve()
            return resolved_paths[path]
        seen, clean = set(), []
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError('镜头匹配格式无效')
            sid, ids = match.get('segment_id'), match.get('clip_ids')
            if sid not in segments or sid in seen or not isinstance(ids, list) or not (0 if allow_incomplete else 1) <= len(ids) <= candidate_limit:
                raise ValueError('每句需要有效的相关镜头候选')
            unique, unique_ids = [], set()
            for cid in ids:
                if not isinstance(cid, str) or cid not in scenes or scenes[cid].get('usable') is not True:
                    raise ValueError('镜头无效或已被判定为待排除')
                if cid not in validated_scenes:
                    scene = scenes[cid]
                    aid = scene['asset_id']
                    if aid not in selected_assets:
                        raise ValueError('镜头不属于本次选择的素材')
                    if aid not in assets:
                        assets[aid] = self.store.asset(aid)
                    asset = assets[aid]
                    start, end = scene.get('start'), scene.get('end')
                    if (not asset or resolved(scene['source_path']) != resolved(asset['path'])
                        or type(start) not in (float, int) or type(end) not in (float, int)
                        or not math.isfinite(start) or not math.isfinite(end)
                        or not 0 <= start < end <= asset['duration'] + .001):
                        raise ValueError('镜头路径或时间范围与登记素材不一致')
                    validated_scenes.add(cid)
                if cid not in unique_ids:
                    unique.append(cid)
                    unique_ids.add(cid)
            seen.add(sid)
            clean.append({'segment_id':sid, 'clip_ids':unique,
                          'confidence':match.get('confidence',1), 'reason':str(match.get('reason','用户选择'))[:300]})
        if not allow_incomplete and seen != set(segments):
            raise ValueError('部分文案尚未匹配画面，请先完成匹配或手动换片段')
        return clean

    def _timeline_inputs(self, draft):
        # Legacy drafts only know their bound choices, never every same-product
        # scene. Do not invent semantic evidence to avoid a new matching call.
        candidates = draft.get('match_candidates')
        if candidates is None:
            candidates = draft.get('matches', [])
        assets = {}
        candidates = self._validate_matches(draft, candidates, allow_incomplete=True, candidate_limit=4500,
                                            _assets=assets)
        ids = {cid for item in candidates for cid in item['clip_ids']}
        scenes = []
        for scene in draft.get('clips', []):
            if scene['clip_id'] in ids:
                asset = assets[scene['asset_id']]
                extra = {'prepared_media': asset['prepared_media']} if asset.get('prepared_media', {}).get('mode') == 'sdr' else {}
                scenes.append({**scene, 'source_sha256':asset['sha256'], **extra})
        return scenes, candidates

    def _timeline_plan(self, draft, settings, *, voice_seconds=None, phase='estimated', _inputs=None):
        from .timeline import plan_timeline
        if draft.get('whole_voice'):
            return self._whole_plan(draft, settings, _inputs=_inputs)[0]
        scenes, candidates = self._timeline_inputs(draft) if _inputs is None else _inputs
        fade = settings['transition_seconds'] if settings['transition']=='fade' else 0
        return plan_timeline(draft['segments'], scenes, candidates,
            target_seconds=draft['target_seconds'], transition_seconds=fade,
            voice_seconds=voice_seconds, phase=phase,
            max_voice_speed=1. if draft.get('whole_voice') else 1.5)

    def _apply_timeline(self, draft, settings, *, voice_seconds=None, phase='estimated'):
        inputs = self._timeline_inputs(draft)
        draft['match_candidates'] = inputs[1]
        plan = self._timeline_plan(draft, settings, voice_seconds=voice_seconds, phase=phase, _inputs=inputs)
        draft['timeline_plan'] = plan
        if draft.get('whole_voice'):
            draft['whole_timeline_plan'] = plan
        by_id = {g['segment_id']:g.get('sentence_ids',[g['segment_id']]) for g in plan.get('visual_groups', [])}
        draft['matches'] = [{'segment_id':sid, 'clip_ids':s['clip_ids'],
                             'confidence':1., 'reason':'根据已关联语义候选本地自动排片'}
                            for s in plan['segments'] if s['clip_ids']
                            for sid in by_id.get(s['segment_id'],[s['segment_id']])]
        draft['unmatched'] = [sid for s in plan['shortages']
                              for sid in by_id.get(s['segment_id'],[s['segment_id']])]
        draft['match_status'] = 'ready' if plan['status']=='ready' else 'needs_review'
        return plan

    @staticmethod
    def _legacy_timeline_refreshable(draft):
        """Only prior-policy semantic choices qualify; never invent candidates."""
        if (draft.get('whole_voice') or draft.get('workflow') != 'script_first_v2' or
                draft.get('match_status') not in {'ready', 'needs_review'}):
            return False
        plan = draft.get('timeline_plan')
        old = ((not plan and draft.get('match_status') == 'ready') or
               (isinstance(plan, dict) and plan.get('version') == 1 and
                plan.get('policy') != 'flexible_reference_v1'))
        candidates = draft.get('match_candidates')
        if candidates is None:
            candidates = draft.get('matches', [])
        return (old and isinstance(candidates, list) and any(
            isinstance(item, dict) and isinstance(item.get('clip_ids'), list) and item['clip_ids']
            for item in candidates))

    def replace_matches_v2(self, identifier, matches):
        with self.action_lock:
            draft = self._v2_draft(identifier)
            if self._draft_busy(identifier):
                raise ValueError('请先取消该文案任务再调整镜头')
            require_script_timing(draft)
            # Manual save replaces the pool. Deselected old alternatives must
            # not silently reappear in the next automatic allocation. Validation
            # and planning share a request-local snapshot before anything saves.
            draft.pop('whole_timeline_plan', None)
            draft.update(match_candidates=matches, revision=draft['revision']+1)
            self._apply_timeline(draft, self.settings.public())
            return self.store.put_draft(draft)

    def bound_v2(self, draft):
        from .timeline import shortage_message
        require_script_timing(draft)
        if (draft.get('match_status') != 'ready' and not
                (self._legacy_timeline_refreshable(draft) and not self._draft_busy(draft['id']))):
            raise ValueError('请先完成画面识别与匹配，再生成配音')
        inputs = self._timeline_inputs(draft)
        plan = self._timeline_plan(draft, self.settings.public(), _inputs=inputs)
        if plan['status'] != 'ready':
            raise ValueError(shortage_message(plan))
        scenes, candidates = inputs
        by_segment = {m['segment_id']:m['clip_ids'] for m in candidates}
        selected = {m['segment_id']:m['clip_ids'] for m in plan['segments']}
        return [{**s, 'clip_id':selected[s['segment_id']][0],
                 'matched_clip_ids':by_segment[s['segment_id']]} for s in draft['segments']]

    def preflight_v2(self, draft, settings):
        """Use the same planner before Fish; inspect all known candidate files."""
        from . import media
        from .timeline import shortage_message
        require_script_timing(draft)
        inputs = self._timeline_inputs(draft)
        plan = self._timeline_plan(draft, settings, _inputs=inputs)
        if plan['status'] != 'ready':
            raise ValueError(shortage_message(plan))
        scenes, _ = inputs
        cache = {}
        for scene in scenes:
            media._window(scene, cache)
        return plan
