"""No copy-review gate: offline production JS helpers and mocked DOM/API events."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


@unittest.skipUnless(shutil.which("node"), "Node is required for offline UI tests")
class ClaimUiTests(unittest.TestCase):
    def run_node(self, code):
        result = subprocess.run(["node", "-e", code, str(WEB / "app.js"), str(WEB / "index.html")], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_review_metadata_is_inert_without_altering_duration_or_match_gates(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
const ready={workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(111),match_status:'ready',timeline_plan:{version:1,status:'ready',shortages:[]}};
for(const fact of [{},{fact_status:'ready',fact_issues:[]},{fact_status:'not_required'},
  {fact_status:'needs_review',fact_issues:[{term:'刷新率'}]}, {fact_status:null}, {fact_status:'unknown'},
  {fact_status:'ready',fact_issues:null}, {fact_issues:{}}, {fact_issues:'bad'}]){
  const draft={...ready,...fact},before=JSON.stringify(draft);
  assert.equal(u.canMatchDraft(draft),true);assert.equal(u.canRenderDraft(draft),true);
  assert.equal(u.canRenderDraft({...draft,match_status:'pending'}),false);
  assert.equal(u.canMatchDraft({...draft,voiceover:'短文'}),true);
  assert.equal(u.canRenderDraft({...draft,voiceover:'好'.repeat(181)}),true);
  assert.equal(u.canMatchDraft({...draft,voiceover:''}),false);
  assert.equal(JSON.stringify(draft),before);
}
assert.equal(u.canRenderDraft({segments:[],fact_status:'needs_review'}),true);
assert.equal(u.canMatchDraft(null),false);assert.equal(u.canRenderDraft(null),false);
""")

    def test_old_pending_review_draft_direct_match_and_ordinary_save_keep_paid_confirmation(self):
        tree = ast.parse((ROOT / "tests" / "test_frontend.py").read_text("utf-8"))
        harness = next(node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "harness" for target in node.targets))
        prefix = harness.split("(async()=>{", 1)[0]
        self.run_node(prefix + r"""
(async()=>{
await flush();
const voice='商品'.repeat(55)+'好。';
data.drafts=[{id:'old-pending',workflow:'script_first_v2',title:'显示器口播',voiceover:voice,target_seconds:25,timing_status:'ready',match_status:'pending',fact_status:'needs_review',fact_issues:[
 {field:'voiceover',segment_id:'s1',term:'刷新率',message:'商品标题未支持此功能'}
],segments:[{segment_id:'s1',text:voice,visual_hint:'INTERNAL_HINT_MUST_NOT_RENDER'}],clips:[],matches:[]}];
data.jobs=[{id:'old-job',kind:'script',status:'completed',created_at:1,message:'文案已保留，待核对商品信息',result:{draft_ids:['old-pending'],needs_fact_review_ids:['old-pending']}}];
timer();await flush();
const allText=node=>node.textContent+node.children.map(allText).join('');
assert.equal(document.getElementById('draft-voiceover').value,voice);
assert.ok($('draft-meta').textContent.includes('27.75'));
assert.ok(!document.getElementById('draft-fact-status'));
assert.equal($('draft-select').children[0].textContent,'显示器口播');
assert.equal($('mix-jobs').querySelector('.job-state').textContent,'已完成');
assert.ok(!allText($('mix-jobs')).includes('待核对'));
assert.ok(!$('mix-jobs').querySelectorAll('button').some(n=>n.textContent==='核对文案'));
assert.equal($('match-status').textContent,'等待匹配画面');
assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);
assert.equal($('save-draft').disabled,true);assert.equal($('save-draft').textContent,'保存修改');
assert.equal($('draft-save-status').textContent,'文案已保存');
assert.ok(!$('match-draft').title.includes('商品信息'));
let before=writes().length;
event($('render-draft'),'click');await flush();assert.equal(writes().length,before);assert.equal($('confirm-dialog').open,false);
// Existing cost confirmation is still required; no review modal or confirmation word is added.
event($('match-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.equal($('confirm-title').textContent,'分析素材并匹配画面？');assert.equal(writes().length,before);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,before);
event($('match-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();
assert.equal(writes().at(-1).path,'/api/v2/drafts/old-pending/match');assert.equal(writes().at(-1).body.allow_paid,true);
// Unsaved edits still block downstream actions; a normal save needs no review status rewrite.
let editor=document.getElementById('draft-voiceover');editor.value='新'+voice.slice(1);event(editor,'input');
assert.equal($('save-draft').disabled,false);assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);
before=writes().length;event($('match-draft'),'click');await flush();assert.equal(writes().length,before);
event($('save-draft'),'click');await flush();assert.equal(writes().at(-1).method,'PUT');
assert.equal($('match-draft').disabled,false);assert.equal($('draft-save-status').textContent,'文案已保存');
assert.equal(data.drafts[0].fact_status,'needs_review');assert.equal(data.drafts[0].fact_issues.length,1);
assert.equal($('confirm-dialog').open,false);
// Matching remains necessary, followed by the original explicit Fish fee confirmation.
data.drafts[0].match_status='ready';data.drafts[0].timeline_plan={version:1,status:'ready',shortages:[]};timer();await flush();assert.equal($('render-draft').disabled,false);
before=writes().length;event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.equal($('confirm-title').textContent,'确认文案并生成视频？');assert.ok($('confirm-description').textContent.includes('Fish Audio'));
assert.equal(writes().length,before);event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,before);
// Reference duration is soft even with obsolete needs_review metadata; a new plan remains necessary.
data.drafts[0].voiceover='短文';data.drafts[0].match_status='pending';data.drafts[0].timeline_plan=null;
data.jobs[0].result.needs_adjustment_ids=['old-pending'];timer();await flush();
assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);
assert.ok($('draft-timing-status').textContent.includes('仅作提示'));
assert.equal($('mix-jobs').querySelector('.job-state').textContent,'已完成');
console.log('NO_CLAIM_GATE_UI=PASS; MOCK_API_ONLY; REAL_PAID_CALLS=0');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_markup_and_scripts_have_no_copy_review_surface(self):
        js = (WEB / "app.js").read_text("utf-8")
        html = (WEB / "index.html").read_text("utf-8")
        for old_ui in ("factReview", "renderDraftFacts", "draft-fact-status", "核对并保存", "先核对商品信息", "待核对商品信息"):
            self.assertFalse(old_ui in js + html, old_ui)
        self.assertIn('confirmAction("分析素材并匹配画面？"', js)
        self.assertIn('confirmAction("确认文案并生成视频？"', js)


if __name__ == "__main__":
    unittest.main()
