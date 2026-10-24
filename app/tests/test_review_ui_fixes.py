"""Regression for pending-save edits; real app.js and fake local APIs only."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('JDMIX_UI_SOURCE',str(ROOT)))
NODE=shutil.which('node') or (r'C:\Program Files\nodejs\node.exe' if Path(r'C:\Program Files\nodejs\node.exe').is_file() else None)

@unittest.skipUnless(NODE,'Node required for offline UI event tests')
class PendingSaveReviewTests(unittest.TestCase):
    def run_case(self,case):
        tree=ast.parse((ROOT/'tests/test_frontend.py').read_text('utf-8'))
        harness=next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='harness' for t in n.targets))
        prefix=harness.split('(async()=>{',1)[0]
        prefix=prefix.replace('const context={document,window',r'''
let releaseSave=null;
const deferredFetch=async(path,options)=>{
 let response=await fetch(path,options);
 if(options.method==='PUT'&&/^\/api\/drafts\/[^/]+$/.test(path)){
   const target=data.drafts.find(d=>path.endsWith('/'+d.id));target.segments=JSON.parse(options.body).segments;
   const saved=structuredClone(target);response={ok:true,json:async()=>({ok:true,data:saved})};
 }
 if(options.method==='PUT'&&/^\/api\/(v2\/)?drafts\/[^/]+$/.test(path)){
   await new Promise(resolve=>{releaseSave=resolve;});
 }
 return response;
};
const context={document,window''')
        prefix=prefix.replace('history:{replaceState(){}},fetch,URL','history:{replaceState(){}},fetch:deferredFetch,URL')
        code=prefix+r'''
(async()=>{await flush();
const initial='好'.repeat(100),submitted='甲'.repeat(100),newer='乙'.repeat(100);
const draft=(id,legacy=false)=>legacy?{id,title:id,target_seconds:25,segments:[{clip_id:'c1',text:initial}],clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3}]}:
 {id,title:id,workflow:'script_first_v2',voiceover:initial,target_seconds:25,match_status:'pending',segments:[{segment_id:'s1',text:initial}],clips:[],matches:[]};
const editor=()=>$('draft-segments').querySelector('textarea');
const edit=text=>{editor().value=text;event(editor(),'input');};
const choose=async(id)=>{$('draft-select').value=id;event($('draft-select'),'change');await flush();if($('confirm-dialog').open){event($('confirm-accept'),'click');await flush();}};
const assertPending=text=>{assert.equal(editor().value,text,'newer editor content must survive');assert.equal($('save-draft').disabled,false,'newer edit must remain unsaved/actionable');assert.ok($('draft-save-status').textContent.includes('未保存'),'do not claim newer edit saved');};
''' + case + r'''
console.log('PENDING_SAVE_REVIEW=PASS; MOCK_API_ONLY; PAID_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run([NODE,'-e',code,str(SOURCE/'web/app.js'),str(SOURCE/'web/index.html')],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_new_typing_while_save_pending_survives(self):
        self.run_case(r'''
data.drafts=[draft('a')];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
edit(newer);releaseSave();await flush();assertPending(newer);
assert.equal(writes().filter(c=>c.method==='PUT').length,1);
assert.equal(writes().find(c=>c.method==='PUT').body.voiceover,submitted);
''')

    def test_switch_to_other_draft_keeps_newer_edit_and_dirty(self):
        self.run_case(r'''
data.drafts=[draft('a'),draft('b')];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
await choose('b');edit(newer);releaseSave();await flush();assert.equal($('draft-select').value,'b');assertPending(newer);
''')

    def test_switch_away_and_back_same_id_does_not_validate_old_response(self):
        self.run_case(r'''
data.drafts=[draft('a'),draft('b')];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
await choose('b');await choose('a');edit(newer);releaseSave();await flush();assert.equal($('draft-select').value,'a');assertPending(newer);
''')

    def test_clean_save_still_clears_dirty_and_unlocks_matching(self):
        self.run_case(r'''
data.drafts=[draft('a')];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
releaseSave();await flush();assert.equal(editor().value,submitted);assert.equal($('save-draft').disabled,true);assert.equal($('draft-save-status').textContent,'文案已保存');assert.equal($('match-draft').disabled,false);
''')

    def test_save_response_does_not_switch_back_from_clean_other_draft(self):
        self.run_case(r'''
data.drafts=[draft('a'),draft('b')];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
await choose('b');releaseSave();await flush();assert.equal($('draft-select').value,'b');assert.equal(editor().value,initial);assert.equal($('save-draft').disabled,true);
''')

    def test_legacy_editor_pending_save_keeps_new_text(self):
        self.run_case(r'''
data.drafts=[draft('legacy',true)];timer();await flush();edit(submitted);event($('save-draft'),'click');await flush();assert.ok(releaseSave);
edit(newer);releaseSave();await flush();assertPending(newer);
''')

    def test_large_auto_pool_can_search_and_replace_one_candidate(self):
        self.run_case(r'''
const clips=Array.from({length:66},(_,i)=>({clip_id:'c'+i,asset_id:'remote',start:i*3,end:i*3+3,usable:true,text:i===65?'replacement-unique':'old candidate'}));
const pool=draft('pool');pool.clips=clips;pool.match_candidates=[{segment_id:'s1',clip_ids:clips.slice(0,65).map(c=>c.clip_id)}];
data.drafts=[pool];timer();await flush();
let boxes=$('match-rows').querySelectorAll('input[type="checkbox"]');const removed=boxes.find(x=>x.value==='c0');removed.checked=false;event(removed,'change');
const search=$('match-rows').querySelector('input');search.value='replacement-unique';event(search,'input');
boxes=$('match-rows').querySelectorAll('input[type="checkbox"]');const replacement=boxes.find(x=>x.value==='c65');
assert.ok(replacement,'selected candidates must not hide the search result');
assert.equal(boxes.filter(x=>x.checked).length,64);replacement.checked=true;event(replacement,'change');assert.equal(replacement.checked,true);
event($('save-matches'),'click');await flush();
const request=writes().find(c=>c.path==='/api/v2/drafts/pool/matches');assert.ok(request);const chosen=request.body.matches[0].clip_indices.map(i=>request.body.clip_table[i]);assert.equal(chosen.length,65);
assert.ok(chosen.includes('c65'));assert.ok(!chosen.includes('c0'));
''')

    def test_candidate_pool_cap_rejects_the_4501st_candidate(self):
        self.run_case(r'''
const clips=Array.from({length:4501},(_,i)=>({clip_id:'c'+i,asset_id:'remote',start:i*3,end:i*3+3,usable:true,text:i===4500?'replacement-unique':'old candidate'}));
const pool=draft('pool');pool.clips=clips;pool.match_candidates=[{segment_id:'s1',clip_ids:clips.slice(0,4500).map(c=>c.clip_id)}];
data.drafts=[pool];timer();await flush();
const search=$('match-rows').querySelector('input');search.value='replacement-unique';event(search,'input');
const boxes=$('match-rows').querySelectorAll('input[type="checkbox"]');const extra=boxes.find(x=>x.value==='c4500');assert.ok(extra);
extra.checked=true;event(extra,'change');assert.equal(extra.checked,false);assert.equal(boxes.filter(x=>x.checked).length,4500);
assert.ok($('toast-region').children[0].textContent.includes('4500'));assert.equal(writes().length,0);
''')

if __name__=='__main__':unittest.main()
