"""C02/C04/C05 regressions: isolated runtime, synthetic media and mocked APIs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(os.environ.get('FIX3_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
sys.path.insert(1, str(Path(__file__).resolve().parent))

from mixstudio import providers, script_engine, vision
from mixstudio.v2 import V2Actions
import test_v2 as fixtures


class VisionCommittedBatchTests(unittest.TestCase):
    def run_case(self, *, duration=6., changed=False):
        with tempfile.TemporaryDirectory(prefix='mix-fix3-vision-') as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'
            source.write_bytes(b'synthetic-only')
            asset = {'id': 'a', 'path': str(source),
                     'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
            event = threading.Event()
            info = {'duration': duration, 'width': 160, 'height': 90,
                    'format_start_time': 0., 'video_start_time': 0.,
                    'video_duration': duration, 'video_end_time': duration}

            def post(url, payload, key, **kwargs):
                clips = json.loads(payload['messages'][1]['content'][0]['text'])['clips']
                observations = [{'clip_id': c['clip_id'], 'description': '显示器正面',
                    'tags': ['外观'], 'ocr': '', 'same_product': 'yes', 'flags': [],
                    'confidence': .9, 'reason': '同型号正面'} for c in clips]
                return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'observations': observations})}}]}).encode(), 'application/json'

            decode = vision._decode_response
            def cancel_after_validated(*args, **kwargs):
                result = decode(*args, **kwargs)
                if changed:
                    source.write_bytes(b'replaced-source')
                event.set()
                return result

            options = dict(api_key='synthetic-key', endpoint=vision.DEFAULT_ENDPOINT,
                model=vision.DEFAULT_MODEL, product_title='显示器', cancel=event)
            with patch.object(vision.media, 'probe', return_value=info), \
                 patch.object(vision, '_frame', return_value=(b'\xff\xd8fixture\xff\xd9',
                    {'mean': 100, 'black_ratio': 0})), \
                 patch.object(providers, '_post', side_effect=post) as api, \
                 patch.object(providers, '_http_open', side_effect=AssertionError('NO NETWORK')):
                with patch.object(vision, '_decode_response', side_effect=cancel_after_validated):
                    with self.assertRaises(providers.ProviderError) as caught:
                        vision.analyze_asset(asset, root/'cache', **options)
                self.assertEqual(api.call_count, 1, 'cancel must prevent next batch')
                caches = list((root/'cache').glob('vision-*.json'))
                if changed:
                    self.assertEqual(caught.exception.code, 'source_changed')
                    self.assertTrue(caught.exception.result_unknown)
                    self.assertEqual(caches, [], 'changed source must never be committed')
                    return
                self.assertEqual(caught.exception.code, 'cancelled')
                self.assertEqual(len(caches), 1, 'validated response must survive cancellation')
                self.assertEqual(len(json.loads(caches[0].read_text('utf8'))['records']), 2)
                event.clear()
                result = vision.analyze_asset(asset, root/'cache', **options)
                self.assertEqual(len(result['scenes']), int(duration/3))
                self.assertEqual(api.call_count, int(duration/6), 'resume must reuse committed batch')

    def test_cancel_after_validation_keeps_completed_batch(self):
        self.run_case()

    def test_cancel_stops_next_batch_and_resume_only_requests_pending(self):
        self.run_case(duration=12.)

    def test_late_cancel_does_not_hide_source_change_or_cache_wrong_source(self):
        self.run_case(changed=True)


class CandidateValidationCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mix-fix3-candidates-')
        self.addCleanup(self.temp.cleanup)
        self.assets = {}
        self.clips = []
        for aid in ('a', 'b'):
            path = Path(self.temp.name)/f'{aid}.mp4'
            path.write_bytes(b'synthetic-only-'+aid.encode())
            self.assets[aid] = {'id': aid, 'path': str(path), 'duration': 150., 'sha256': aid*64}
            self.clips.extend({'clip_id': f'{aid}{i}', 'asset_id': aid, 'source_path': str(path),
                'start': i*3., 'end': (i+1)*3., 'usable': True} for i in range(50))
        self.draft = {'clips': self.clips, 'asset_ids': ['a', 'b'],
                      'segments': [{'segment_id': f's{i}', 'text': '显示器正面。'} for i in range(5)]}
        self.matches = [{'segment_id': f's{i}', 'clip_ids': [c['clip_id'] for c in self.clips]}
                        for i in range(5)]
        self.actions = V2Actions()
        self.actions.store = SimpleNamespace(asset=lambda aid: copy.deepcopy(self.assets.get(aid)))

    def test_each_request_reads_each_asset_and_resolves_each_path_once(self):
        resolve = Path.resolve
        with patch.object(self.actions.store, 'asset', wraps=self.actions.store.asset) as lookup, \
             patch.object(Path, 'resolve', autospec=True, side_effect=resolve) as paths:
            result = self.actions._validate_matches(self.draft, self.matches, candidate_limit=4500)
        self.assertEqual(lookup.call_count, 2, 'database cost must depend on unique assets, not references')
        self.assertEqual(paths.call_count, 2, 'shared paths must be resolved once per request')
        self.assertEqual([m['clip_ids'] for m in result], [m['clip_ids'] for m in self.matches])

    def test_repeated_ids_are_deduplicated_without_changing_order(self):
        matches = [{'segment_id': 's0', 'clip_ids': ['b2', 'a1', 'b2', 'a1']}]
        result = self.actions._validate_matches(self.draft, matches, allow_incomplete=True)
        self.assertEqual(result[0]['clip_ids'], ['b2', 'a1'])

    def test_next_request_reloads_changed_asset_record(self):
        self.actions._validate_matches(self.draft, self.matches, candidate_limit=4500)
        self.assets['a']['duration'] = 1.
        with self.assertRaisesRegex(ValueError, '路径或时间范围'):
            self.actions._validate_matches(self.draft, self.matches, candidate_limit=4500)

    def test_every_distinct_scene_still_validates_its_range_and_path(self):
        for field, value in [('start', -1.), ('end', float('nan')),
                             ('end', 1000.), ('source_path', str(Path(self.temp.name)/'other.mp4'))]:
            with self.subTest(field=field, value=value):
                draft = copy.deepcopy(self.draft)
                draft['clips'][-1][field] = value
                with self.assertRaisesRegex(ValueError, '路径或时间范围'):
                    self.actions._validate_matches(draft, self.matches, candidate_limit=4500)


class SharedTimelineValidationTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    tearDown = fixtures.V2Tests.tearDown
    wait = fixtures.V2Tests.wait
    generated = fixtures.V2Tests.generated
    make_draft = fixtures.V2Tests.make_draft
    scenes = fixtures.V2Tests.scenes

    def ready_draft(self):
        draft = self.make_draft()
        draft.update(clips=self.scenes(), match_status='ready',
            matches=[{'segment_id': 's1', 'clip_ids': ['c0', 'c1', 'c2', 'c3', 'c4']}])
        self.studio.store.put_draft(draft)
        return draft

    def test_manual_save_validates_once_and_shares_asset_records_with_planner(self):
        draft = self.ready_draft()
        with patch.object(self.studio, '_validate_matches', wraps=self.studio._validate_matches) as validate, \
             patch.object(self.studio.store, 'asset', wraps=self.studio.store.asset) as lookup:
            saved = self.studio.replace_matches_v2(draft['id'], draft['matches'])
        self.assertEqual(saved['match_status'], 'ready')
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(saved['match_candidates'][0]['clip_ids'], draft['matches'][0]['clip_ids'])

    def test_preflight_and_bound_reuse_inputs_only_within_each_operation(self):
        draft = self.ready_draft()
        with patch.object(self.studio.store, 'asset', wraps=self.studio.store.asset) as lookup:
            self.studio.bound_v2(draft)
        self.assertEqual(lookup.call_count, 1)
        with patch.object(self.studio.store, 'asset', wraps=self.studio.store.asset) as lookup, \
             patch('mixstudio.media._window'):
            self.studio.preflight_v2(draft, self.studio.settings.public())
        self.assertEqual(lookup.call_count, 1)

    def test_manual_save_revalidates_after_record_changes(self):
        draft = self.ready_draft()
        self.studio.replace_matches_v2(draft['id'], draft['matches'])
        changed = {**self.asset, 'duration': 1.}
        self.studio.store.put_asset(changed)
        with self.assertRaisesRegex(ValueError, '路径或时间范围'):
            self.studio.replace_matches_v2(draft['id'], draft['matches'])

    def test_completed_script_and_match_results_mark_commit(self):
        with patch.object(script_engine, 'generate_scripts', return_value=self.generated()):
            generated = self.wait(self.studio.generate_v2({'asset_ids': ['a1'],
                'target_seconds': 25, 'count': 1, 'allow_paid': True}))
        self.assertIs(generated['result'].get('committed'), True)
        draft = self.studio.store.draft(generated['result']['draft_ids'][0])
        ranked = [{'segment_id': 's1', 'clip_ids': ['c0', 'c1', 'c2', 'c3', 'c4']}]
        with patch.object(vision, 'analyze_asset', return_value={'scenes': self.scenes(), 'warnings': []}), \
             patch.object(script_engine, 'match_segments', return_value={'candidates': ranked}):
            matched = self.wait(self.studio.match_v2(draft['id'], True))
        self.assertEqual(matched['status'], 'completed')
        self.assertIs(matched['result'].get('committed'), True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
