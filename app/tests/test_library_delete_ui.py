"""Real frontend event handlers with a fake DOM/API; no native user deletion."""
import unittest
import test_simple_mix_ui as fixture


class LibraryDeleteUiTests(unittest.TestCase):
    prefix=fixture.SimpleMixUiTests.prefix
    run_node=fixture.SimpleMixUiTests.run_node

    def test_empty_selection_disabled_and_cancel_never_submits(self):
        self.run_node(self.prefix()+r'''
(async()=>{await flush();assert.equal($('delete-selected-assets').disabled,true);
$('select-all').checked=true;event($('select-all'),'change');
assert.equal($('delete-selected-assets').disabled,false);
event($('delete-selected-assets'),'click');await flush();
assert.equal($('confirm-dialog').open,true);assert.equal($('confirm-accept').textContent,'确认');
assert.ok($('confirm-description').textContent.includes('移到回收站'));
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
assert.equal($('selection-count').textContent,'已选 2 个视频');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_confirm_sends_only_checked_current_library_ids_once(self):
        self.run_node(self.prefix()+r'''
(async()=>{await flush();$('select-all').checked=true;event($('select-all'),'change');
event($('library-tab-douyin'),'click');await flush();
event($('delete-selected-assets'),'click');await flush();
event($('delete-selected-assets'),'click');await flush();
event($('confirm-accept'),'click');await flush();
const changes=writes().filter(c=>c.path==='/api/library/remove');assert.equal(changes.length,1);
assert.deepEqual(JSON.parse(JSON.stringify(changes[0].body)),{asset_ids:['duplicate'],library:'douyin',confirmed:true});
assert.ok(!changes[0].body.asset_ids.includes('other'));assert.ok(!changes[0].body.asset_ids.includes('remote'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_success_prunes_selection_count_and_cards(self):
        prefix=self.prefix().replace("if(path==='/api/product/read'", "if(path==='/api/library/remove'){for(const a of data.assets){if(body.asset_ids.includes(a.id)){a.availability='trashed';a.selectable=false;}}result={removed_ids:body.asset_ids,removed_count:body.asset_ids.length,failed:[],warnings:[]};}if(path==='/api/product/read'")
        self.run_node(prefix+r'''
(async()=>{await flush();$('select-all').checked=true;event($('select-all'),'change');
event($('delete-selected-assets'),'click');await flush();event($('confirm-accept'),'click');await flush();
assert.equal($('selection-count').textContent,'已选 0 个视频');
assert.equal($('delete-selected-assets').disabled,true);assert.equal($('asset-grid').children.length,0);
assert.ok(data.assets.find(a=>a.id==='remote').selectable!==false);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_button_follows_sort_text(self):
        html=(fixture.WEB/'index.html').read_text('utf-8')
        self.assertLess(html.index('按导入时间 · 最新在前'),html.index('id="delete-selected-assets"'))
        self.assertIn('一键删除',html)


if __name__=='__main__':unittest.main()
