"""Offline DOM tests for a ten-item newest-first copy selector."""
import unittest
import test_simple_mix_ui as simple


class DraftRetentionUiTests(unittest.TestCase):
    run_node = simple.SimpleMixUiTests.run_node
    prefix = simple.SimpleMixUiTests.prefix

    def test_selector_shows_ten_newest_not_all_fifty(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.drafts=Array.from({length:55},(_,i)=>({id:'d-'+i,created_at:i,title:'文案'+i,
workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),segments:[],clips:[],matches:[],match_status:'pending'}));
timer();await flush();
const options=$('draft-select').children;
assert.equal(options.length,10);
assert.deepEqual(options.map(o=>o.value),Array.from({length:10},(_,i)=>'d-'+(54-i)));
assert.equal($('draft-select').value,'d-54');
assert.equal(writes().length,0);
console.log('LATEST_TEN_SELECTOR=PASS; PAID_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_rollover_selects_newest_without_overwriting_dirty_copy(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
const draft=i=>({id:'d-'+i,created_at:i,title:'文案'+i,workflow:'script_first_v2',
target_seconds:25,voiceover:'好'.repeat(100),segments:[{segment_id:'s1',text:'好'.repeat(100)}],clips:[],matches:[],match_status:'pending'});
data.drafts=Array.from({length:10},(_,i)=>draft(9-i));timer();await flush();
$('draft-select').value='d-0';event($('draft-select'),'change');await flush();
const input=document.getElementById('draft-voiceover');input.value='未保存的文案';event(input,'input');
data.drafts.unshift(draft(10));timer();await flush();
assert.equal(document.getElementById('draft-voiceover').value,'未保存的文案');
assert.equal($('save-draft').disabled,false);assert.equal(writes().length,0);
event($('save-draft'),'click');await flush();timer();await flush();
assert.equal($('draft-select').children.length,10);
assert.equal($('draft-select').value,'d-10');
assert.equal(writes().length,1);assert.equal(writes()[0].body.voiceover,'未保存的文案');
console.log('DIRTY_COPY_PRESERVED=PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_empty_selector_and_history_error_message(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
assert.equal($('draft-select').children.length,0);
assert.equal($('draft-history-warning').hidden,true);
data.draft_history={warning:'旧文案暂未移入回收站，数据继续保留'};timer();await flush();
assert.equal($('draft-history-warning').hidden,false);
assert.match($('draft-history-warning').textContent,/数据继续保留/);
data.draft_history.warning='';timer();await flush();assert.equal($('draft-history-warning').hidden,true);
assert.equal(writes().length,0);console.log('HISTORY_WARNING=PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
