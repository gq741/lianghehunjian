"""Offline transport/job diagnostic regressions. Every HTTP call is mocked."""
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from mixstudio import providers, tasklog
from mixstudio.jobs import JobManager
from mixstudio.store import Store


class ProviderDiagnosticsTests(unittest.TestCase):
    def failure(self, status=503, body=b'{}', headers=None, fp=None):
        error = HTTPError('https://example.invalid/private-url', status,
                          'private-remote-error', headers or {}, fp or io.BytesIO(body))
        with patch.object(providers, '_http_open', side_effect=error) as call:
            with self.assertRaises(providers.ProviderError) as caught:
                providers._post('https://example.invalid/unused', {}, 'offline-test-key')
        self.assertEqual(call.call_count, 1)
        return caught.exception

    def normalize(self, value):
        self.assertIsNotNone(importlib.util.find_spec('mixstudio.diagnostics'),
                             'Failure metadata normalizer is missing')
        return importlib.import_module('mixstudio.diagnostics').normalize_diagnostic(value)

    def test_http_status_survives_in_metadata_and_message(self):
        for code in (409, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                error = self.failure(code)
                self.assertEqual(getattr(error, 'diagnostic', {}).get('http_status'), code)
                self.assertIn(f'HTTP {code}', str(error))
                self.assertEqual(error.result_unknown, code >= 500 or code == 409)

    def test_business_code_is_not_confused_with_http_status(self):
        for code, label in (('1113', '余额'), ('1302', '限流'), ('1305', '服务繁忙')):
            with self.subTest(code=code):
                error = self.failure(429, json.dumps({'error': {'code': code,
                    'message': 'private-body'}}).encode())
                self.assertEqual(getattr(error, 'diagnostic', {}).get('business_code'), code)
                self.assertIn(label, str(error))
                self.assertEqual(error.code, 'http_429')
                self.assertNotIn('private-body', str(error))

    def test_request_id_and_sanitized_fields_only(self):
        error = self.failure(503, b'{"error":{"code":"1305","message":"private-body"}}',
            {'X-Request-ID': 'req-test-123', 'Authorization': 'private-header'})
        diagnostic = getattr(error, 'diagnostic', {})
        self.assertEqual(diagnostic.get('request_id'), 'req-test-123')
        self.assertEqual(diagnostic.get('request_phase'), 'read')
        self.assertIs(type(diagnostic.get('elapsed_ms')), int)
        text = str(error) + json.dumps(diagnostic)
        for private in ('private-body', 'private-header', 'private-url', 'private-remote-error'):
            self.assertNotIn(private, text)

    def test_secret_echo_and_untrusted_business_code_rejected(self):
        for code in ('1113\nBearer secret', 'https://secret.invalid', ['1113'], True):
            with self.subTest(code=code):
                error = self.failure(429, json.dumps({'error': {'code': code}}).encode(),
                    {'X-Request-ID': 'offline-test-key'})
                diagnostic = getattr(error, 'diagnostic', {})
                self.assertEqual(diagnostic.get('http_status'), 429)
                self.assertNotIn('business_code', diagnostic)
                self.assertNotIn('request_id', diagnostic)

    def test_error_body_read_is_bounded_and_close_always_happens(self):
        class Bounded(io.BytesIO):
            def __init__(self): super().__init__(b'x' * 100000); self.sizes = []
            def read1(self, size): self.sizes.append(size); return super().read(size)
            def read(self, size=-1): self.sizes.append(size); return super().read(size)
        body = Bounded()
        error = self.failure(fp=body)
        self.assertTrue(body.closed)
        self.assertEqual(len(body.sizes), 1)
        self.assertGreater(body.sizes[0], 0)
        self.assertLessEqual(body.sizes[0], 4097)
        self.assertEqual(getattr(error, 'diagnostic', {}).get('http_status'), 503)

    def test_error_body_read_failure_does_not_replace_http_error(self):
        class Broken(io.BytesIO):
            def read1(self, size): raise TimeoutError('private-read-error')
            def read(self, size=-1): raise TimeoutError('private-read-error')
        error = self.failure(fp=Broken())
        self.assertEqual(error.code, 'http_503')
        self.assertEqual(getattr(error, 'diagnostic', {}).get('http_status'), 503)
        self.assertNotIn('private-read-error', str(error))

    def test_deeply_nested_error_json_does_not_replace_http_failure(self):
        error = self.failure(503, b'[' * 1500 + b'0' + b']' * 1500)
        self.assertEqual(error.code, 'http_503')
        self.assertEqual(getattr(error, 'diagnostic', {}).get('http_status'), 503)
        self.assertNotIn('business_code', error.diagnostic)

    def test_transport_failure_has_safe_phase_and_code(self):
        with patch.object(providers, '_http_open', side_effect=URLError(TimeoutError('private'))):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._post('https://example.invalid/', {}, 'offline-test-key')
        diagnostic = getattr(caught.exception, 'diagnostic', {})
        self.assertEqual(diagnostic.get('code'), 'timeout')
        self.assertEqual(diagnostic.get('request_phase'), 'connect')
        self.assertIs(type(diagnostic.get('elapsed_ms')), int)

    def test_normalizer_has_fixed_fields_and_types(self):
        clean = self.normalize({'http_status': True, 'elapsed_ms': -1,
            'response_bytes': float('nan'), 'business_code': 'Bearer secret',
            'request_id': 'sk-secret', 'code': 'private-error', 'raw': 'private',
            'response_shape': {'content': 'string', 'arbitrary': 'private',
                               'choices_count': True, 'finish_reason': 'secret'}})
        self.assertEqual(clean, {'response_shape': {'content': 'string'}})

    def test_schema_metadata_detached_and_preserved(self):
        value = {'code': 'invalid_vision_response', 'response_issue': 'schema_invalid',
                 'schema_detail': (1, 'description', 'missing'), 'vision_batch': (9, 18, 2),
                 'response_shape': {'content': 'string', 'observations_count': 2},
                 'response_sha256': 'a' * 64}
        clean = self.normalize(value)
        self.assertEqual(clean['schema_detail'], [1, 'description', 'missing'])
        self.assertEqual(clean['vision_batch'], [9, 18, 2])
        self.assertEqual(clean['response_sha256'], 'a' * 64)
        value['response_shape']['content'] = 'private'
        self.assertEqual(clean['response_shape']['content'], 'string')

    def test_failure_and_cancel_preserve_diagnostic_in_db_and_log(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / 'state.sqlite')
                manager = JobManager(store, workers=1)
                store.create_job('offline-job', 'match', {})
                error = providers.ProviderError('sanitized', code='http_503', result_unknown=True)
                error.diagnostic = {'http_status': 503, 'business_code': '1305',
                    'vision_batch': [9, 18, 2], 'raw': 'private'}
                def work(event, progress):
                    if cancel: event.set()
                    raise error
                manager._run('offline-job', threading.Event(), work)
                manager.shutdown()
                job = store.job('offline-job')
                diagnostic = job.get('diagnostic', {})
                self.assertEqual(diagnostic.get('http_status'), 503)
                self.assertEqual(diagnostic.get('code'), 'http_503')
                self.assertEqual(job['status'], 'cancelled' if cancel else 'failed')
                self.assertNotIn('raw', diagnostic)
                records = [json.loads(line) for path in (Path(folder)/'logs').glob('*.jsonl')
                           for line in path.read_text('utf8').splitlines()]
                self.assertEqual(records[-1].get('diagnostic'), diagnostic)

    def test_task_log_revalidates_untrusted_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            tasklog.append_event(folder, {'id': 'job1', 'kind': 'match', 'status': 'failed',
                'diagnostic': {'http_status': 503, 'raw': 'private',
                               'business_code': ['private'], 'request_id': 'private\nheader'}})
            data = json.loads(next((Path(folder)/'logs').glob('*.jsonl')).read_text('utf8'))
            self.assertEqual(data.get('diagnostic'), {'http_status': 503})


if __name__ == '__main__':
    unittest.main()
