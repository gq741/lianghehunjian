"""Offline partial-analysis UI contracts; fake DOM/API only, no paid requests."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(os.environ.get("SIMPLE_MIX_UI_SOURCE", ROOT)) / "web"


@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class SimpleMixUiTests(unittest.TestCase):
    def run_node(self, code):
        result = subprocess.run(
            ["node", "-e", code, str(WEB / "app.js"), str(WEB / "index.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def prefix(self):
        tree = ast.parse((ROOT / "tests/test_frontend.py").read_text("utf-8"))
        harness = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "harness" for t in n.targets))
        return harness.split("(async()=>{", 1)[0]

    def test_partial_analysis_summary_and_resume_button_without_new_render_gate(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
data.drafts=[{id:'partial',workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),
 match_status:'ready',timeline_plan:{version:1,status:'ready',shortages:[],segments:[]},
 segments:[{segment_id:'s1',text:'好'.repeat(100)}],matches:[{segment_id:'s1',clip_ids:['c1']}],
 clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true},{clip_id:'c2',asset_id:'remote',start:3,end:6,usable:false}],
 analysis_summary:{planned:97,completed:71,pending:26,complete:false,failed_batches:[{http_status:503}]}}];
timer();await flush();
assert.match($('match-detail').textContent,/已分析 71\/97 个窗口/);
assert.match($('match-detail').textContent,/已保存的结果可用于匹配，剩余 26 个可稍后继续/);
assert.match($('match-detail').textContent,/1 个可用片段，1 个已排除或待核对/);
assert.equal($('match-draft').textContent,'继续分析并匹配');
assert.equal($('match-draft').disabled,false);
assert.equal($('render-draft').disabled,false);
assert.equal(writes().length,0);
// Completing the analysis resets wording, without adding a second workflow action.
data.drafts[0].analysis_summary={planned:97,completed:97,pending:0,complete:true,failed_batches:[]};
timer();await flush();
assert.match($('match-detail').textContent,/已分析 97\/97 个窗口/);
assert.ok(!$('match-detail').textContent.includes('可稍后继续'));
assert.equal($('match-draft').textContent,'自动匹配画面');
assert.equal($('render-draft').disabled,false);
// A partial inventory alone is not a completed timeline.
data.drafts[0].analysis_summary={planned:97,completed:71,pending:26,complete:false,failed_batches:[]};
data.drafts[0].match_status='needs_review';data.drafts[0].timeline_plan.status='shortage';
data.drafts[0].timeline_plan.shortages=[{segment_id:'s1',missing_seconds:1}];
timer();await flush();assert.equal($('render-draft').disabled,true);
console.log('PARTIAL_ANALYSIS_UI=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_pending_draft_and_legacy_summary_do_not_fake_completion(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
data.drafts=[{id:'pending',workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),
 match_status:'pending',segments:[],clips:[],matches:[],
 analysis_summary:{planned:97,completed:0,pending:97,complete:false,failed_batches:[]}}];
timer();await flush();
assert.match($('match-detail').textContent,/已分析 0\/97 个窗口/);
assert.ok(!$('match-detail').textContent.includes('已保存的结果可用于匹配'));
assert.equal($('render-draft').disabled,true);
assert.equal($('match-draft').textContent,'继续分析并匹配');
delete data.drafts[0].analysis_summary;timer();await flush();
assert.equal($('match-draft').textContent,'自动匹配画面');
assert.ok(!$('match-detail').textContent.includes('undefined'));
assert.ok(!$('match-detail').textContent.includes('NaN'));
assert.equal(writes().length,0);
console.log('LEGACY_AND_EMPTY_ANALYSIS_UI=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_match_confirmation_six_images_cache_reuse_and_no_hidden_submission(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
data.drafts=[{id:'resume',workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),
 match_status:'pending',segments:[],clips:[],matches:[],analysis_summary:{planned:97,completed:71,pending:26,complete:false}}];
timer();await flush();event($('match-draft'),'click');await flush();
assert.equal($('confirm-dialog').open,true);assert.equal(writes().length,0);
const notice=$('confirm-description').textContent;
assert.match(notice,/每批最多 2 个片段、6 张图/);
assert.match(notice,/3 秒窗口、每窗最多 3 帧/);
assert.match(notice,/仅未完成的画面分析会新调用 GLM/);
assert.match(notice,/有效缓存会复用/);
assert.match(notice,/候选池变化时，DeepSeek 匹配可能再次调用/);
assert.ok(!notice.includes('旧采样缓存不混用'));
assert.ok(!notice.includes('10 张图'));assert.ok(!notice.includes('5 帧'));
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
event($('match-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/v2/drafts/resume/match');
assert.deepEqual(writes()[0].body,{allow_paid:true});
console.log('MATCH_RESUME_CONFIRMATION=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
