"""Automatic timeline and compact task UI, fully local Node/fake API tests."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

@unittest.skipUnless(shutil.which("node"), "Node required for offline frontend tests")
class AutoTimelineUiTests(unittest.TestCase):
    def run_node(self, code):
        result = subprocess.run(["node", "-e", code, str(WEB / "app.js"), str(WEB / "index.html")], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    def prefix(self):
        tree = ast.parse((ROOT / "tests/test_frontend.py").read_text("utf-8"))
        harness = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "harness" for t in n.targets))
        return harness.split("(async()=>{",1)[0]
    def test_ready_requires_unified_plan_not_just_one_clip_per_sentence(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
const d={workflow:'script_first_v2',target_seconds:25,voiceover:'好'.repeat(100),match_status:'ready'};
assert.equal(u.canRenderDraft(d),false);
assert.equal(u.canRenderDraft({...d,timeline_plan:{version:1,status:'shortage',shortages:[{segment_id:'s1'}]}}),false);
assert.equal(u.canRenderDraft({...d,timeline_plan:{version:1,status:'ready',shortages:[]}}),true);
assert.equal(u.canRenderDraft({...d,timeline_plan:{version:1,status:'ready',shortages:[{}]}}),false);
assert.equal(u.canMatchDraft(d),true);
""")
    def test_limited_repeat_counts_source_time_not_clip_id(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
const clips=[{clip_id:'a',asset_id:'x',start:0,end:3},{clip_id:'alias',asset_id:'y',start:1,end:3},{clip_id:'b',asset_id:'x',start:3,end:6},{clip_id:'c',asset_id:'x',start:6,end:9}];
const assets=[{id:'x',sha256:'same'},{id:'y',sha256:'same'}];
assert.equal(u.selectionBudget(clips,[['a'],['alias']],assets).ready,true);
assert.equal(u.selectionBudget(clips,[['a'],['alias'],['a']],assets).ready,false);
assert.equal(u.selectionBudget(clips,[['a','b','c'],['a','b','c']],assets).ready,false);
assert.equal(u.selectionBudget(clips,[['a','b'],['a','b']],assets).repeatCount,2);
""")
    def test_job_pages_are_twenty_active_first_without_mutating_all_tasks(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),u=require(process.argv[1]);
const jobs=Array.from({length:35},(_,i)=>({id:String(i),created_at:i,status:'completed'}));
jobs[0].status='running';jobs[1].status='cancelling';const before=JSON.stringify(jobs);
const page=u.jobPage(jobs,0);assert.equal(page.items.length,20);assert.ok(page.items.slice(0,2).every(j=>j.status!=='completed'));assert.equal(page.total,35);
assert.equal(u.jobPage(jobs,1).items.length,15);assert.equal(JSON.stringify(jobs),before);
""")
    def test_real_handlers_show_net_deficits_and_allow_bounded_cross_sentence_reuse(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
const voice='商品'.repeat(50)+'。';
const plan={version:1,status:'shortage',phase:'estimated',speed:1.25,repeat_count:1,total_seconds:20,transition_seconds:.2,
 segments:[{segment_id:'s1',required_seconds:10,available_seconds:10},{segment_id:'s2',required_seconds:10,available_seconds:7}],
 shortages:[{segment_id:'s2',required_seconds:10,available_seconds:7,missing_seconds:3,reason:'画面不足'}]};
data.drafts=[{id:'auto',workflow:'script_first_v2',title:'自动匹配',voiceover:voice,target_seconds:25,match_status:'needs_review',timeline_plan:plan,
segments:[{segment_id:'s1',text:voice.slice(0,50)},{segment_id:'s2',text:voice.slice(50)}],
clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true},{clip_id:'c2',asset_id:'remote',start:3,end:6,usable:true}],
matches:[{segment_id:'s1',clip_ids:['c1']},{segment_id:'s2',clip_ids:['c2']}]}];
timer();await flush();const text=n=>n.textContent+n.children.map(text).join('');
assert.ok(text($('timeline-summary')).includes('1.25'));assert.ok(text($('timeline-summary')).includes('净画面'));
assert.ok(text($('timeline-summary')).includes('3.00'));assert.equal($('render-draft').disabled,true);
assert.ok($('render-action-reason').textContent.includes('缺'));assert.equal($('match-draft').disabled,false);
const row2=$('match-rows').querySelectorAll('fieldset.match-row')[1];
const reuse=row2.querySelectorAll('input[type="checkbox"]').find(x=>x.value==='c1');reuse.checked=true;event(reuse,'change');
assert.equal(reuse.checked,true);assert.equal($('save-matches').disabled,false);
assert.equal($('match-draft').disabled,true);assert.ok($('match-action-reason').textContent.includes('保存画面选择'));
event($('save-matches'),'click');await flush();assert.equal(writes().at(-1).method,'PUT');
assert.deepEqual(writes().at(-1).body.matches[1].clip_indices.map(i=>writes().at(-1).body.clip_table[i]),['c2','c1']);
assert.equal($('render-draft').disabled,true); // Saving a selection is not itself a ready plan.
data.drafts[0].timeline_plan={...plan,status:'ready',phase:'actual',shortages:[],segments:plan.segments.map(s=>({...s,available_seconds:s.required_seconds}))};
data.drafts[0].match_status='ready';timer();await flush();
assert.equal($('render-draft').disabled,false);assert.ok(text($('timeline-summary')).includes('实测'));
// Hidden-page active tasks still affect locking; only rendering is paginated.
data.jobs=Array.from({length:25},(_,i)=>({id:'other-'+i,kind:'product',status:'running',created_at:i}));
data.jobs.push({id:'hidden-match',kind:'match',draft_id:'auto',status:'running',created_at:-100});
timer();await flush();assert.equal($('mix-jobs').children.length,20);
assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);
assert.ok($('match-action-reason').textContent.includes('正在进行'));
console.log('AUTO_TIMELINE_DOM=PASS; REAL_API_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    def test_shared_candidate_pool_does_not_pretend_to_be_final_repeat_plan(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.drafts=[{id:'shared',workflow:'script_first_v2',voiceover:'好'.repeat(100),target_seconds:25,match_status:'needs_review',
segments:['s1','s2','s3'].map(id=>({segment_id:id,text:'商品口播'})),
clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true},{clip_id:'c2',asset_id:'remote',start:3,end:6,usable:true}],
matches:[{segment_id:'s1',clip_ids:['c1']},{segment_id:'s2',clip_ids:['c1']},{segment_id:'s3',clip_ids:['c2']}],
timeline_plan:{version:1,status:'shortage',phase:'estimated',speed:1,repeat_count:1,segments:[],shortages:[{segment_id:'s3',missing_seconds:4}]}}];
timer();await flush();
const row=$('match-rows').querySelectorAll('fieldset.match-row')[2];
const box=row.querySelectorAll('input[type="checkbox"]').find(x=>x.value==='c1');box.checked=true;event(box,'change');
assert.equal(box.checked,true);event($('save-matches'),'click');await flush();
assert.equal(writes().at(-1).path,'/api/v2/drafts/shared/matches');
assert.deepEqual(writes().at(-1).body.matches[2].clip_indices.map(i=>writes().at(-1).body.clip_table[i]),['c2','c1']);
assert.equal($('render-draft').disabled,true); // Backend final plan, never candidate count, determines readiness.
console.log('SHARED_CANDIDATES=PASS; MOCK_API_ONLY');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    def test_manual_editor_shows_candidate_pool_and_saves_empty_replacement(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.drafts=[{id:'pool',workflow:'script_first_v2',voiceover:'好'.repeat(100),target_seconds:25,match_status:'needs_review',
segments:[{segment_id:'s1',text:'商品口播'}],clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true},{clip_id:'c2',asset_id:'remote',start:3,end:6,usable:true}],
match_candidates:[{segment_id:'s1',clip_ids:['c1','c2']}],matches:[{segment_id:'s1',clip_ids:['c1']}],
timeline_plan:{version:1,status:'shortage',phase:'estimated',speed:1,repeat_count:0,segments:[],shortages:[{segment_id:'s1',missing_seconds:20}]}}];
timer();await flush();const boxes=$('match-rows').querySelectorAll('input[type="checkbox"]');
assert.equal(boxes.length,2);assert.ok(boxes.every(x=>x.checked));
assert.ok($('match-review-hint').textContent.includes('替换本句候选'));
for(const box of boxes){box.checked=false;event(box,'change');}
event($('save-matches'),'click');await flush();
assert.deepEqual(writes().at(-1).body,{clip_table:[],matches:[{segment_id:'s1',clip_indices:[]}]});
assert.equal($('render-draft').disabled,true);
console.log('MANUAL_POOL_REPLACE=PASS; MOCK_API_ONLY');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    def test_compact_job_paging_error_details_and_local_logs_route(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.jobs=Array.from({length:27},(_,i)=>({id:'finished-'+i,kind:'download',status:'completed',created_at:i}));
data.jobs.push({id:'older-running',kind:'download',status:'running',created_at:-100,message:'下载中',progress:.2});
data.jobs.push({id:'error',kind:'match',status:'failed',created_at:1,error:'错误详情'.repeat(100)});
timer();await flush();assert.equal($('download-jobs').children.length,20);
assert.ok($('download-jobs').children[0].querySelectorAll('button').some(n=>n.textContent==='取消'));
assert.equal($('mix-jobs').querySelector('details'),null);
const errorDetail=$('mix-jobs').querySelector('.job-heading').querySelector('.job-inline-detail');assert.ok(errorDetail);
event(errorDetail,'click');await flush();assert.equal($('job-detail-dialog').open,true);
assert.ok($('job-detail-content').textContent.length>100);event($('close-job-detail'),'click');
event($('download-next'),'click');await flush();assert.equal($('download-jobs').children.length,8);
event($('download-prev'),'click');await flush();assert.equal($('download-jobs').children.length,20);
const before=writes().length;event($('open-mix-logs'),'click');await flush();
assert.equal(writes().length,before+1);assert.equal(writes().at(-1).path,'/api/open-logs');
console.log('COMPACT_TASK_UI=PASS; MOCK_API_ONLY');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

if __name__ == "__main__": unittest.main()
