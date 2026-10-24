"""Read a JD title from an isolated visible product page, never from an LLM."""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit


def normalize_jd_link(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("请粘贴有效的京东商品链接")
    # Chinese share copy commonly joins the URL directly to “点击链接”.
    # These characters delimit copy, not an ASCII JD host/path. Still validate
    # the entire extracted URL below, rather than accepting a known prefix.
    urls = re.findall(r'''https?://[^\s<>"'\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+''', value)
    if len(urls) != 1:
        raise ValueError("一次读取一个京东商品链接")
    url = urls[0].rstrip('，。；！）)]}')
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        host = parsed.hostname
        if host == 'item.jd.com':
            match = re.fullmatch(r'/(\d{5,20})\.html', parsed.path)
        elif host == 'item.m.jd.com':
            match = re.fullmatch(r'/product/(\d{5,20})\.html', parsed.path)
        else:
            match = None
        if match:
            sku = match.group(1)
            return {'sku': sku, 'source': f'https://item.jd.com/{sku}.html'}
        if host in {'3.cn', 'u.jd.com'} and re.fullmatch(r'/[A-Za-z0-9_/-]{2,160}', parsed.path):
            return {'sku': None, 'source': url}
    except (ValueError, TypeError):
        pass
    raise ValueError("请使用京东商品详情链接或京东分享短链接")


def validate_product(value, expected_sku=None):
    if not isinstance(value, dict):
        raise ValueError("商品信息读取结果无效，请在商品窗口确认页面")
    normalized = normalize_jd_link(value.get('source'))
    sku, title = value.get('sku'), value.get('title')
    if not normalized['sku'] or sku != normalized['sku'] or (expected_sku and sku != expected_sku):
        raise ValueError("商品页面已跳转到其他型号，请重新核对链接")
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 300 or any(ord(c) < 32 for c in title):
        raise ValueError("未读取到有效商品标题，请在京东窗口完成加载或登录")
    if re.search(r'安全验证|验证码|京东登录|登录京东|访问受限|页面不存在|商品已下柜', title):
        raise ValueError("当前不是有效商品详情页，请在京东窗口确认")
    observed = value.get('observed_at')
    if type(observed) not in (int, float) or not math.isfinite(observed) or observed <= 0:
        observed = time.time()
    return {'sku': sku, 'title': title.strip(), 'source': normalized['source'],
            'observed_at': observed, 'status': 'read'}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate product data field')
        result[key] = value
    return result


def _json_object(text, start):
    """Extract a brace-balanced object; tolerate only JS trailing commas."""
    depth, quoted, escaped, cleaned = 0, False, False, []
    for i in range(start, len(text)):
        char = text[i]
        if quoted:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '{[':
            depth += 1
        elif char in '}]':
            depth -= 1
        elif char == ',':
            look = i + 1
            while look < len(text) and text[look].isspace():
                look += 1
            if look < len(text) and text[look] in '}]':
                continue
        cleaned.append(char)
        if depth == 0:
            return ''.join(cleaned), i + 1
    raise ValueError('Incomplete product data')


def parse_public_product(html, expected_sku):
    """Only the selected SKU's full structured title, never a recommendation."""
    if not isinstance(html, str) or not re.fullmatch(r'\d{5,20}', str(expected_sku)):
        return None
    for match in re.finditer(r'\bwindow\._itemInfo\s*=\s*\(?\s*(?=\{)', html):
        try:
            encoded, end = _json_object(html, match.end())
            data = json.loads(encoded, object_pairs_hook=_unique_pairs)
            if not re.match(r'\s*\)?\s*;', html[end:end + 20]):
                continue
            if not isinstance(data, dict) or str(data.get('errCode', '0')) != '0':
                continue
            item = data.get('product')
            if not isinstance(item, dict) or str(item.get('skuId', '')) != expected_sku:
                continue
            title = item.get('skuName')
            if (not isinstance(title, str) or re.search(r'\.\.\.|…|正品低价|品质保障|轻松购物|京东商城首页', title)
                    or title.strip().lower() in {'京东', '京东商城', 'jd.com'}):
                continue
            return validate_product({'sku': expected_sku, 'title': title,
                'source': f'https://item.jd.com/{expected_sku}.html'}, expected_sku)
        except (ValueError, TypeError, RecursionError):
            continue
    return None


class _NoProductRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_public_product(link, cancel):
    """One credential-free request to a fixed official JD mobile endpoint."""
    sku = link.get('sku')
    if not isinstance(sku, str) or not re.fullmatch(r'\d{5,20}', sku) or cancel.is_set():
        return None
    url = f'https://item.m.jd.com/product/{sku}.html'
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
        'Accept': 'text/html', 'Accept-Encoding': 'identity',
    })
    opener = urllib.request.build_opener(_NoProductRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if response.status != 200 or response.geturl() != url:
                return None
            limit, data = 2 * 1024 * 1024, bytearray()
            size = response.headers.get('Content-Length')
            if size and (not size.isdigit() or int(size) > limit):
                return None
            while not cancel.is_set():
                chunk = response.read(min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    return None
            if cancel.is_set():
                return None
            encoding = response.headers.get_content_charset() or 'utf-8'
            return parse_public_product(bytes(data).decode(encoding), sku)
    except Exception:
        # Public title unavailable is not a verified title; use the visible reader.
        return None


def fetch_public_product(link, *, cancel, timeout=6.0):
    """End-to-end deadline, including DNS/read/parse; one daemon worker only."""
    from .jobs import Cancelled
    if cancel.is_set():
        raise Cancelled('商品读取已取消')
    if not link.get('sku'):
        return None
    stop, done, box = threading.Event(), threading.Event(), {}
    class Cancellation:
        def is_set(self):
            return stop.is_set() or cancel.is_set()
    def work():
        try:
            box['result'] = _read_public_product(link, Cancellation())
        except Exception:
            box['result'] = None
        finally:
            done.set()
    deadline = time.monotonic() + max(0, min(float(timeout), 6.0))
    threading.Thread(target=work, daemon=True).start()
    while True:
        if cancel.is_set():
            stop.set()
            raise Cancelled('商品读取已取消')
        if done.is_set():
            return box.get('result')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            return None
        done.wait(min(.05, remaining))


READ_TITLE_JS = r"""(() => {
  const host=location.hostname, path=location.pathname;
  const hit=host==='item.jd.com' ? /^\/(\d{5,20})\.html$/.exec(path) :
    host==='item.m.jd.com' ? /^\/product\/(\d{5,20})\.html$/.exec(path) : null;
  if (!hit) return {ready:false};
  const visible=n=>!!n && !!n.getClientRects().length && getComputedStyle(n).visibility!=='hidden';
  const text=n=>(n?.innerText||'').replace(/\s+/g,' ').trim();
  if (/请完成验证|滑块验证|安全验证|访问受限/.test((document.body?.innerText||'').slice(0,20000))) return {ready:false};
  const node=[...document.querySelectorAll('.sku-name,.itemInfo-wrap h1,h1.sku-title,.product-intro h1,.prod_name,.fn')].find(n=>visible(n)&&text(n));
  let title=text(node);
  if (!title) title=document.querySelector('meta[property="og:title"]')?.content?.trim()||'';
  if (!title || /京东登录|登录京东|安全验证/.test(title)) return {ready:false};
  const selected=document.querySelector('#choose-btns [data-sku]')?.getAttribute('data-sku');
  if (selected && selected!==hit[1]) return {ready:false};
  return {ready:true,sku:hit[1],title:title.slice(0,300),source:'https://item.jd.com/'+hit[1]+'.html'};
})()"""


class JDWindowReader:
    """No Python bridge is exposed to the remote JD window. No cookie export."""
    def __init__(self):
        self._lock = threading.Lock()
        self._window = None

    def __call__(self, link, *, cancel, progress):
        import webview
        if not self._lock.acquire(blocking=False):
            raise ValueError("已有京东商品窗口正在读取，请完成或取消后再试")
        try:
            self.close()
            progress(.05, '正在快速读取京东商品标题，不加载完整商品页面')
            result = fetch_public_product(link, cancel=cancel)
            if result:
                if cancel.is_set():
                    from .jobs import Cancelled
                    raise Cancelled('商品读取已取消')
                progress(1, '商品标题已读取（公开页面快速读取）')
                return result
            closed = threading.Event()
            window = webview.create_window('京东商品读取 · 请在此窗口完成登录或验证',
                link['source'], js_api=None, width=1100, height=820, text_select=True)
            self._window = window
            window.events.closed += closed.set
            deadline = time.monotonic() + 180
            progress(.1, '快速读取未取得标题，已打开京东窗口；请完成加载、登录或验证（最多等待180秒，可取消）')
            while time.monotonic() < deadline:
                if cancel.wait(.4):
                    from .jobs import Cancelled
                    self.close()
                    raise Cancelled('商品读取已取消')
                if closed.is_set():
                    raise ValueError('商品窗口已关闭，未读取到标题')
                if not window.events.loaded.wait(.1):
                    continue
                # WebView2's ExecuteScriptAsync wrapper can block indefinitely
                # during navigation. Keep one daemon query per window and wait
                # cancellably; never let it hold the job pool during shutdown.
                completed, box = threading.Event(), {}
                def query():
                    try:
                        box['data'] = window.evaluate_js(READ_TITLE_JS)
                    except Exception:
                        box['navigation_error'] = True
                    finally:
                        completed.set()
                threading.Thread(target=query, daemon=True).start()
                query_deadline = min(deadline, time.monotonic() + 10)
                while not completed.wait(.1):
                    if cancel.is_set():
                        from .jobs import Cancelled
                        self.close()
                        raise Cancelled('商品读取已取消')
                    if closed.is_set() or time.monotonic() >= query_deadline:
                        self.close()
                        raise ValueError('商品窗口查询已中断或超时，请完成加载后重试')
                try:
                    data = box.get('data')
                    if isinstance(data, dict) and data.get('ready'):
                        result = validate_product(data, link['sku'])
                        progress(1, '商品标题已读取')
                        return result
                except ValueError:
                    raise
                except Exception:
                    # Navigations can invalidate a document while it is queried.
                    continue
            raise ValueError('商品页读取超时，请完成登录或验证后重新读取')
        finally:
            self._lock.release()

    def close(self):
        window, self._window = self._window, None
        if window:
            def dispose():
                try:
                    window.destroy()
                except Exception:
                    pass
            thread = threading.Thread(target=dispose, daemon=True)
            thread.start()
            thread.join(.2)
