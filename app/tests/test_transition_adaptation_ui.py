"""Transition-only recovery explanations; offline fake DOM, zero paid calls."""
import shutil
import unittest

import test_simple_mix_ui as ui_fixture


@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class TransitionAdaptationUiTests(unittest.TestCase):
    prefix = ui_fixture.SimpleMixUiTests.prefix
    run_node = ui_fixture.SimpleMixUiTests.run_node

    def fixture(self):
        return self.prefix() + r"""
const hint='当前计划已自动改为直切，避免淡化转场重叠造成时长缺口。';
const text=n=>n.textContent+n.children.map(text).join('');
const plan={version:1,status:'ready',phase:'estimated',speed:1.5,repeat_count:2,total_seconds:23.5,
 transition_seconds:0,transition_adjustment:{reason:'transition_overlap_shortage'},shortages:[],
 segments:[{segment_id:'s1',required_seconds:6,available_seconds:6,missing_seconds:0}]};
const draft={id:'adapted',workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),
 match_status:'ready',timeline_plan:plan,segments:[{segment_id:'s1',text:'好'.repeat(100)}],
 clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true}],
 matches:[{segment_id:'s1',clip_ids:['c1']}]};
"""

    def test_current_adapted_plan_explains_hardcut_without_new_render_gate(self):
        self.run_node(self.fixture() + r"""
(async()=>{await flush();data.drafts=[draft];timer();await flush();
const hints=$('timeline-summary').querySelectorAll('.field-hint').filter(n=>n.textContent===hint);
assert.equal(hints.length,1);assert.equal($('render-draft').disabled,false);
assert.equal($('timeline-summary').querySelectorAll('button').length,0);
data.drafts[0].timeline_plan={...plan,phase:'actual'};timer();await flush();
assert.ok(text($('timeline-summary')).includes(hint));assert.equal($('render-draft').disabled,false);
assert.equal(writes().length,0);console.log('TRANSITION_ADAPTATION_UI=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_unadapted_or_nonzero_fade_plans_do_not_claim_hardcut(self):
        self.run_node(self.fixture() + r"""
(async()=>{await flush();
for(const patch of [{transition_adjustment:undefined},
 {transition_adjustment:{reason:'other_reason'}},{transition_seconds:.2},{transition_seconds:'0'}]){
 data.drafts=[{...draft,timeline_plan:{...plan,...patch}}];timer();await flush();
 assert.ok(!text($('timeline-summary')).includes(hint));assert.equal($('render-draft').disabled,false);
}
assert.equal(writes().length,0);console.log('TRANSITION_HINT_GUARDS=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_dirty_text_or_selection_hides_stale_adaptation_hint(self):
        self.run_node(self.fixture() + r"""
(async()=>{await flush();data.drafts=[draft];timer();await flush();
assert.ok(text($('timeline-summary')).includes(hint));
const editor=document.getElementById('draft-voiceover');editor.value+='新';event(editor,'input');
assert.ok(!text($('timeline-summary')).includes(hint));assert.equal($('render-draft').disabled,true);
event($('draft-select'),'change');await flush();event($('confirm-accept'),'click');await flush();
assert.ok(text($('timeline-summary')).includes(hint));
const box=$('match-rows').querySelectorAll('input[type="checkbox"]')[0];
box.checked=false;event(box,'change');
assert.ok(!text($('timeline-summary')).includes(hint));assert.equal($('render-draft').disabled,true);
assert.equal(writes().length,0);console.log('STALE_TRANSITION_HINT=HIDDEN; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == '__main__':
    unittest.main()
