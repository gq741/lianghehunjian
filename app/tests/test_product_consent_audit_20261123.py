from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import test_auto_timeline_ui as ui
import test_six_paid_integrity as fixture
from mixstudio import v2


class ConfirmedProductTests(unittest.TestCase):
    setUp = fixture.GenerationDedupTests.setUp

    def test_confirmed_title_cannot_turn_into_another_product_or_edition(self):
        for sku, title in [('234567', self.product['title']), (self.product['sku'], '另一款洗衣机')]:
            with self.subTest(sku=sku, title=title), self.assertRaisesRegex(ValueError, '商品已改变'):
                self.app.generate_v2({**self.payload, 'expected_product': {'sku':sku,'title':title}})
        self.assertEqual([], self.jobs)

    def test_same_product_is_submitted_once(self):
        expected={k:self.product[k] for k in ('sku','title')}
        for _ in range(2):
            self.app.generate_v2({**self.payload, 'expected_product':expected})
        self.assertEqual(1,len(self.jobs))

    @unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
    def test_product_switch_during_consent_does_not_submit(self):
        harness=ui.AutoTimelineUiTests()
        with patch.object(ui, 'WEB', Path(v2.__file__).resolve().parents[1]/'web'):
            harness.run_node(harness.prefix()+r"""
(async()=>{await flush();
const box=$('asset-grid').children[0].querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
event($('prepare-form'),'submit');await flush();assert.equal($('confirm-dialog').open,true);
data.product={...data.product,title:'另一件商品',sku:'234567',source:'https://item.jd.com/234567.html'};timer();await flush();
event($('confirm-accept'),'click');await flush();
assert.equal(writes().filter(x=>x.path==='/api/v2/generate').length,0);
event($('prepare-form'),'submit');await flush();event($('confirm-accept'),'click');await flush();
assert.deepEqual(writes().at(-1).body.expected_product,{sku:'234567',title:'另一件商品'});
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
