"""Single-line task errors and product-input shortcuts; isolated DOM only."""
from pathlib import Path
import shutil
import unittest
import test_simple_mix_ui as fixture


@unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
class CompactErrorsUiTests(unittest.TestCase):
    prefix=fixture.SimpleMixUiTests.prefix
    run_node=fixture.SimpleMixUiTests.run_node

    def script(self, body):
        return self.prefix()+"\n(async()=>{await flush();\n"+body+"\n})().catch(e=>{console.error(e);process.exitCode=1;});"

    def test_all_task_areas_keep_error_in_heading_and_open_full_modal(self):
        self.run_node(self.script(r'''
const full='接口错误第一行\n'+('完整内容<script>不会当HTML执行</script>；'.repeat(60));
data.jobs=[{id:'mix-error',kind:'match',status:'failed',created_at:2,error:full},
{id:'download-error',kind:'download',status:'failed',created_at:1,error:full}];timer();await flush();
for(const area of ['mix-jobs','download-jobs']){
 const row=$(area).children[0];assert.equal(row.querySelector('details'),null);
 const detail=row.querySelector('.job-heading').querySelector('.job-inline-detail');assert.ok(detail);
 assert.ok(!detail.textContent.includes('\n'));
 event(detail,'click');await flush();assert.equal($('job-detail-dialog').open,true);
 assert.ok($('job-detail-content').textContent.includes(full));
 assert.equal(row.querySelector('details'),null);
 event($('close-job-detail'),'click');assert.equal($('job-detail-dialog').open,false);
}
assert.equal(writes().length,0);
'''))

    def test_long_completed_warning_is_clickable_without_expanding_task_row(self):
        self.run_node(self.script(r'''
const warning='请检查缓存。'.repeat(100);
data.jobs=[{id:'render',kind:'render',status:'completed',created_at:1,message:'处理完成',result:{warnings:[warning]}}];
timer();await flush();const detail=$('mix-jobs').querySelector('.job-inline-detail');assert.ok(detail);
event(detail,'click');await flush();assert.ok($('job-detail-content').textContent.includes(warning));
assert.equal($('mix-jobs').querySelector('details'),null);assert.equal(writes().length,0);
'''))

    def test_clear_product_only_edits_input_and_marks_product_dirty(self):
        self.run_node(self.script(r'''
$('select-all').checked=true;event($('select-all'),'change');assert.equal($('prepare-button').disabled,false);
$('product-url').value='https://item.jd.com/123456.html';event($('clear-product'),'click');await flush();
assert.equal($('product-url').value,'');assert.equal($('prepare-button').disabled,true);
assert.equal(document.activeElement,$('product-url'));assert.equal(writes().length,0);
assert.equal($('product-title').textContent,'测试商品标题');
'''))

    def test_paste_uses_injected_native_clipboard_without_reading_product(self):
        self.run_node(self.script(r'''
let clipboardCalls=0;window.pywebview={api:{read_clipboard:async()=>{clipboardCalls++;return 'https://item.jd.com/654321.html';}}};
$('select-all').checked=true;event($('select-all'),'change');event($('paste-product'),'click');await flush();
assert.equal(clipboardCalls,1);assert.equal($('product-url').value,'https://item.jd.com/654321.html');
assert.equal($('prepare-button').disabled,true);assert.equal(writes().length,0);
'''))

    def test_paste_browser_fallback_and_failure_preserve_existing_input(self):
        self.run_node(self.script(r'''
context.navigator={clipboard:{readText:async()=>'https://item.jd.com/765432.html'}};
event($('paste-product'),'click');await flush();assert.equal($('product-url').value,'https://item.jd.com/765432.html');
context.navigator.clipboard.readText=async()=>{throw new Error('clipboard unavailable');};
event($('paste-product'),'click');await flush();assert.equal($('product-url').value,'https://item.jd.com/765432.html');
assert.equal(writes().length,0);
'''))

    def test_late_clipboard_response_does_not_overwrite_newer_input(self):
        self.run_node(self.script(r'''
let release;window.pywebview={api:{read_clipboard:()=>new Promise(resolve=>{release=resolve;})}};
event($('paste-product'),'click');await flush();
$('product-url').value='https://item.jd.com/newer.html';event($('product-url'),'input');
release('https://item.jd.com/older.html');await flush();
assert.equal($('product-url').value,'https://item.jd.com/newer.html');assert.equal(writes().length,0);
'''))

    def test_error_css_is_ellipsized_and_dialog_can_scroll(self):
        css=(Path(__file__).resolve().parents[1]/'web/styles.css').read_text('utf8')
        self.assertIn('.job-inline-detail',css)
        rule=css.split('.job-inline-detail',1)[1].split('}',1)[0]
        for text in ('white-space:nowrap','overflow:hidden','text-overflow:ellipsis'):
            self.assertIn(text,rule)
        self.assertIn('.job-detail-content',css)


if __name__=='__main__':unittest.main()
