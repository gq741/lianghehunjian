"""Fast JD title regression tests. Public network and native windows are mocked."""
import json
import io
from email.message import Message
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mixstudio import product
from mixstudio.jobs import Cancelled


SKU = '10144049649180'
TITLE = '测试品牌电视 55英寸 高清显示 家用客厅款'
LINK = {'sku': SKU, 'source': f'https://item.jd.com/{SKU}.html'}
PRODUCT = {**LINK, 'title': TITLE, 'observed_at': 123456, 'status': 'read'}


def html_product(title=TITLE, sku=SKU, **extra):
    payload = {'product': {'skuId': sku, 'skuName': title}, **extra}
    return '<html><head><title>截断标题...</title></head><script>window._itemInfo = (' + json.dumps(payload, ensure_ascii=False) + ');</script></html>'


class PublicProductParserTests(unittest.TestCase):
    def test_full_same_sku_title_and_canonical_source(self):
        result = product.parse_public_product(html_product(), SKU)
        self.assertEqual(result['title'], TITLE)
        self.assertEqual(result['sku'], SKU)
        self.assertEqual(result['source'], LINK['source'])
        self.assertEqual(result['status'], 'read')

    def test_quotes_unicode_parentheses_are_json_not_executable(self):
        title = '测试品牌“护眼”电视（55英寸） "高清" 家用款'
        result = product.parse_public_product(html_product(title=title), SKU)
        self.assertEqual(result['title'], title)

    def test_escaped_unicode_json_is_decoded(self):
        value = json.dumps({'product': {'skuId': SKU, 'skuName': TITLE}}, ensure_ascii=True)
        result = product.parse_public_product('<script>window._itemInfo=(' + value + ');</script>', SKU)
        self.assertEqual(result['title'], TITLE)

    def test_real_page_trailing_commas_accepted_without_eval(self):
        value = json.dumps({'product': {'skuId': SKU, 'skuName': TITLE}, 'loginEntry': {'text': '登录'}})
        html = '<script>window._itemInfo = (' + value[:-1] + ',\n});</script>'
        self.assertEqual(product.parse_public_product(html, SKU)['title'], TITLE)

    def test_trailing_comma_cleanup_preserves_string_content(self):
        title = '测试电视 ",}" 与 ",]" 转义引号 \\" 家用款'
        value = json.dumps({'product': {'skuId': SKU, 'skuName': title}, 'other': ['x,}', 'y,]']})
        html = '<script>window._itemInfo=(' + value[:-1] + ',});</script>'
        self.assertEqual(product.parse_public_product(html, SKU)['title'], title)

    def test_duplicate_keys_with_trailing_comma_still_rejected(self):
        value = '{"product":{"skuId":"' + SKU + '","skuName":"错误标题","skuName":"' + TITLE + '",},}'
        self.assertIsNone(product.parse_public_product('<script>window._itemInfo=(' + value + ');</script>', SKU))

    def test_item_only_and_document_title_are_not_accepted(self):
        html = '<title>测试商品...</title><script>window._itemOnly={"item":{"skuName":"测试商品..."}};</script>'
        self.assertIsNone(product.parse_public_product(html, SKU))

    def test_recommended_title_does_not_replace_product_title(self):
        payload = {'product': {'skuId': SKU}, 'recommendations': [{'skuId': SKU, 'skuName': TITLE}]}
        html = '<script>window._itemInfo=(' + json.dumps(payload) + ');</script>'
        self.assertIsNone(product.parse_public_product(html, SKU))

    def test_different_sku_rejected_even_when_recommendations_match(self):
        html = html_product(sku='10144049649181', recommendations=[{'skuId': SKU, 'skuName': TITLE}])
        self.assertIsNone(product.parse_public_product(html, SKU))

    def test_missing_or_invalid_product_fields_rejected(self):
        for payload in [{}, {'product': []}, {'product': {'skuId': SKU}}, {'product': {'skuId': True, 'skuName': TITLE}}]:
            with self.subTest(payload=payload):
                html = '<script>window._itemInfo=(' + json.dumps(payload) + ');</script>'
                self.assertIsNone(product.parse_public_product(html, SKU))

    def test_generic_login_verification_and_truncated_names_rejected(self):
        for title in ['', '京东', '京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物！', '京东登录', '安全验证', '商品页面不存在', '测试商品...', '测试商品…']:
            with self.subTest(title=title):
                self.assertIsNone(product.parse_public_product(html_product(title=title), SKU))

    def test_duplicate_json_keys_rejected(self):
        payloads = [
            '{"product":{"skuId":"' + SKU + '","skuName":"错误标题","skuName":"' + TITLE + '"}}',
            '{"product":{"skuId":"99999","skuId":"' + SKU + '","skuName":"' + TITLE + '"}}',
            '{"product":{},"product":{"skuId":"' + SKU + '","skuName":"' + TITLE + '"}}',
        ]
        for value in payloads:
            with self.subTest(value=value):
                self.assertIsNone(product.parse_public_product('<script>window._itemInfo=(' + value + ');</script>', SKU))

    def test_malformed_or_javascript_expression_not_executed(self):
        for body in ['{broken', 'alert("ignored")', '{product:{skuId:"' + SKU + '",skuName:"' + TITLE + '"}}']:
            with self.subTest(body=body):
                self.assertIsNone(product.parse_public_product('<script>window._itemInfo=(' + body + ');</script>', SKU))


