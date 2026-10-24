"""Speed-aware copy readiness: Node helpers and production handlers, no live API."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class SpeedTimingUiTests(unittest.TestCase):
    def run_node(self, code):
        result = subprocess.run(
            ["node", "-e", code, str(ROOT / "web/app.js"), str(ROOT / "web/index.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def prefix(self):
        tree = ast.parse((ROOT / "tests/test_frontend.py").read_text("utf-8"))
        harness = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "harness" for t in n.targets))
        return harness.split("(async()=>{", 1)[0]

    def test_estimated_duration_is_reference_only_for_valid_copy(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
const d={workflow:'script_first_v2',target_seconds:25,timing_status:'needs_adjustment',estimated_seconds:5};
for(const units of [1,79,80,120,122,180,181,500]){
 const t=u.narrationTiming({...d,voiceover:'中'.repeat(units)});
 assert.equal(t.ready,true,units);assert.equal(t.mode,'reference_only',units);
 assert.equal(t.minUnits,1);assert.equal(t.maxUnits,240);assert.equal(t.maxAdaptableUnits,360);
 assert.equal(t.estimatedVoiceSpeed,1);
}
const t=u.narrationTiming({...d,voiceover:'中'.repeat(122)});
assert.equal(t.estimated,30.5);assert.equal(t.deviation,5.5);
assert.ok(t.message.includes('仅作提示'));assert.ok(t.message.includes('可继续匹配'));
assert.ok(!t.message.includes('删减'));assert.ok(!t.message.includes('待调整'));
for(const units of [1,219,220,240,241,360,361])assert.equal(u.canMatchDraft({...d,target_seconds:60,voiceover:'中'.repeat(units)}),true,units);
assert.equal(u.narrationTiming({...d,target_seconds:5,voiceover:'中'.repeat(60)}).ready,true);
assert.equal(u.narrationTiming({...d,target_seconds:5,voiceover:'中'.repeat(61)}).ready,true);
assert.equal(u.narrationTiming({...d,target_seconds:NaN,voiceover:'中'.repeat(100)}).ready,false);
""")

    def test_saved_122_units_unlock_match_not_render_or_paid_calls(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
const voice='中'.repeat(122);
data.drafts=[{id:'speed',workflow:'script_first_v2',title:'显示器口播',voiceover:voice,target_seconds:25,
 estimated_seconds:30.5,timing_status:'needs_adjustment',timing_message:'请至少删减 2 字后保存。',match_status:'pending',
 segments:[{segment_id:'s1',text:voice}],clips:[],matches:[]}];
data.jobs=[{id:'old-job',kind:'script',status:'completed',created_at:1,message:'文案已保留，待调整时长。请删减 2 字。',
 result:{draft_ids:['speed'],needs_adjustment_ids:['speed']}}];
timer();await flush();
assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);assert.equal($('save-draft').disabled,true);
assert.ok($('draft-timing-status').textContent.includes('仅作提示'));assert.ok($('draft-timing-status').textContent.includes('可继续匹配'));
assert.ok(!$('draft-timing-status').className.includes('needs-adjustment'));
assert.ok(!$('draft-select').children[0].textContent.includes('待调整'));
assert.equal($('draft-save-status').textContent,'文案已保存');
const text=n=>n.textContent+n.children.map(text).join('');
assert.equal($('mix-jobs').querySelector('.job-state').textContent,'已完成');
assert.ok(!text($('mix-jobs')).includes('删减 2'));assert.ok(!text($('mix-jobs')).includes('待调整时长'));
assert.equal(writes().length,0);
event($('match-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);assert.equal(writes().length,0);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
const editor=document.getElementById('draft-voiceover');editor.value='中'.repeat(181);event(editor,'input');
assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);assert.equal($('save-draft').disabled,false);
assert.ok($('draft-timing-status').textContent.includes('45.25'));assert.ok(!$('draft-timing-status').textContent.includes('删减'));
console.log('SPEED_TIMING_DOM=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_ready_plan_and_dirty_text_remain_separate_gates(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.drafts=[{id:'planned',workflow:'script_first_v2',voiceover:'中'.repeat(122),target_seconds:25,match_status:'ready',
 segments:[{segment_id:'s1',text:'中'.repeat(122)}],clips:[],matches:[],
 timeline_plan:{version:1,status:'ready',phase:'estimated',speed:30.5/30,total_seconds:30,repeat_count:0,segments:[],shortages:[]}}];
timer();await flush();assert.equal($('render-draft').disabled,false);
const editor=document.getElementById('draft-voiceover');editor.value='中'.repeat(123);event(editor,'input');
assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);
assert.equal(writes().length,0);
console.log('SPEED_TIMING_PLAN_GATE=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
