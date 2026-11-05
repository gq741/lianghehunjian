import unittest
import shutil
import test_auto_timeline_ui as ui


@unittest.skipUnless(shutil.which('node'), 'Node required')
class TimelineDisplayTests(unittest.TestCase):
    def test_voice_duration_is_not_labelled_as_available_footage(self):
        harness=ui.AutoTimelineUiTests()
        harness.run_node(harness.prefix()+r'''
(async()=>{await flush();
data.drafts=[{id:'shortage',workflow:'script_first_v2',production_mode:'whole_voice_v1',voiceover:'商品'.repeat(50),target_seconds:30,
segments:[{segment_id:'s1',text:'第一句'},{segment_id:'s2',text:'第二句'},{segment_id:'s3',text:'第三句'}],
whole_timeline_plan:{version:1,status:'shortage',phase:'actual',speed:1,repeat_count:2,total_seconds:32.03,covered_seconds:28.24,
visual_groups:[{segment_id:'s1',sentence_ids:['s1','s2']},{segment_id:'s3',sentence_ids:['s3']}],
segments:[{segment_id:'s1',required_seconds:27.34,available_seconds:27.34},{segment_id:'s3',required_seconds:4.69,available_seconds:.90}],
shortages:[{segment_id:'s3',missing_seconds:3.79,reason:'相关候选不足'}]}}];
timer();await flush();const text=n=>n.textContent+n.children.map(text).join('');const t=text($('timeline-summary'));
assert.ok(t.includes('口播 32.03 秒'));assert.ok(t.includes('已配净画面 28.24 秒'));
assert.ok(!t.includes('净画面 32.03 秒'));assert.ok(t.includes('文案组 2：相关候选不足'));
console.log('TIMELINE_DISPLAY_PASS; MOCK_API_ONLY');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
