"""Offline regressions for resource-scoped CDN redirects, not a new URL allowlist."""
from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mixstudio import providers as p
import test_download_candidate_fallback as fixture


SOURCE_HOST = "v5-coldx.douyinvod.com"
SOURCE_PATH = "/c54e8acbbf1234567890/67181234/video/tos/cn/tos-cn-ve-15/example.mp4"
SOURCE = "https://" + SOURCE_HOST + SOURCE_PATH + "?original=fixture"
TARGET_HOST = "dc-123abc.bdcgslb.com"
TARGET_PATH = "/" + SOURCE_HOST + SOURCE_PATH
TARGET = "https://" + TARGET_HOST + TARGET_PATH + "?server=fixture&new=parameter"
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]


class DelegatedDownloadRedirectTests(unittest.TestCase):
    def setUp(self):
        self.dns_patch = patch.object(p, "_public_download_addresses", return_value=PUBLIC)
        self.dns = self.dns_patch.start()
        self.addCleanup(self.dns_patch.stop)
        self.open = self.enterContext(patch.object(p, "_open_public_download"))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")))

    def follow(self, url=SOURCE, allowed=None, **kwargs):
        return p._follow_download_redirects(
            url, p.DOWNLOAD_MEDIA_HOSTS if allowed is None else allowed,
            cancel=kwargs.pop("cancel", None),
            deadline=kwargs.pop("deadline", time.monotonic() + 10),
            user_agent=p.DOWNLOAD_MEDIA_UA, **kwargs)

    @staticmethod
    def redirect(target=TARGET, **kwargs):
        return fixture.Response(status=302, headers={"Location": target}, **kwargs)

    def test_exact_media_resource_handoff_preserves_server_query(self):
        first, last = self.redirect(), fixture.Response(fixture.MP4)
        self.open.side_effect = [first, last]
        self.assertIs(self.follow(), last)
        self.assertEqual([c.args[0] for c in self.open.call_args_list], [SOURCE, TARGET])
        self.assertEqual([c.args[0] for c in self.dns.call_args_list], [SOURCE_HOST, TARGET_HOST])
        self.assertEqual(self.open.call_args_list[1].args[1], PUBLIC)
        self.assertTrue(first.closed)
        self.assertFalse(last.closed)
        last.close()

    def test_checked_url_and_direct_entry_still_reject_delegated_host(self):
        with self.assertRaises(p.ProviderError):
            p._checked_download_url(TARGET, p.DOWNLOAD_MEDIA_HOSTS)
        with self.assertRaises(p.ProviderError):
            self.follow(TARGET)
        self.open.assert_not_called()
        self.assertNotIn("bdcgslb.com", p.DOWNLOAD_PLATFORM_DOMAINS)
        self.assertNotIn("bdcgslb.com", p.DOWNLOAD_MEDIA_HOSTS)

    def test_delegation_does_not_mutate_global_allowlist_or_leak_across_requests(self):
        original_media = p.DOWNLOAD_MEDIA_HOSTS
        original_platforms = p.DOWNLOAD_PLATFORM_DOMAINS
        self.open.side_effect = [self.redirect(), fixture.Response(fixture.MP4)]
        self.follow().close()
        self.open.reset_mock()
        with self.assertRaises(p.ProviderError):
            self.follow(TARGET)
        self.open.assert_not_called()
        self.assertIs(p.DOWNLOAD_MEDIA_HOSTS, original_media)
        self.assertIs(p.DOWNLOAD_PLATFORM_DOMAINS, original_platforms)

    def test_bad_hostname_shapes_are_rejected_before_dns_or_connection(self):
        hosts = ["bdcgslb.com", "dc-123abc.bdcgslb.com.evil.test", "evilbdcgslb.com",
                 "nested.dc-123abc.bdcgslb.com", "-bad.bdcgslb.com", "bad-.bdcgslb.com",
                 "a" * 64 + ".bdcgslb.com", "127.0.0.1", "[::1]"]
        for host in hosts:
            with self.subTest(host=host):
                self.open.reset_mock()
                self.dns.reset_mock()
                first = self.redirect("https://" + host + TARGET_PATH)
                self.open.side_effect = [first, fixture.Response()]
                with self.assertRaises(p.ProviderError) as caught:
                    self.follow()
                self.assertEqual(caught.exception.code, "invalid_url")
                self.assertEqual(self.open.call_count, 1)
                self.assertEqual(self.dns.call_count, 1)
                self.assertTrue(first.closed)

    def test_resource_path_must_match_exact_source_host_and_path(self):
        paths = ["/video/other.mp4", TARGET_PATH + "/extra", TARGET_PATH.replace("example", "other"),
                 TARGET_PATH.replace(SOURCE_HOST, "v6-coldx.douyinvod.com"),
                 TARGET_PATH.replace("example", "%65xample"), TARGET_PATH.replace("/video/", "/./video/")]
        for path in paths:
            with self.subTest(path=path):
                self.open.reset_mock()
                self.open.side_effect = [self.redirect("https://" + TARGET_HOST + path), fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow()
                self.assertEqual(self.open.call_count, 1)

    def test_other_approved_media_domains_cannot_delegate(self):
        for host in ("v5.bytecdn.cn", "douyinvod.com", "aweme.snssdk.com"):
            with self.subTest(host=host):
                self.open.reset_mock()
                source = "https://" + host + SOURCE_PATH
                target = "https://" + TARGET_HOST + "/" + host + SOURCE_PATH
                self.open.side_effect = [self.redirect(target), fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow(source)
                self.assertEqual(self.open.call_count, 1)

    def test_delegation_is_not_enabled_in_share_or_feed_contexts(self):
        contexts = [("https://v.douyin.com/example/", p.DOWNLOAD_SHARE_HOSTS),
                    ("https://aweme.snssdk.com/aweme/v1/feed/", frozenset({"aweme.snssdk.com"})),
                    (SOURCE, frozenset({SOURCE_HOST}))]
        for source, allowed in contexts:
            with self.subTest(source=source, allowed=allowed):
                self.open.reset_mock()
                self.open.side_effect = [self.redirect(), fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow(source, allowed)
                self.assertEqual(self.open.call_count, 1)

    def test_protocol_port_credentials_fragment_and_control_still_rejected(self):
        targets = [TARGET.replace("https://", "http://"),
                   TARGET.replace(TARGET_HOST, TARGET_HOST + ":444"),
                   TARGET.replace(TARGET_HOST, "user:password@" + TARGET_HOST),
                   TARGET + "#fragment", TARGET + " ", TARGET + "\x7f",
                   TARGET + "\t", TARGET + "\r\n", "\n" + TARGET]
        for target in targets:
            with self.subTest(target=repr(target)):
                self.open.reset_mock()
                self.open.side_effect = [self.redirect(target), fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow()
                self.assertEqual(self.open.call_count, 1)

    def test_dot_segments_or_encoded_source_path_cannot_delegate(self):
        for path in ("/video/../other.mp4", "/video/./other.mp4", "/video/%2e%2e/other.mp4",
                     "/video/%65xample.mp4", "/video/a%2fb.mp4"):
            with self.subTest(path=path):
                self.open.reset_mock()
                source = "https://" + SOURCE_HOST + path
                target = "https://" + TARGET_HOST + "/" + SOURCE_HOST + path
                self.open.side_effect = [self.redirect(target), fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow(source)
                self.assertEqual(self.open.call_count, 1)

    def test_private_dns_on_delegated_target_is_checked_before_connection(self):
        self.dns_patch.stop()
        def addresses(host, *_args, **_kwargs):
            if host == TARGET_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
            return PUBLIC
        self.open.side_effect = [self.redirect(), fixture.Response()]
        with patch.object(socket, "getaddrinfo", side_effect=addresses) as resolver:
            with self.assertRaises(p.ProviderError) as caught:
                self.follow()
        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual([c.args[0] for c in resolver.call_args_list], [SOURCE_HOST, TARGET_HOST])
        self.assertEqual(self.open.call_count, 1)

    def test_tls_error_on_delegated_target_is_not_swallowed(self):
        self.open.side_effect = [self.redirect(), p.ProviderError("TLS check failed", code="download_tls")]
        with self.assertRaises(p.ProviderError) as caught:
            self.follow()
        self.assertEqual(caught.exception.code, "download_tls")
        self.assertEqual(self.open.call_count, 2)

    def test_delegated_authorization_is_not_reused_for_relative_resource(self):
        for location in ("/different-resource.mp4", "?new-resource=1", TARGET, "https://other.bdcgslb.com" + TARGET_PATH):
            with self.subTest(location=location):
                self.open.reset_mock()
                second = self.redirect(location)
                self.open.side_effect = [self.redirect(), second, fixture.Response()]
                with self.assertRaises(p.ProviderError):
                    self.follow()
                self.assertEqual(self.open.call_count, 2)
                self.assertTrue(second.closed)

    def test_delegate_can_redirect_back_to_an_approved_media_host(self):
        final = fixture.Response(fixture.MP4)
        self.open.side_effect = [self.redirect(), self.redirect(SOURCE), final]
        self.assertIs(self.follow(), final)
        self.assertEqual([c.args[0] for c in self.open.call_args_list], [SOURCE, TARGET, SOURCE])
        final.close()

    def test_five_hop_limit_includes_delegated_hops(self):
        responses = [self.redirect(), self.redirect(SOURCE), self.redirect(), self.redirect(SOURCE), self.redirect()]
        self.open.side_effect = responses
        with self.assertRaises(p.ProviderError) as caught:
            self.follow()
        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(self.open.call_count, 5)
        self.assertTrue(all(response.closed for response in responses))

    def test_same_deadline_and_cancellation_are_passed_to_every_hop(self):
        event = threading.Event()
        deadline = time.monotonic() + 15
        self.open.side_effect = [self.redirect(), fixture.Response(fixture.MP4)]
        self.follow(cancel=event, deadline=deadline).close()
        for call in self.dns.call_args_list + self.open.call_args_list:
            self.assertIs(call.kwargs["cancel"], event)
            self.assertEqual(call.kwargs["deadline"], deadline)

    def test_cancel_during_first_response_stops_before_target(self):
        event = threading.Event()
        self.open.side_effect = [self.redirect(on_close=event.set), fixture.Response()]
        with self.assertRaises(p.ProviderError) as caught:
            self.follow(cancel=event)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(self.open.call_count, 1)

    def test_expired_deadline_before_target_stops_without_new_connection(self):
        clock = [1000.0]
        def expire():
            clock[0] = 1011.0
        self.open.side_effect = [self.redirect(on_close=expire), fixture.Response()]
        with patch.object(p.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(p.ProviderError) as caught:
                self.follow(deadline=1010.0)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(self.open.call_count, 1)

    def test_delegated_http_401_and_429_stop_and_never_expose_query(self):
        for status in (401, 429):
            with self.subTest(status=status):
                self.open.reset_mock()
                response = fixture.Response(status=status)
                self.open.side_effect = [self.redirect(), response, fixture.Response()]
                with self.assertRaises(p.ProviderError) as caught:
                    self.follow()
                self.assertEqual(self.open.call_count, 2)
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("server=fixture", str(caught.exception))
                self.assertTrue(response.closed)


class DelegatedDownloadEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.enterContext(patch.object(p, "_public_download_addresses", return_value=PUBLIC))
        self.open = self.enterContext(patch.object(p, "_open_public_download"))
        self.enterContext(patch.object(p.shutil, "which", side_effect=lambda name: name))
        self.process = self.enterContext(patch.object(p, "_run_process", side_effect=fixture.DownloadCandidateFallbackTests.fake_process))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")))

    def test_download_entry_publishes_only_fully_validated_delegated_mp4(self):
        self.open.side_effect = [fixture.feed_response([SOURCE]),
                                fixture.Response(status=302, headers={"Location": TARGET}),
                                fixture.Response(fixture.MP4)]
        result = p.download_douyin(fixture.VIDEO_URL, self.folder)
        self.assertEqual(Path(result["path"]).read_bytes(), fixture.MP4)
        self.assertEqual(self.open.call_count, 3)
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(list(self.folder.iterdir()), [Path(result["path"])])

    def test_delegated_http_401_or_429_does_not_try_backup(self):
        for status in (401, 429):
            with self.subTest(status=status):
                self.open.reset_mock()
                self.open.side_effect = [fixture.feed_response([SOURCE, fixture.URLS[1]]),
                                        fixture.Response(status=302, headers={"Location": TARGET}),
                                        fixture.Response(status=status), fixture.Response(fixture.MP4)]
                with self.assertRaises(p.ProviderError) as caught:
                    p.download_douyin(fixture.VIDEO_URL, self.folder)
                self.assertIn(str(status), str(caught.exception))
                self.assertEqual(self.open.call_count, 3)
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_delegated_invalid_content_is_not_published_or_retried(self):
        self.open.side_effect = [fixture.feed_response([SOURCE, fixture.URLS[1]]),
                                fixture.Response(status=302, headers={"Location": TARGET}),
                                fixture.Response(b"<html>not media</html>"), fixture.Response(fixture.MP4)]
        with self.assertRaises(p.ProviderError) as caught:
            p.download_douyin(fixture.VIDEO_URL, self.folder)
        self.assertEqual(caught.exception.code, "invalid_download")
        self.assertEqual(self.open.call_count, 3)
        self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
