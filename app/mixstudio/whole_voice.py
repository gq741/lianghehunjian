"""Free checks -> one cached utterance -> local timing -> matching -> one export.

The legacy endpoints remain available for historical jobs. The desktop's default
production endpoint uses this route, with no nested tasks or automatic paid retry.
"""
from pathlib import Path
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
import wave

from .jobs import Cancelled
from .speech import (selection as speech_selection, validate as validate_speech,
    metadata as speech_metadata, recorded_settings, synthesize as synthesize_speech,
    require_retry_selection, require_confirmed_selection)
from .v2 import require_script_timing, validate_duration
from .voice_timing import align_segments
from .output_names import output_directory, reserve_output


def _cancel(event):
    if event is not None and event.is_set():
        raise Cancelled('已取消；成功缓存保留')


def _hash(path, event=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024*1024):
            _cancel(event)
            h.update(block)
    return h.hexdigest()


def _key(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _distribution(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _audio_key(draft, settings):
    from . import providers
    chosen = speech_selection(settings)
    if chosen['provider'] == 'fish':
        # Preserve existing Fish audio and unknown-request fence keys.
        return _key(['whole_voice_v1', draft['voiceover'], settings['voice_id'], providers.FISH_MODEL,
                     providers.FISH_URL, 44100, 'zh-CN', True])
    return _key(['whole_voice_minimax_v1', draft['voiceover'], chosen['voice_id'],
                 chosen['model'], providers.MINIMAX_URL, 32000, 'Chinese', 'wav'])


def _valid_words(words, duration):
    """Validate reusable raw ASR before sentence mapping (which may still fail)."""
    if not isinstance(words, list) or not 1 <= len(words) <= 20000:
        return False
    previous = -1.
    for word in words:
        if not isinstance(word, dict):
            return False
        a, b, text = word.get('start'), word.get('end'), word.get('word')
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or not 0 <= a <= b <= duration+.04
                or a < previous-.02 or not isinstance(text, str) or not text.strip()):
            return False
        previous = a
    return True


def _atomic(path, value):
    tmp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _pcm_duration(path):
    with wave.open(str(path), 'rb') as source:
        count, rate = source.getnframes(), source.getframerate()
        if source.getcomptype() != 'NONE' or rate <= 0 or count <= 0:
            raise ValueError('配音格式异常；未重新调用配音')
        actual = source.readframes(count)
        if len(actual) != count*source.getsampwidth()*source.getnchannels():
            raise ValueError('配音数据不完整；未重新调用配音')
        return count/rate


class WholeVoiceActions:
    def _free_preflight(self, asset_ids, event, progress, *, require_config=True, settings_snapshot=None):
        """Real local I/O and decoder/filter checks, never provider requests."""
        from . import media, providers, script_engine, vision
        settings = self.settings.public() if settings_snapshot is None else dict(settings_snapshot)
        for name in ('ffmpeg', 'ffprobe'):
            if not shutil.which(name):
                raise ValueError(f'免费预检：缺少{name}，尚未调用收费接口')
        assets = self._v2_assets(asset_ids)
        for asset in assets:
            _cancel(event)
            self._unchanged(asset, event)
        # Test runtime/output permissions now, not after buying an utterance.
        for root in (self.root, output_directory(settings['output_dir'])):
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.preflight-', dir=root) as temporary:
                test = Path(temporary)/'write-test'
                test.write_bytes(b'local-only')
            # Budget grows with actual selected data; it is not an exact estimate.
            selected_bytes = sum(Path(a['path']).stat().st_size for a in assets)
            hdr_bytes = sum(Path(a['path']).stat().st_size for a in assets if a.get('media_info', {}).get('hdr'))
            required = max(256*1024**2, selected_bytes//20 + 2*hdr_bytes)
            if shutil.disk_usage(root).free < required:
                raise ValueError('免费预检：可用磁盘空间不足，请清理空间后继续；尚未调用收费接口')
        if settings['bgm_path']:
            self.require_music(settings)
            if not Path(settings['bgm_path']).is_file() or not media.probe(Path(settings['bgm_path'])).get('has_audio'):
                raise ValueError('免费预检：背景音乐不存在或音轨无效；尚未调用收费接口')
        if require_config:
            chosen = speech_selection(settings)
            for name, label in (('deepseek', 'DeepSeek'), ('vision', 'GLM'),
                                (chosen['provider'], chosen['label'])):
                try:
                    providers._key(self.settings.secret(name))
                except providers.ProviderError:
                    raise ValueError(f'免费预检：请在设置中检查{label} API Key；尚未调用收费接口') from None
            script_engine._model(settings['deepseek_model'])
            vision.validate_config(settings['vision_endpoint'], settings['vision_model'])
            try:
                validate_speech(settings)
            except ValueError as exc:
                raise ValueError('免费预检：' + str(exc)) from None
            model = Path(settings['asr_model_path'])
            if (not model.is_absolute() or not all((model/n).is_file()
                    for n in ('model.bin', 'config.json', 'tokenizer.json'))
                    or importlib.util.find_spec('faster_whisper') is None):
                raise ValueError('免费预检：请设置完整的本地语音识别模型，用于整条音频时间对齐；尚未调用收费接口')
            if importlib.util.find_spec('rapidocr_onnxruntime') is None:
                raise ValueError('免费预检：请安装本地RapidOCR字幕定位依赖；尚未调用收费接口')
            from .media_identity import file_version
            from .subtitle_regions import probe_fingerprint
            local_versions = {
                'voice_alignment_worker.py': (str(model), settings['asr_device'],
                    tuple((n, file_version(model/n)) for n in ('model.bin','config.json','tokenizer.json')),
                    _distribution('faster-whisper'), _distribution('ctranslate2')),
                'subtitle_worker.py': probe_fingerprint(),
            }
            from .v2 import visual_slot
            with visual_slot(self.asr_slot,event):
                versions = getattr(self, '_free_local_versions', {})
                for script, args in [('voice_alignment_worker.py',['--model',model,'--device',settings['asr_device']]),
                                     ('subtitle_worker.py',[])]:
                    local_version = (local_versions[script], file_version(Path(__file__).with_name(script)))
                    if versions.get(script) != local_version:
                        code,_ = providers._run_process([sys.executable,'-I','-B','-X','utf8',
                            Path(__file__).with_name(script),'--probe',*args], cancel=event,
                            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1'),timeout=120)
                        if code:
                            label = '语音对齐' if script.startswith('voice') else '旧字幕识别'
                            raise ValueError(f'免费预检：本地{label}模型加载失败；尚未调用收费接口')
                        versions[script] = local_version
                self._free_local_versions = versions
        progress(.08, '免费预检：检查素材解码、HDR准备及本地输出能力')
        prepared = self._prepare_assets(asset_ids, event, lambda v, m: progress(.1+.7*v, '免费预检 · '+m))
        from .media_identity import file_version
        # Cache only successful local smoke checks for an unchanged filesystem
        # version. Source hashes, free space and permissions are still checked.
        smoke = getattr(self, '_free_smoke_versions', set())
        binary = Path(shutil.which('ffmpeg'))
        binary_version = file_version(binary)
        for asset in prepared:
            _cancel(event)
            path = Path(asset.get('prepared_media', {}).get('path', asset['path']))
            version = file_version(path)
            check = ('decode', str(path), version, str(binary), binary_version)
            if version is None or binary_version is None or check not in smoke:
                media._run(['ffmpeg', '-hide_banner', '-v', 'error', '-xerror', '-i', path,
                            '-t', '0.2', '-map', '0:v:0', '-f', 'null', '-'], cancel=event, timeout=30)
                if version is not None and version == file_version(path):
                    smoke.add(check)
        # Exercise exactly the installed encoder/filter family using synthetic
        # media. This also checks font/subtitle support without charging a model.
        encode_check = ('encode', str(binary), binary_version,
            tuple((name, settings[name]) for name in ('width','height','subtitle_font_size','clean_mode','preset')))
        if binary_version is None or encode_check not in smoke:
            with tempfile.TemporaryDirectory(prefix='preflight-', dir=self.root/'work') as tmp:
                directory = Path(tmp)
                media._write_captions(directory/'check.ass', [{'start':0., 'end':.2, 'text':'字幕预检'}],
                                      settings['width'], settings['height'], settings['subtitle_font_size'],
                                      settings['height']//2)
                effects = []
                if settings['clean_mode'] == 'delogo':
                    effects.append('delogo=x=2:y=30:w=80:h=20')
                elif settings['clean_mode'] == 'blur':
                    effects.append('boxblur=1:1')
                effects += ['ass=filename=check.ass', 'format=yuv420p']
                media._run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n',
                    '-f','lavfi','-i','color=c=gray:s=128x128:r=30:d=0.2',
                    '-vf',','.join(effects), *media._encoder_args('cpu', settings['preset']),
                    directory/'check.mp4'], cancel=event, timeout=30, cwd=directory)
            smoke.add(encode_check)
        self._free_smoke_versions = smoke if len(smoke) <= 128 else {encode_check}
        result = {'status':'ready', 'asset_ids':[a['id'] for a in prepared],
                  'source_hashes':[a['sha256'] for a in prepared], 'checked_at':time.time(),
                  'message':'免费预检通过；不包含远端账号余额、网络可用性或整条素材无损保证'}
        self.store.meta('free_preflight', result)
        progress(1., result['message'])
        return result

    def free_preflight(self, asset_ids):
        identifiers = [a['id'] for a in self._v2_assets(asset_ids)]
        with self.action_lock:
            if any(j['kind'] == 'preflight' for j in self.store.active_jobs()):
                raise ValueError('免费预检正在进行，请等待完成')
            return self.jobs.submit('preflight', {'asset_ids':identifiers},
                lambda e, p: self._free_preflight(identifiers, e, p))

    def _whole_audio(self, draft, settings, event, retry_unknown=False):
        from . import providers
        text = draft['voiceover']
        if '<|' in text:
            raise ValueError('请移除文案中的说话人控制标记')
        key = _audio_key(draft, settings)
        cache = self.root/'voice-cache'/('whole-v1-'+key+'.wav')
        metadata = cache.with_suffix('.json')
        marker = cache.with_suffix('.pending.json')
        from .service import _slot
        with _slot(self.voice_slots[int(key[:2],16)], event):
            if cache.exists():
                duration = _pcm_duration(cache)
                signature = _hash(cache)
                if metadata.exists():
                    record = json.loads(metadata.read_text(encoding='utf-8'))
                    if record != {'sha256':signature, 'duration':duration, 'key':key}:
                        raise ValueError('整条配音缓存校验失败，请检查缓存；未自动重新收费')
                else:
                    # Recover only a WAV whose validated digest was durably journaled.
                    journal = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
                    if journal.get('sha256') != signature or journal.get('duration') != duration:
                        raise ValueError('整条配音缺少有效校验记录；未自动重新收费')
                    _atomic(metadata, {'sha256':signature, 'duration':duration, 'key':key})
                marker.unlink(missing_ok=True)
                return cache, key, signature, duration
            if marker.exists() and (not retry_unknown or json.loads(marker.read_text(encoding='utf-8')).get('attempt') != retry_unknown):
                raise providers.ProviderError('上次整条配音结果待确认，请核对后点击任务重试，可能再次计费',
                    code='voice_result_unknown', result_unknown=True)
            _cancel(event)
            with tempfile.TemporaryDirectory(prefix='whole-voice-', dir=self.root/'work') as tmp:
                output = Path(tmp)/'utterance.wav'
                attempt = uuid.uuid4().hex
                _atomic(marker, {'attempt':attempt, 'state':'pending'})
                try:
                    chosen = speech_selection(settings)
                    synthesize_speech(text, output, settings=settings,
                        api_key=self.settings.secret(chosen['provider']),
                        cancel=event, commit_complete=True)
                    # A validated full response has a short non-cancellable commit.
                    duration, signature = _pcm_duration(output), _hash(output)
                    _atomic(marker, {'attempt':attempt,'state':'validated','sha256':signature,'duration':duration})
                    os.replace(output, cache)
                    _atomic(metadata, {'sha256':signature, 'duration':duration, 'key':key})
                    marker.unlink(missing_ok=True)
                except providers.ProviderError as exc:
                    # ProviderError's uncertainty flag distinguishes a definite
                    # rejection/pre-submit cancel from an already submitted try.
                    if not exc.result_unknown:
                        marker.unlink(missing_ok=True)
                    raise
                return cache, key, signature, duration

    def _align_whole(self, draft, audio, signature, duration, settings, event, *, refresh=False):
        from . import providers
        validate_duration(draft['target_seconds'], duration)
        model = Path(settings['asr_model_path'])
        model_identity = [(n, (model/n).stat().st_size, (model/n).stat().st_mtime_ns)
                          for n in ('model.bin','config.json','tokenizer.json')]
        identity = _key(['word_onsets_v1', signature, draft['segments'], str(model), model_identity])
        legacy_path = self.root/'voice-cache'/('alignment-'+identity+'.json')
        from .media_identity import file_version
        raw_identity = _key(['raw_word_onsets_v3_unprompted', signature, str(model), model_identity,
            settings['asr_device'], file_version(Path(__file__).with_name('voice_alignment_worker.py')),
            _distribution('faster-whisper'), _distribution('ctranslate2')])
        record_path = self.root/'voice-cache'/('alignment-'+raw_identity+'.json')
        words = None
        recognized = False
        if not refresh:
            for path in (record_path, legacy_path):
                try:
                    if path.stat().st_size > 2*1024**2:
                        continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                    cached = data.get('words') if isinstance(data, dict) else None
                    if _valid_words(cached, duration):
                        # Valid timestamps alone do not make recognition useful.
                        # A failed text mapping must not permanently replay the
                        # same bad words on every retry. Try another cache, then
                        # at most one free local decode with the current worker.
                        return align_segments(draft['segments'], cached, duration)
                except (OSError, ValueError, TypeError):
                    # This is derived local ASR, not a paid result. Recompute
                    # corrupt records locally without touching the raw utterance.
                    continue
        if words is None:
            recognized = True
            with tempfile.TemporaryDirectory(prefix='voice-align-', dir=self.root/'work') as tmp:
                root = Path(tmp)
                copy = root/'copy.txt'
                copy.write_text(draft['voiceover'], encoding='utf-8')
                output = root/'words.json'
                env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                           HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false')
                code, _ = providers._run_process([sys.executable,'-I','-B','-X','utf8',
                    Path(__file__).with_name('voice_alignment_worker.py'), '--source',audio,
                    '--model',model,'--device',settings['asr_device'],'--text',copy,'--output',output],
                    env=env, cancel=event, timeout=600)
                if code or not output.is_file() or output.stat().st_size > 2*1024**2:
                    raise ValueError('本地语音对齐失败；整条音频已缓存，可免费重新本地对齐，尚未调用画面模型')
                try:
                    words = json.loads(output.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise ValueError('本地语音时间码格式异常；整条音频保留，可免费重新本地对齐') from None
                if not _valid_words(words, duration):
                    raise ValueError('本地语音时间码无效；整条音频保留，可免费重新本地对齐')
        # Cache the recognition independently of successful sentence mapping.
        # Failed words remain available for diagnosis but are never accepted
        # without the same text mapping check. No paid utterance is replayed.
        cache_warnings = []
        if recognized:
            try:
                _atomic(record_path, {'words':words})
            except OSError:
                cache_warnings.append('本次本地识别缓存暂未写入，继续使用内存结果；整条配音保留，后续可能需要重新本地识别')
        timing = align_segments(draft['segments'], words, duration)
        timing['warnings'].extend(cache_warnings)
        return timing

    def _whole_plan(self, draft, settings, *, _inputs=None):
        """Adjacent sentences sharing identical evidence can share a visual unit."""
        from .timeline import plan_timeline
        scenes, candidates = self._timeline_inputs(draft) if _inputs is None else _inputs
        by_sid = {m['segment_id']:m['clip_ids'] for m in candidates}
        timing = draft['whole_voice']
        original = draft['segments']
        raw = timing['segment_seconds']
        if (not isinstance(raw, list) or len(raw) != len(original)
                or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in raw)):
            raise ValueError('整条配音段落时长无效，请免费重新本地对齐')
        source = {s['segment_id']:(s, seconds) for s, seconds in zip(original, raw)}
        ordered = [s['segment_id'] for s in original]
        sentence_groups = timing.get('sentence_groups', [[sid] for sid in ordered])
        if (not isinstance(sentence_groups, list) or any(not isinstance(g, list) or not g for g in sentence_groups)
                or [sid for g in sentence_groups for sid in g] != ordered):
            raise ValueError('整条配音段落分组无效，请免费重新本地对齐')
        # Older timing caches can contain sub-frame sentences too. Group them
        # before *any* planner invocation, without rewriting speech or timestamps.
        units, pending = [], []
        for ids in sentence_groups:
            pending.extend(ids)
            if sum(source[sid][1] for sid in pending) >= .05:
                units.append(pending); pending = []
        if pending:
            if not units:
                raise ValueError('整条配音过短，请检查已保存音频')
            units[-1].extend(pending)
        groups, pools, seconds = [], [], []
        for sids in units:
            segment = {**source[sids[0]][0], 'text':''.join(source[sid][0]['text'] for sid in sids)}
            duration = sum(source[sid][1] for sid in sids)
            # A neighbor's candidate must not conceal a genuinely unmatched
            # sentence, even when its audio occupies only a fraction of a frame.
            ids = list(dict.fromkeys(cid for sid in sids for cid in by_sid.get(sid, []))) if all(by_sid.get(sid) for sid in sids) else []
            if groups and ids and set(ids) == set(pools[-1]['clip_ids']):
                groups[-1]['text'] += segment['text']
                groups[-1]['sentence_ids'].extend(sids)
                seconds[-1] += duration
            else:
                groups.append({**segment,'sentence_ids':list(sids)})
                pools.append({'segment_id':segment['segment_id'], 'clip_ids':ids})
                seconds.append(duration)
        plan = plan_timeline(groups, scenes, pools, target_seconds=draft['target_seconds'],
            transition_seconds=settings['transition_seconds'] if settings['transition']=='fade' else 0,
            voice_seconds=seconds, phase='actual', max_voice_speed=1.)
        plan['visual_groups'] = groups
        plan['timing_method'] = draft['whole_voice']['method']
        return plan, groups, scenes

    def retry_whole_job(self, identifier, allow_paid):
        job = self.store.job(identifier)
        if not job or job['kind'] != 'render' or job['status'] not in {'failed','cancelled','interrupted'}:
            raise ValueError('请选择失败、取消或中断的出片任务')
        draft = self._v2_draft(job['payload']['draft_id'])
        if draft['revision'] != job['payload']['revision']:
            raise ValueError('文案或画面选择已修改，请确认当前版本后重新生成')
        require_retry_selection(job['payload'], self.settings.public())
        return self.produce_whole(draft['id'], allow_paid, retry_unknown=True, retry_payload=job['payload'])

    def realign_whole(self, identifier, *, refresh=False):
        """Local-only repair: require an existing, validated paid utterance."""
        with self.action_lock:
            draft = self._v2_draft(identifier)
            require_script_timing(draft)
            if self._draft_busy(identifier):
                raise ValueError('该文案已有任务，请等待或取消后再本地对齐')
            settings = self.settings.public()
            record = draft.get('whole_audio') or draft.get('whole_voice') or {}
            try:
                audio_settings = recorded_settings(settings, record)
            except ValueError:
                raise ValueError('已有整条配音缺少有效服务、模型或音色记录；本地对齐未调用收费接口') from None
            voice_binding = speech_metadata(audio_settings)
            key = _audio_key(draft, audio_settings)
            audio = self.root/'voice-cache'/('whole-v1-'+key+'.wav')
            if (record.get('key') != key or not audio.is_file()
                    or Path(record.get('path','')).resolve() != audio.resolve()):
                raise ValueError('当前文案没有可复用的整条配音，请先确认生成；本地对齐不会调用收费接口')
            revision = draft['revision']
            def work(event, progress):
                from .v2 import visual_slot
                _cancel(event)
                signature = _hash(audio, event)
                duration = _pcm_duration(audio)
                if signature != record.get('sha256') or duration != record.get('duration'):
                    raise ValueError('整条配音校验失败；未调用收费接口')
                progress(.1, '仅本地重新对齐整条配音，不调用任何收费接口')
                with visual_slot(self.asr_slot, event):
                    if refresh:
                        timing = self._align_whole(draft, audio, signature, duration, settings, event, refresh=True)
                    else:
                        timing = self._align_whole(draft, audio, signature, duration, settings, event)
                _cancel(event)
                with self.action_lock:
                    current = self._v2_draft(identifier)
                    if current['revision'] != revision:
                        raise ValueError('文案版本已变化；整条音频及本地对齐缓存保留')
                    current['whole_voice'] = {**timing, 'path':str(audio), 'sha256':signature,
                        'key':key, 'duration':duration, **voice_binding}
                    current['production_mode'] = 'whole_voice_v1'
                    current.pop('whole_timeline_plan', None)
                    current['timeline_plan'] = None
                    if current.get('clips') and current.get('match_candidates') is not None:
                        self._apply_timeline(current, settings)
                    else:
                        current['match_status'] = 'pending'
                    self.store.put_draft(current)
                return {'draft_id':identifier, 'committed':True, 'duration':duration,
                        'pipeline':'local_alignment', 'warnings':timing['warnings']}
            return self.jobs.submit('alignment', {'draft_id':identifier,'revision':revision}, work)

    def produce_whole(self, identifier, allow_paid, *, retry_unknown=False, retry_payload=None, expected_voice=None):
        if allow_paid is not True:
            raise ValueError('请确认整条配音、GLM识别与DeepSeek匹配的本次费用')
        with self.action_lock:
            draft = self._v2_draft(identifier)
            require_script_timing(draft)
            from .voice_timing import units
            if any(not units(s['text']) for s in draft['segments']):
                raise ValueError('请将仅含标点的段落合并到相邻文案后保存；尚未调用收费接口')
            if self._draft_busy(identifier):
                raise ValueError('该文案已有任务，请等待或取消后再试')
            settings = self.settings.public()
            require_confirmed_selection(expected_voice, settings)
            if retry_payload is not None:
                require_retry_selection(retry_payload, settings)
            voice_binding = speech_metadata(settings)
            revision = draft['revision']
            retry_attempt = None
            if retry_unknown:
                vk = _audio_key(draft, settings)
                pending = self.root/'voice-cache'/('whole-v1-'+vk+'.pending.json')
                if pending.exists() and not (self.root/'voice-cache'/('whole-v1-'+vk+'.wav')).exists():
                    retry_attempt = json.loads(pending.read_text(encoding='utf-8'))['attempt']
            def work(event, progress):
                from . import media
                from .timeline import shortage_message
                self._free_preflight(draft['asset_ids'], event, lambda v,m:progress(.1*v,m),
                                     settings_snapshot=settings)
                progress(.12, '整条配音 · 复用成功缓存或一次生成完整口播')
                audio, key, signature, duration = self._whole_audio(draft,settings,event,retry_attempt)
                saved = self._v2_draft(identifier)
                saved['whole_audio'] = {'path':str(audio),'key':key,'sha256':signature,'duration':duration,
                                        **voice_binding}
                self.store.put_draft(saved)
                _cancel(event)
                progress(.23, f'整条配音已保存，实测{duration:.3f}秒；正在本地对齐文案')
                from .v2 import visual_slot
                with visual_slot(self.asr_slot, event):
                    timing = self._align_whole(draft,audio,signature,duration,settings,event)
                with self.action_lock:
                    current = self._v2_draft(identifier)
                    if current['revision'] != revision:
                        raise ValueError('文案版本已变化，音频缓存保留')
                    current['whole_voice'] = {**timing,'path':str(audio),'sha256':signature,'key':key,
                        'duration':duration, **voice_binding}
                    current['production_mode'] = 'whole_voice_v1'
                    # The old plan references previous sentence timings. Clear
                    # both views in the same commit, including a later cancel.
                    current.update(timeline_plan=None, whole_timeline_plan=None,
                                   match_status='pending')
                    self.store.put_draft(current)
                _cancel(event)
                if not (current.get('clips') and current.get('match_candidates') is not None
                        and current.get('analysis_summary', {}).get('complete') is True):
                    progress(.3, '画面分析与匹配 · 使用真实音频时间轴')
                    self._match_operation(current,settings)(event,lambda v,m:progress(.3+.45*v,m))
                current = self._v2_draft(identifier)
                plan = self._apply_timeline(current, settings)
                groups = plan['visual_groups']
                scenes = self._timeline_inputs(current)[0]
                self.store.put_draft(current)
                if plan['status'] != 'ready':
                    raise ValueError(shortage_message(plan)+' 整条音频和成功分析均已保留，可调整画面后继续。')
                _cancel(event)
                from .subtitle_regions import locate
                cleaning_warnings = locate(self,plan,scenes,event,lambda v,m:progress(.75+.02*v,m))
                # Only actual encoding occupies the render admission, not Fish/GLM.
                with self.render_condition:
                    while self.render_active >= settings['render_concurrency']:
                        _cancel(event)
                        self.render_condition.wait(.2)
                    self.render_active += 1
                try:
                    for aid in current['asset_ids']:
                        self._unchanged(self.store.asset(aid),event)
                    if _hash(audio, event) != signature:
                        raise ValueError('整条音频在匹配期间改变，未渲染或重新收费')
                    originals = {c['clip_id']:c for c in scenes}
                    render_clips = []
                    for row, group in zip(plan['segments'], groups):
                        ids = list(dict.fromkeys(row['clip_ids']))
                        primary = {**originals[ids[0]],'text':group['text']}
                        primary['additional_clips'] = [dict(originals[cid]) for cid in ids[1:]]
                        render_clips.append(primary)
                    # No black plate. Clean the selected source band first;
                    # existing per-window layouts place new text over that band.
                    final = {**settings, 'caption_plate':False,
                        'clean_mode':settings['clean_mode'] if settings['clean_mode']!='none' else 'delogo',
                        'timeline_plan':plan, 'subtitle_segments':timing['captions'],
                        'visual_unit_seconds':[s['voice_seconds'] for s in plan['segments']],
                        'duration_seconds':duration,'min_output_seconds':.1,'max_output_seconds':60}
                    progress(.77,'清理旧字幕并添加新字幕、配音与音乐，正在导出')
                    product_name = (current.get('product') or {}).get('title') or current.get('product_name', '')
                    with reserve_output(settings['output_dir'], product_name) as output:
                        result = media.render_video(render_clips,audio,output,final,cancel=event,
                            progress=lambda v,m='':progress(.77+.22*v,m or '正在合成'))
                    result.update(draft_id=identifier,voice_duration=duration,voice_speed=1.,
                                  timeline_plan=plan,pipeline='whole_voice_v1',
                                  timing_method=timing['method'])
                    result.setdefault('warnings',[]).extend(timing['warnings']+cleaning_warnings)
                    try:
                        self.store.put_draft({**current,'status':'rendered'})
                    except sqlite3.Error:
                        if not result.get('published') or not output.is_file():
                            raise
                        result['warnings'].append('成片已发布，草稿状态暂未更新')
                    return result
                finally:
                    with self.render_condition:
                        self.render_active -= 1
                        self.render_condition.notify_all()
            return self.jobs.submit('render',{'draft_id':identifier,'revision':revision,
                'pipeline':'whole_voice_v1', 'voice':voice_binding},work,production=True)
