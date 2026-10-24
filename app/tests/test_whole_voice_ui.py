"""Default desktop production without estimated-match prerequisites."""
import unittest
import shutil
import test_simple_mix_ui as fixture


@unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
class WholeVoiceUiTests(unittest.TestCase):
    prefix=fixture.SimpleMixUiTests.prefix
    run_node=fixture.SimpleMixUiTests.run_node

    def test_confirmed_copy_can_produce_without_matching_and_has_combined_consent(self):
        self.run_node(self.prefix()+r'''
(async()=>{await flush();
data.drafts=[{id:'whole',workflow:'script_first_v2',production_mode:'whole_voice_v1',
target_seconds:25,voiceover:'看看商品的外观。',segments:[{segment_id:'s1',text:'看看商品的外观。'}],
match_status:'pending',clips:[],matches:[]}];timer();await flush();
assert.equal($('render-draft').disabled,false);
assert.equal($('match-draft').hidden,true);
event($('render-draft'),'click');await flush();
assert.equal($('confirm-dialog').open,true);
for(const name of ['免费预检','Fish Audio','GLM','DeepSeek','一整条'])assert.ok($('confirm-description').textContent.includes(name));
assert.equal(writes().length,0);
event($('confirm-accept'),'click');await flush();
assert.equal(writes()[0].path,'/api/v2/drafts/whole/produce');assert.equal(writes()[0].body.allow_paid,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_preflight_is_first_visible_step_and_blocks_generation_while_running(self):
        self.run_node(self.prefix()+r'''
(async()=>{await flush();$('select-all').checked=true;event($('select-all'),'change');
data.jobs=[{id:'pre',kind:'preflight',status:'running',created_at:1,progress:.1,message:'检查本地模型'}];
timer();await flush();assert.equal($('prepare-button').disabled,true);
assert.ok($('preflight-status').textContent.includes('检查本地模型'));
assert.ok($('mix-jobs').querySelector('.job-name').textContent.includes('免费预检'));
assert.equal(writes().length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_dirty_copy_still_requires_save_no_automatic_submission(self):
        self.run_node(self.prefix()+r'''
(async()=>{await flush();data.drafts=[{id:'w',workflow:'script_first_v2',desktop_production_mode:'whole_voice_v1',
target_seconds:25,voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[]}];
timer();await flush();assert.equal($('render-draft').disabled,false);
const text=$('draft-segments').querySelector('textarea');text.value='看看细节。';event(text,'input');
assert.equal($('render-draft').disabled,true);assert.equal(writes().length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__=='__main__':unittest.main()