class PublicProductDeadlineTests(unittest.TestCase):
    def test_success_returns_worker_value(self):
        with patch.object(product, '_read_public_product', return_value=PRODUCT) as read:
            self.assertEqual(product.fetch_public_product(LINK, cancel=threading.Event()), PRODUCT)
        read.assert_called_once()

    def test_network_exception_returns_none_for_browser_fallback(self):
        with patch.object(product, '_read_public_product', side_effect=OSError('offline fixture')):
            self.assertIsNone(product.fetch_public_product(LINK, cancel=threading.Event()))

    def test_total_deadline_returns_without_waiting_for_slow_worker(self):
        release = threading.Event()
        entered = threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return PRODUCT
        began = time.monotonic()
        try:
            with patch.object(product, '_read_public_product', side_effect=slow):
                result = product.fetch_public_product(LINK, cancel=threading.Event(), timeout=0.05)
            self.assertIsNone(result)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - began, 0.5)
        finally:
            release.set()

    def test_pre_cancelled_request_does_not_start_network(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(product, '_read_public_product') as read, self.assertRaises(Cancelled):
            product.fetch_public_product(LINK, cancel=cancel)
        read.assert_not_called()

    def test_cancellation_while_worker_runs_is_prompt(self):
        cancel = threading.Event()
        release = threading.Event()
        def slow(*args, **kwargs):
            cancel.set()
            release.wait(2)
            return PRODUCT
        began = time.monotonic()
        try:
            with patch.object(product, '_read_public_product', side_effect=slow), self.assertRaises(Cancelled):
                product.fetch_public_product(LINK, cancel=cancel)
            self.assertLess(time.monotonic() - began, 0.5)
        finally:
            release.set()

    def test_cancellation_wins_over_simultaneous_worker_result(self):
        cancel = threading.Event()
        def finish(*args, **kwargs):
            cancel.set()
            return PRODUCT
        with patch.object(product, '_read_public_product', side_effect=finish), self.assertRaises(Cancelled):
            product.fetch_public_product(LINK, cancel=cancel)


class ProductReaderFastPathTests(unittest.TestCase):
    def test_success_never_creates_browser(self):
        progress = []
        with patch.object(product, 'fetch_public_product', return_value=PRODUCT) as fetch, patch('webview.create_window') as create:
            value = product.JDWindowReader()(LINK, cancel=threading.Event(), progress=lambda *args: progress.append(args))
        self.assertEqual(value['title'], TITLE)
        fetch.assert_called_once()
        create.assert_not_called()
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], 1)

    def test_unavailable_fast_title_falls_back_to_isolated_window(self):
        class Event:
            def __iadd__(self, callback):
                return self
            def wait(self, timeout):
                return True
        window = SimpleNamespace(
            events=SimpleNamespace(loaded=Event(), closed=Event()),
            evaluate_js=lambda code: {**PRODUCT, 'ready': True},
            destroy=lambda: None,
        )
        with patch.object(product, 'fetch_public_product', return_value=None) as fetch, patch('webview.create_window', return_value=window) as create:
            value = product.JDWindowReader()(LINK, cancel=threading.Event(), progress=lambda *a: None)
        self.assertEqual(value['title'], TITLE)
        fetch.assert_called_once()
        create.assert_called_once()
        self.assertIsNone(create.call_args.kwargs['js_api'])

    def test_fast_path_cancel_does_not_create_browser(self):
        with patch.object(product, 'fetch_public_product', side_effect=Cancelled('cancelled fixture')), patch('webview.create_window') as create, self.assertRaises(Cancelled):
            product.JDWindowReader()(LINK, cancel=threading.Event(), progress=lambda *a: None)
        create.assert_not_called()


