"""MiniMax synchronous TTS contracts; synthetic PCM and mocks, never paid I/O."""
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import wave

from mixstudio import providers as p


def pcm_wav(*, rate=32000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b'\0' * (3200 * channels * width))
    return output.getvalue()


def payload(audio=None, *, status=2, code=0, message='success'):
    return {'base_resp': {'status_code': code, 'status_msg': message},
            'data': {'status': status, 'audio': (audio or pcm_wav()).hex()}}


class Response:
    def __init__(self, body, *, declared=None, content_type='application/json'):
        self.body = io.BytesIO(body)
        self.headers = {'Content-Type': content_type,
                        'Content-Length': str(len(body) if declared is None else declared)}
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.body.close()


class MiniMaxProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'voice.wav'

    def call(self, **kwargs):
        values = dict(text='这款商品方便日常使用。', output=self.output,
                      api_key='test-minimax-placeholder')
        values.update(kwargs)
        return p.synthesize_minimax_voice(**values)

    def response(self, value):
        return Response(json.dumps(value).encode('utf-8'))

    def assert_failure(self, *, code=None, unknown=True, **kwargs):
        with self.assertRaises(p.ProviderError) as context:
            self.call(**kwargs)
        error = context.exception
        if code is not None:
            self.assertEqual(error.code, code)
        self.assertIs(error.result_unknown, unknown)
        self.assertFalse(self.output.exists())
        return error

    @patch.object(p, '_http_open')
    def test_official_request_and_complete_pcm_result(self, opener):
        audio = pcm_wav()
        opener.return_value = self.response(payload(audio))
        result = self.call()
        self.assertEqual(result, {'path': str(self.output), 'duration': .1})
        self.assertEqual(self.output.read_bytes(), audio)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.minimax.cn/v1/t2a_v2')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-minimax-placeholder')
        self.assertEqual(json.loads(request.data), {
            'model': 'speech-2.8-hd', 'text': '这款商品方便日常使用。',
            'stream': False, 'output_format': 'hex', 'language_boost': 'Chinese',
            'voice_setting': {'voice_id': 'male-qn-qingse', 'speed': 1, 'vol': 1, 'pitch': 0},
            'audio_setting': {'sample_rate': 32000, 'format': 'wav', 'channel': 1},
        })
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, '_http_open')
    def test_selected_turbo_and_voice(self, opener):
        opener.return_value = self.response(payload())
        self.call(model='speech-2.8-turbo', voice_id='Chinese (Mandarin)_Gentle_Woman')
        sent = json.loads(opener.call_args.args[0].data)
        self.assertEqual(sent['model'], 'speech-2.8-turbo')
        self.assertEqual(sent['voice_setting']['voice_id'], 'Chinese (Mandarin)_Gentle_Woman')

    @patch.object(p, '_http_open')
    def test_voice_boundary_accepts_unicode_spaces_and_256_characters(self, opener):
        for voice in ('声', '中文定制音色 (柔和 女声) 🎙', '声' * 256):
            with self.subTest(length=len(voice)):
                opener.return_value = self.response(payload())
                self.call(voice_id=voice)
                sent = json.loads(opener.call_args.args[0].data)
                self.assertEqual(sent['voice_setting']['voice_id'], voice)
                self.assertEqual(sent['language_boost'], 'Chinese')
                self.output.unlink()

    @patch.object(p, '_http_open')
    def test_preflight_invalid_inputs_do_not_submit(self, opener):
        cases = [dict(text=''), dict(text='a' * 2001), dict(api_key='bad key'),
                 dict(model='unlisted-model'), dict(voice_id=''), dict(voice_id='bad\nvoice'),
                 dict(voice_id='声' * 257), dict(voice_id='bad\tvoice'),
                 dict(voice_id='bad\x7fvoice'), dict(voice_id='bad\x85voice'),
                 dict(voice_id='bad\x9fvoice'),
                 dict(output=Path('relative.wav')), dict(output=self.output.with_suffix('.mp3')),
                 dict(output=self.output.parent / 'null\0.wav'),
                 dict(output=self.output.parent / 'missing' / 'voice.wav')]
        for values in cases:
            with self.subTest(values=list(values)):
                self.assert_failure(unknown=False, **values)
        opener.assert_not_called()

    @patch.object(p, '_http_open')
    def test_existing_output_is_not_overwritten_or_submitted(self, opener):
        self.output.write_bytes(b'existing')
        with self.assertRaises(p.ProviderError) as context:
            self.call()
        self.assertEqual(context.exception.code, 'output_exists')
        self.assertFalse(context.exception.result_unknown)
        self.assertEqual(self.output.read_bytes(), b'existing')
        opener.assert_not_called()

    @patch.object(p, '_http_open')
    def test_response_is_validated_before_any_output_is_created(self, opener):
        def respond(*args, **kwargs):
            self.assertFalse(self.output.exists())
            return self.response(payload())
        opener.side_effect = respond
        self.call()

    @patch.object(p, '_http_open')
    def test_documented_rejections_are_classified_without_echo(self, opener):
        for business, category in [(1002, 'request_limit'), (1004, 'invalid_key'),
                (1008, 'provider_error'), (1026, 'invalid_input'), (1042, 'invalid_input'),
                (1045, 'request_limit'), (2013, 'invalid_input'), (20132, 'invalid_input'),
                (2014, 'invalid_input'), (2022, 'provider_error'), (2042, 'provider_error'),
                (2043, 'invalid_input'), (2049, 'invalid_key'), (2053, 'provider_error'),
                (2054, 'invalid_input'), (2067, 'provider_error')]:
            with self.subTest(business=business):
                opener.return_value = self.response(payload(code=business,
                    message='test-minimax-placeholder 这款商品方便日常使用。 raw server message'))
                error = self.assert_failure(code=category, unknown=False)
                self.assertEqual(error.diagnostic['business_code'], str(business))
                surface = str(error) + json.dumps(error.diagnostic, ensure_ascii=False)
                for private in ('test-minimax-placeholder', '这款商品方便日常使用。', 'raw server message'):
                    self.assertNotIn(private, surface)
        self.assertEqual(opener.call_count, 16)

    @patch.object(p, '_http_open')
    def test_unknown_system_timeout_and_output_filter_keep_uncertainty(self, opener):
        for business in (1000, 1001, 1024, 1027, 1033, 2064, 2066, 999999):
            with self.subTest(business=business):
                opener.return_value = self.response(payload(code=business))
                error = self.assert_failure(code='provider_error')
                self.assertEqual(error.diagnostic['business_code'], str(business))
        self.assertEqual(opener.call_count, 8)

    @patch.object(p, '_http_open')
    def test_business_code_cannot_echo_numeric_key(self, opener):
        opener.return_value = self.response(payload(code=1008, message='1008'))
        error = self.assert_failure(api_key='1008', unknown=False)
        self.assertNotIn('1008', str(error) + json.dumps(error.diagnostic))

    @patch.object(p, '_http_open')
    def test_json_and_schema_failures_keep_uncertainty(self, opener):
        raw_cases = [b'not-json', b'{', b'[]', b'null', b'{}',
                     b'{"base_resp":{"status_code":0,"status_code":1008}}']
        values = [{'base_resp': None}, {'base_resp': {'status_code': True}},
                  {'base_resp': {'status_code': '0'}}, {'base_resp': {'status_code': 0}},
                  {'base_resp': {'status_code': 0}, 'data': None}]
        for raw in raw_cases + [json.dumps(v).encode() for v in values]:
            with self.subTest(raw=raw[:30]):
                opener.return_value = Response(raw)
                self.assert_failure(code='invalid_response')

    @patch.object(p, '_http_open')
    def test_content_type_is_json_not_html_or_audio(self, opener):
        for content_type in ('text/html', 'audio/wav', 'text/plain'):
            with self.subTest(content_type=content_type):
                opener.return_value = Response(json.dumps(payload()).encode(), content_type=content_type)
                self.assert_failure(code='invalid_response')

    @patch.object(p, '_http_open')
    def test_incomplete_or_unknown_data_status_is_not_committed(self, opener):
        for status in (0, 1, 3, None, '2', True):
            with self.subTest(status=status):
                opener.return_value = self.response(payload(status=status))
                self.assert_failure(code='invalid_response')

    @patch.object(p, '_http_open')
    def test_hex_requires_nonempty_even_unbroken_hexadecimal(self, opener):
        for audio in ('', '0', 'gg', '52 49', 'https://private/audio.wav', None, 42):
            with self.subTest(audio=audio):
                value = payload()
                value['data']['audio'] = audio
                opener.return_value = self.response(value)
                self.assert_failure(code='invalid_audio')

    @patch.object(p, '_http_open')
    def test_truncated_wrong_format_and_streaming_placeholders_are_rejected(self, opener):
        audio = pcm_wav()
        streaming = bytearray(audio)
        struct.pack_into('<I', streaming, 4, 0xffffffff)
        struct.pack_into('<I', streaming, 40, 0xffffffff)
        wrong_length = bytearray(audio)
        struct.pack_into('<I', wrong_length, 40, len(audio))
        wrong_encoding = bytearray(audio)
        struct.pack_into('<H', wrong_encoding, 20, 3)
        for invalid in (audio[:-1], audio + b'x', b'RIFF' + b'\0' * 60,
                        bytes(streaming), bytes(wrong_length), bytes(wrong_encoding),
                        pcm_wav(rate=44100), pcm_wav(channels=2), pcm_wav(width=1)):
            with self.subTest(size=len(invalid)):
                opener.return_value = self.response(payload(invalid))
                self.assert_failure(code='invalid_audio')

    @patch.object(p, '_http_open')
    def test_zero_frames_and_invalid_rate_are_rejected_by_pcm_validator(self, opener):
        empty = bytearray(pcm_wav()[:44])
        struct.pack_into('<I', empty, 4, 36)
        struct.pack_into('<I', empty, 40, 0)
        zero_rate = bytearray(pcm_wav())
        struct.pack_into('<I', zero_rate, 24, 0)
        struct.pack_into('<I', zero_rate, 28, 0)
        for audio in (bytes(empty), bytes(zero_rate)):
            with self.subTest(size=len(audio)):
                with self.assertRaises(ValueError):
                    p._normalize_fish_wav(audio)
                opener.return_value = self.response(payload(audio))
                self.assert_failure(code='invalid_audio')

    def test_pcm_validator_rejects_complete_audio_over_600_seconds(self):
        # 8 kHz keeps this genuine complete PCM fixture below MAX_AUDIO_BYTES;
        # the validator must reject duration, not merely RIFF truncation/size.
        frames = b'\0\0' * (8000 * 601)
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(frames)
        audio = output.getvalue()
        self.assertLess(len(audio), p.MAX_AUDIO_BYTES)
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            self.assertEqual(wav.getnframes() / wav.getframerate(), 601)
        with self.assertRaises(ValueError):
            p._normalize_fish_wav(audio)

    @patch.object(p, '_http_open')
    def test_cancel_before_submit_is_not_unknown(self, opener):
        cancel = threading.Event()
        cancel.set()
        self.assert_failure(code='cancelled', unknown=False, cancel=cancel)
        opener.assert_not_called()

    @patch.object(p, '_http_open')
    def test_cancel_during_response_is_unknown_and_never_retried(self, opener):
        cancel = threading.Event()
        response = self.response(payload())
        original_read = response.read
        def read(size):
            result = original_read(size)
            cancel.set()
            return result
        response.read = read
        opener.return_value = response
        self.assert_failure(code='cancelled', cancel=cancel)
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, '_http_open')
    def test_commit_complete_preserves_fully_received_success_on_late_cancel(self, opener):
        cancel = threading.Event()
        response = self.response(payload())
        original_read = response.read
        def read(size):
            value = original_read(size)
            cancel.set()
            return value
        response.read = read
        opener.return_value = response
        self.call(cancel=cancel, commit_complete=True)
        self.assertTrue(self.output.is_file())

    @patch.object(p, '_http_open')
    def test_network_timeout_is_unknown_and_has_no_retry_or_fish_fallback(self, opener):
        opener.side_effect = TimeoutError('test-minimax-placeholder raw message')
        with patch.object(p, 'synthesize_voice') as fish:
            error = self.assert_failure(code='timeout')
        fish.assert_not_called()
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn('test-minimax-placeholder', str(error))

    @patch.object(p, '_http_open')
    def test_http_auth_rejection_remains_a_single_attempt(self, opener):
        opener.side_effect = urllib.error.HTTPError('https://api.minimax.cn', 401,
                                                   'private message', {}, io.BytesIO(b'{}'))
        self.assert_failure(code='http_401', unknown=False)
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, '_http_open')
    def test_large_hex_json_uses_its_own_bounded_limit(self, opener):
        value = payload()
        value['ignored_metadata'] = 'x' * (p.MAX_JSON_BYTES + 1)
        response = self.response(value)
        opener.return_value = response
        self.call()
        self.assertGreaterEqual(p.MAX_MINIMAX_JSON_BYTES, 64 * 1024 * 1024)
        self.assertLessEqual(max(response.read_sizes), 65536)

    @patch.object(p, '_http_open')
    def test_declared_oversize_response_is_rejected_without_read(self, opener):
        response = Response(b'', declared=p.MAX_MINIMAX_JSON_BYTES + 1)
        opener.return_value = response
        self.assert_failure(code='response_too_large')
        self.assertEqual(response.read_sizes, [])

    @patch.object(p, '_http_open')
    def test_truncated_http_body_is_unknown(self, opener):
        opener.return_value = Response(b'{}', declared=10)
        self.assert_failure(code='network')

    @patch.object(p, '_http_open')
    def test_actual_chunked_json_requires_terminating_frame(self, opener):
        body = json.dumps(payload()).encode()
        chunk = f'{len(body):x}\r\n'.encode() + body + b'\r\n'
        for complete in (False, True):
            with self.subTest(complete=complete):
                wire = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                        b'Transfer-Encoding: chunked\r\n\r\n' + chunk
                        + (b'0\r\n\r\n' if complete else b''))
                class Socket:
                    def makefile(self, *args):
                        return io.BytesIO(wire)
                response = p.http.client.HTTPResponse(Socket())
                response.begin()
                opener.return_value = response
                if complete:
                    self.call()
                    self.assertEqual(self.output.read_bytes(), pcm_wav())
                else:
                    self.assert_failure(code='network')

    @patch.object(p, '_http_open')
    def test_hex_audio_size_limit_is_checked_before_decode(self, opener):
        opener.return_value = self.response(payload())
        with patch.object(p, 'MAX_AUDIO_BYTES', 64), patch.object(p, '_normalize_fish_wav') as validate:
            self.assert_failure(code='invalid_audio')
        validate.assert_not_called()

    @patch.object(p, '_http_open')
    def test_commit_complete_does_not_ignore_cancel_before_body_completion(self, opener):
        cancel = threading.Event()
        value = payload()
        value['ignored_metadata'] = 'x' * 65536
        response = self.response(value)
        original_read = response.read
        def read(size):
            result = original_read(size)
            cancel.set()
            return result
        response.read = read
        opener.return_value = response
        self.assert_failure(code='cancelled', cancel=cancel, commit_complete=True)
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, '_http_open')
    def test_no_framing_is_unknown(self, opener):
        response = self.response(payload())
        response.headers.pop('Content-Length')
        opener.return_value = response
        self.assert_failure(code='invalid_response')

    @patch.object(p, '_http_open')
    def test_racing_existing_output_is_preserved_after_paid_result(self, opener):
        def respond(*args, **kwargs):
            self.output.write_bytes(b'concurrent-owner')
            return self.response(payload())
        opener.side_effect = respond
        with self.assertRaises(p.ProviderError) as context:
            self.call()
        self.assertEqual(context.exception.code, 'output_exists')
        self.assertTrue(context.exception.result_unknown)
        self.assertEqual(self.output.read_bytes(), b'concurrent-owner')

    @patch.object(p, '_http_open')
    def test_write_failure_is_sanitized_and_partial_output_is_cleaned(self, opener):
        opener.return_value = self.response(payload())
        with patch.object(p.os, 'fsync', side_effect=OSError('raw private path and key')):
            error = self.assert_failure(code='file_error')
        self.assertNotIn('raw private path and key', str(error))


if __name__ == '__main__':
    unittest.main()
