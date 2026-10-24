"""Offline, single-attempt transport limits and sanitized failure diagnostics."""
import io
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from mixstudio import providers as p


class Response:
    headers = {'Content-Type': 'application/json', 'Content-Length': '2'}
    def __init__(self): self.body = io.BytesIO(b'{}')
    def __enter__(self): return self
    def __exit__(self, *args): self.body.close()
    def read(self, size): return self.body.read(size)


class TransportDiagnosticsTests(unittest.TestCase):
    def test_visual_timeout_is_opt_in_not_global(self):
        with patch.object(p, '_http_open', side_effect=lambda *a: Response()) as call:
            self.assertEqual(p._post(p.DEEPSEEK_URL, {}, 'test-placeholder',
                                    timeout_seconds=120, total_seconds=180)[0], b'{}')
            self.assertEqual(call.call_args.args[1], 120)
            p._post(p.DEEPSEEK_URL, {}, 'test-placeholder')
            self.assertEqual(call.call_args.args[1], 60)
            self.assertEqual(call.call_count, 2)

    def test_invalid_limits_never_submit(self):
        with patch.object(p, '_http_open') as call:
            for field in ('timeout_seconds', 'total_seconds'):
                for value in (0, -1, True, float('nan'), float('inf'), '120', 181):
                    with self.subTest(field=field, value=value), self.assertRaises(p.ProviderError):
                        p._post(p.DEEPSEEK_URL, {}, 'test-placeholder', **{field:value})
            call.assert_not_called()

    def test_failure_classes_are_sanitized_and_not_retried(self):
        for reason, code in ((TimeoutError('private-secret'), 'timeout'),
                             (socket.gaierror(-2, 'private-secret'), 'dns'),
                             (ssl.SSLError('private-secret'), 'tls'),
                             (ConnectionResetError('private-secret'), 'connection'),
                             (OSError('private-secret'), 'network')):
            for error in (reason, urllib.error.URLError(reason)):
                with self.subTest(code=code, wrapped=isinstance(error, urllib.error.URLError)), \
                     patch.object(p, '_http_open', side_effect=error) as call:
                    with self.assertRaises(p.ProviderError) as ctx:
                        p._post(p.DEEPSEEK_URL, {}, 'test-placeholder')
                    self.assertEqual(ctx.exception.code, code)
                    self.assertTrue(ctx.exception.result_unknown)
                    self.assertIn('连接或等待响应', str(ctx.exception))
                    self.assertEqual(ctx.exception.request_phase, 'connect')
                    self.assertNotIn('private-secret', str(ctx.exception))
                    self.assertEqual(call.call_count, 1)

    def test_read_timeout_has_read_phase(self):
        response = Response()
        with patch.object(response, 'read', side_effect=TimeoutError('private-secret')), \
             patch.object(p, '_http_open', return_value=response) as call:
            with self.assertRaises(p.ProviderError) as ctx:
                p._post(p.DEEPSEEK_URL, {}, 'test-placeholder')
            self.assertEqual(ctx.exception.code, 'timeout')
            self.assertIn('读取响应', str(ctx.exception))
            self.assertEqual(ctx.exception.request_phase, 'read')
            self.assertNotIn('private-secret', str(ctx.exception))
            self.assertEqual(call.call_count, 1)

    def test_total_limit_is_checked_and_does_not_change_defaults(self):
        with patch.object(p, '_http_open', return_value=Response()) as call, \
             patch.object(p.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaises(p.ProviderError) as ctx:
                p._post(p.DEEPSEEK_URL, {}, 'test-placeholder',timeout_seconds=120,total_seconds=10)
            self.assertEqual(ctx.exception.code, 'timeout')
            self.assertEqual(call.call_args.args[1], 10)
            self.assertEqual(call.call_count, 1)


if __name__ == '__main__': unittest.main()