class PublicProductHttpTests(unittest.TestCase):
    def response(self, data=None, *, status=200, url=None, size=None):
        data = html_product().encode('utf-8') if data is None else data
        body = io.BytesIO(data)
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'
        if size is not None:
            headers['Content-Length'] = str(size)
        response = Mock(status=status, headers=headers)
        response.geturl.return_value = url or f'https://item.m.jd.com/product/{SKU}.html'
        response.read.side_effect = body.read
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_fixed_mobile_endpoint_no_cookie_no_redirect_timeout_five(self):
        response = self.response()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(product.urllib.request, 'build_opener', return_value=opener) as build:
            result = product._read_public_product({**LINK, 'source': 'https://untrusted.test/ignored'}, threading.Event())
        self.assertEqual(result['title'], TITLE)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f'https://item.m.jd.com/product/{SKU}.html')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 5)
        self.assertNotIn('cookie', {name.lower() for name, _ in request.header_items()})
        self.assertNotIn('authorization', {name.lower() for name, _ in request.header_items()})
        redirect_handler = build.call_args.args[0]
        self.assertIsNone(redirect_handler.redirect_request(request, None, 302, '', {}, 'https://other.test/'))

    def test_http_failure_or_changed_final_url_rejected(self):
        for response in [self.response(status=403), self.response(url='https://passport.jd.com/new/login.aspx')]:
            with self.subTest(status=response.status), patch.object(product.urllib.request, 'build_opener', return_value=Mock(open=Mock(return_value=response))):
                self.assertIsNone(product._read_public_product(LINK, threading.Event()))
            response.read.assert_not_called()

    def test_oversized_declared_body_rejected_before_read(self):
        response = self.response(size=2 * 1024 * 1024 + 1)
        with patch.object(product.urllib.request, 'build_opener', return_value=Mock(open=Mock(return_value=response))):
            self.assertIsNone(product._read_public_product(LINK, threading.Event()))
        response.read.assert_not_called()

    def test_oversized_undeclared_body_is_bounded(self):
        response = self.response(b'x' * (2 * 1024 * 1024 + 50000))
        with patch.object(product.urllib.request, 'build_opener', return_value=Mock(open=Mock(return_value=response))):
            self.assertIsNone(product._read_public_product(LINK, threading.Event()))
        self.assertLessEqual(sum(call.args[0] for call in response.read.call_args_list), 2 * 1024 * 1024 + 1)

    def test_cancellation_during_stream_read_discards_result(self):
        response = self.response()
        cancel = threading.Event()
        def first_chunk(count):
            cancel.set()
            return html_product().encode('utf-8')
        response.read.side_effect = first_chunk
        with patch.object(product.urllib.request, 'build_opener', return_value=Mock(open=Mock(return_value=response))):
            self.assertIsNone(product._read_public_product(LINK, cancel))

    def test_missing_sku_or_pre_cancelled_does_not_request_network(self):
        cancel = threading.Event()
        cancel.set()
        for link, event in [({'sku': None, 'source': 'https://3.cn/abc123'}, threading.Event()), (LINK, cancel)]:
            with self.subTest(link=link), patch.object(product.urllib.request, 'build_opener') as build:
                self.assertIsNone(product._read_public_product(link, event))
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
