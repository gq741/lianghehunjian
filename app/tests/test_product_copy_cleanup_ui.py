"""Requested copy cleanup: static markup and offline fake DOM/API only."""
from html.parser import HTMLParser
import re
import shutil
import unittest

import test_simple_mix_ui as ui_fixture

WEB = ui_fixture.WEB


class Elements(HTMLParser):
    """Tiny structural reader so tests distinguish hidden labels from headings."""

    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "text": ""}
        if self.stack:
            self.stack[-1]["children"].append(node)
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr", "use"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        for node in self.stack:
            node["text"] += value

    def by_id(self, identifier):
        return next(node for node in self.nodes if node["attrs"].get("id") == identifier)


class ProductCopyCleanupMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text("utf-8")
        self.js = (WEB / "app.js").read_text("utf-8")
        self.css = (WEB / "styles.css").read_text("utf-8")
        self.elements = Elements(self.html)

    def test_download_explanation_and_description_reference_are_removed(self):
        self.assertNotIn("下载完成自动入库，最新在前。", self.html)
        self.assertNotIn("download-hint", self.html)
        self.assertNotIn("download-hint", self.js)
        self.assertNotIn("download-hint", self.elements.by_id("download-link")["attrs"].get("aria-describedby", "").split())
        self.assertEqual(self.elements.by_id("queue-download")["attrs"]["type"], "submit")
        self.assertEqual(self.elements.by_id("clear-download")["attrs"]["type"], "button")

    def test_product_heading_is_one_large_line_and_input_keeps_accessible_label(self):
        headings = [node for node in self.elements.nodes if node["tag"] == "h2" and node["text"] == "商品信息/请填入京东商品链接"]
        self.assertEqual(len(headings), 1)
        self.assertEqual(headings[0]["children"], [])
        self.assertIn("product-heading", headings[0]["attrs"].get("class", "").split())
        self.assertRegex(self.css, r"\.product-heading\s*\{[^}]*font-size:\s*22px")
        labels = [node for node in self.elements.nodes if node["tag"] == "label" and node["attrs"].get("for") == "product-url"]
        self.assertEqual(len(labels), 1)
        spans = [node for node in labels[0]["children"] if node["tag"] == "span"]
        self.assertEqual([(node["text"], node["attrs"].get("class")) for node in spans], [("京东商品链接", "sr-only")])
        self.assertIn("required", self.elements.by_id("product-url")["attrs"])
        self.assertRegex(self.css, r"\.sr-only\s*\{[^}]*position:\s*absolute")

    def test_product_card_contains_only_the_dynamic_title(self):
        card = self.elements.by_id("product-card")
        self.assertEqual([(node["tag"], node["attrs"].get("id")) for node in card["children"]], [("strong", "product-title")])
        self.assertEqual(card["text"].strip(), "")
        self.assertNotIn("product-meta", self.html)
        self.assertNotIn("product-meta", self.js)
        self.assertNotIn("商品已读取。请核对标题和具体规格，再生成文案。", self.js)
        self.assertNotIn("京东页面实际读取", self.js)

    def test_read_status_starts_empty_hidden_and_keeps_status_role(self):
        status = self.elements.by_id("product-read-status")
        self.assertIn("hidden", status["attrs"])
        self.assertEqual(status["attrs"].get("role"), "status")
        self.assertEqual(status["text"], "")
        self.assertEqual(status["children"], [])


@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class ProductCopyCleanupEventTests(unittest.TestCase):
    prefix = ui_fixture.SimpleMixUiTests.prefix
    run_node = ui_fixture.SimpleMixUiTests.run_node

    def test_success_keeps_each_real_title_exactly_and_hides_success_hint(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
const titles=['米家小米扫地机器人3C增强版智能家用大吸力吸尘器激光导航全自动扫拖一体机 擦地拖地 【新品旗舰】小米扫拖机器人4 全新正品 现货速发',
 '另一款商品 <保留原字> & 规格 A24 2026款'];
for(const [index,title] of titles.entries()){
 data.product={title,sku:String(100+index),source:'https://item.jd.com/'+(100+index)+'.html',status:'read'};
 data.jobs=[{id:'read-'+index,kind:'product',status:'completed',created_at:200+index}];
 timer();await flush();assert.equal($('product-title').textContent,title);
 assert.equal($('product-card').hidden,false);assert.equal($('product-read-status').textContent,'');
 assert.equal($('product-read-status').hidden,true);assert.equal($('product-title').children.length,0);
}
assert.equal(writes().length,0);console.log('DYNAMIC_TITLE_ONLY=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_idle_and_pending_preserve_useful_progress_without_extra_requests(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();data.product=null;data.jobs=[];timer();await flush();
assert.equal($('product-card').hidden,true);assert.equal($('product-title').textContent,'');
assert.equal($('product-read-status').hidden,true);assert.equal($('product-read-status').textContent,'');
for(const status of ['queued','running','cancelling']){
 data.jobs=[{id:'pending-'+status,kind:'product',status,created_at:300,message:'商品读取进度：'+status}];
 timer();await flush();assert.equal($('product-read-status').hidden,false);
 assert.equal($('product-read-status').textContent,'商品读取进度：'+status);
}
delete data.jobs[0].message;timer();await flush();
assert.equal($('product-read-status').hidden,false);assert.match($('product-read-status').textContent,/正在打开商品页面并读取标题/);
assert.equal(writes().length,0);console.log('IDLE_HIDDEN_PENDING_VISIBLE=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_failure_message_survives_until_success_then_clears(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
const fail={id:'failed-read',kind:'product',status:'failed',created_at:400,error:'读取失败，请在商品窗口完成登录后重试。'};
data.jobs=[fail];timer();await flush();assert.equal($('product-read-status').hidden,false);
assert.equal($('product-read-status').textContent,fail.error);
data.jobs.unshift({id:'new-read',kind:'product',status:'running',created_at:500,message:'正在读取新商品'});
timer();await flush();assert.equal($('product-read-status').hidden,false);
assert.equal($('product-read-status').textContent,'正在读取新商品');
data.jobs[0].status='completed';delete data.jobs[0].message;
data.product={title:'成功后的真实商品名',sku:'200',source:'https://item.jd.com/200.html',status:'read'};
timer();await flush();assert.equal($('product-title').textContent,'成功后的真实商品名');
assert.equal($('product-card').hidden,false);assert.equal($('product-read-status').hidden,true);
assert.equal($('product-read-status').textContent,'');
assert.equal(writes().length,0);console.log('ERROR_PENDING_SUCCESS_TRANSITIONS=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
