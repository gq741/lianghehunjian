"""Soft reference duration and modest picture slow-down; local fake DOM only."""
import shutil
import unittest

import test_simple_mix_ui as ui_fixture


@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class SoftReferenceUiTests(unittest.TestCase):
    prefix = ui_fixture.SimpleMixUiTests.prefix
    run_node = ui_fixture.SimpleMixUiTests.run_node

    def test_reference_difference_does_not_block_valid_copy(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
for(const target of [5,25,60]){
 assert.deepEqual(u.durationBounds(target),{min:.1,max:60});
 for(const units of [1,2,79,122,181,241,361,1000]){
  const d={workflow:'script_first_v2',target_seconds:target,voiceover:'中'.repeat(units)};
  const t=u.narrationTiming(d);
  assert.equal(t.ready,true);assert.equal(u.canMatchDraft(d),true);
  assert.equal(t.estimated,units/4);assert.equal(t.referenceSeconds,target);
  assert.equal(t.deviation,units/4-target);assert.equal(t.estimatedVoiceSpeed,1);
  assert.ok(t.message.includes('仅作提示'));assert.ok(t.message.includes('最长 60 秒'));
  assert.ok(!t.message.includes('删减'));assert.ok(!t.message.includes('补充'));
 }
}
for(const voiceover of ['', '  ', '，。！？'])assert.equal(u.canMatchDraft({workflow:'script_first_v2',target_seconds:25,voiceover}),false);
for(const target_seconds of [4,61,25.5,NaN])assert.equal(u.canMatchDraft({workflow:'script_first_v2',target_seconds,voiceover:'有口播'}),false);
console.log('SOFT_REFERENCE_HELPERS=PASS; REAL_API_CALLS=0');
""")

    def test_saved_copy_continues_but_unsaved_changes_and_missing_plan_still_gate(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
const draft={id:'soft',workflow:'script_first_v2',target_seconds:25,voiceover:'短口播',
 match_status:'pending',segments:[{segment_id:'s1',text:'短口播'}],clips:[],matches:[]};
data.drafts=[draft];timer();await flush();
assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);
assert.ok($('draft-timing-status').textContent.includes('仅作提示'));
assert.equal($('draft-save-status').textContent,'文案已保存');
data.drafts[0]={...draft,voiceover:'中'.repeat(500)};timer();await flush();
assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);
const editor=document.getElementById('draft-voiceover');editor.value='更短';event(editor,'input');
assert.equal($('save-draft').disabled,false);assert.equal($('match-draft').disabled,true);
assert.equal($('render-draft').disabled,true);assert.equal(writes().length,0);
$('target-seconds').value='60';event($('target-seconds'),'input');
assert.match($('duration-hint').textContent,/参考时长.*实测配音.*最长 60 秒/);
assert.ok(!$('duration-hint').textContent.includes('55～60'));
console.log('SOFT_REFERENCE_DOM=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_picture_slowdown_is_not_reported_as_voice_speed(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();const text=n=>n.textContent+n.children.map(text).join('');
const plan={version:1,status:'ready',phase:'estimated',speed:1,video_speed:.85,repeat_count:0,
 total_seconds:10,transition_seconds:0,shortages:[],segments:[],
 transition_adjustment:{reason:'transition_overlap_shortage'}};
data.drafts=[{id:'slow',workflow:'script_first_v2',target_seconds:25,voiceover:'中文口播',
 match_status:'ready',timeline_plan:plan,segments:[{segment_id:'s1',text:'中文口播'}],clips:[],matches:[]}];
timer();await flush();assert.ok(text($('timeline-summary')).includes('部分镜头已适度慢放以覆盖口播'));
assert.ok(text($('timeline-summary')).includes('预计配音倍速 1.00×'));
assert.ok(text($('timeline-summary')).includes('当前计划已自动改为直切'));
assert.equal($('render-draft').disabled,false);
for(const video_speed of [1,undefined,0,-.5,NaN]){
 data.drafts[0].timeline_plan={...plan,video_speed};timer();await flush();
 assert.ok(!text($('timeline-summary')).includes('部分镜头已适度慢放'));
}
data.drafts[0].timeline_plan=plan;timer();await flush();
const editor=document.getElementById('draft-voiceover');editor.value+='改';event(editor,'input');
assert.ok(!text($('timeline-summary')).includes('部分镜头已适度慢放'));
assert.equal(writes().length,0);console.log('PICTURE_SLOWDOWN_HINT=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == '__main__':
    unittest.main()
