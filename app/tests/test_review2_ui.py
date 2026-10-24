"""Review-2 UI regressions: actual JS with fake DOM and local APIs; no cloud calls."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('JDMIX_UI_SOURCE', str(ROOT)))
NODE=shutil.which('node') or r'C:\Program Files\nodejs\node.exe'

class Review2UiTests(unittest.TestCase):
    def run_case(self,case):
        tree=ast.parse((ROOT/'tests/test_frontend.py').read_text('utf-8'))
        harness=next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='harness' for t in n.targets))
        prefix=harness.split('(async()=>{',1)[0]
        prefix=prefix.replace('const context={document,window',r'''
let release=null,hold='',failNext='';
const delayed=async(path,options)=>{
 const base=await fetch(path,options);let result=null;
 const kind=path.endsWith('/matches')?'matches':path==='/api/settings'?'settings':path==='/api/settings/reset-credentials'?'reset':'';
 if(kind&&options.method!=='GET'){
  if(failNext===kind+'-schema'){failNext='';return {ok:false,status:400,json:async()=>({ok:false,error:'synthetic schema error'})};}
  if(failNext===kind){failNext='';return {ok:false,status:500,json:async()=>({ok:false,error:'synthetic failure'})};}
  const body=JSON.parse(options.body);
  if(kind==='matches'){
   const d=data.drafts.find(x=>path==='/api/v2/drafts/'+x.id+'/matches');d.match_candidates=body.matches.map(row=>({segment_id:row.segment_id,clip_ids:row.clip_indices?row.clip_indices.map(i=>body.clip_table[i]):row.clip_ids}));d.revision=(d.revision||1)+1;result=structuredClone(d);
  }else if(kind==='settings'){Object.assign(data.settings,body);result=structuredClone(data.settings);}
  else {assert.deepEqual(body,{confirm:true});Object.assign(data.settings,{credentials_need_reset:false,deepseek_key_set:false,fish_key_set:false,glm_key_set:false});result=structuredClone(data.settings);}
  if(hold===kind)await new Promise(r=>{release=r;});
 }
 return result?{ok:true,json:async()=>({ok:true,data:result})}:base;
};
const context={document,window''').replace('history:{replaceState(){}},fetch,URL','history:{replaceState(){}},fetch:delayed,URL')
        code=prefix+r'''
(async()=>{await flush();
const draft=id=>({id,title:id,workflow:'script_first_v2',voiceover:'好'.repeat(100),target_seconds:25,match_status:'ready',timeline_plan:{version:1,status:'ready',shortages:[]},segments:[{segment_id:'s1',text:'好'.repeat(100)}],clips:[{clip_id:'c1',asset_id:'remote',start:0,end:3,usable:true},{clip_id:'c2',asset_id:'remote',start:3,end:6,usable:true}],match_candidates:[{segment_id:'s1',clip_ids:['c1']}]});
const box=id=>$('match-rows').querySelectorAll('input[type="checkbox"]').find(x=>x.value===id);
const toggle=(id,checked)=>{box(id).checked=checked;event(box(id),'change');};
const selected=()=>$('match-rows').querySelectorAll('input[type="checkbox"]').filter(x=>x.checked).map(x=>x.value);
const choose=async id=>{$('draft-select').value=id;event($('draft-select'),'change');await flush();if($('confirm-dialog').open){event($('confirm-accept'),'click');await flush();}};
const settingsEdit=(id,value)=>{$(id).value=value;event($('settings-form'),'input');};
const broken=async()=>{data.settings.credentials_need_reset=true;timer();await flush();assert.ok($('credential-recovery'),'recovery UI exists');assert.equal($('credential-recovery').hidden,false);};
''' + case + r'''
console.log('REVIEW2_UI=PASS; MOCK_API_ONLY; PAID_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run([NODE,'-e',code,str(SOURCE/'web/app.js'),str(SOURCE/'web/index.html')],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_matches_save_preserves_newer_choices(self):
        self.run_case(r'''data.drafts=[draft('a')];timer();await flush();hold='matches';toggle('c1',false);event($('save-matches'),'click');await flush();assert.ok(release);toggle('c2',true);release();await flush();assert.deepEqual(selected(),['c2']);assert.equal($('save-matches').disabled,false);assert.equal($('match-save-status').textContent,'画面选择未保存');''')

    def test_matches_save_does_not_clear_other_draft_choices(self):
        self.run_case(r'''data.drafts=[draft('a'),draft('b')];timer();await flush();hold='matches';toggle('c1',false);event($('save-matches'),'click');await flush();await choose('b');toggle('c2',true);release();await flush();assert.equal($('draft-select').value,'b');assert.deepEqual(selected(),['c1','c2']);assert.equal($('save-matches').disabled,false);''')

    def test_matches_switch_back_is_new_editing_session(self):
        self.run_case(r'''data.drafts=[draft('a'),draft('b')];timer();await flush();hold='matches';toggle('c1',false);event($('save-matches'),'click');await flush();await choose('b');await choose('a');toggle('c2',true);release();await flush();assert.equal($('draft-select').value,'a');assert.ok(selected().includes('c2'));assert.equal($('save-matches').disabled,false);''')

    def test_matches_save_preserves_newer_voice_edit(self):
        self.run_case(r'''data.drafts=[draft('a')];timer();await flush();hold='matches';toggle('c1',false);event($('save-matches'),'click');await flush();const voice=document.getElementById('draft-voiceover');voice.value='新'.repeat(100);event(voice,'input');release();await flush();assert.equal(document.getElementById('draft-voiceover').value,'新'.repeat(100));assert.equal($('save-draft').disabled,false);''')

    def test_matches_clean_save_updates_saved_state(self):
        self.run_case(r'''data.drafts=[draft('a')];timer();await flush();toggle('c1',false);event($('save-matches'),'click');await flush();assert.deepEqual(selected(),[]);assert.equal($('save-matches').disabled,true);assert.equal(writes().filter(c=>c.path.endsWith('/matches')).length,1);''')

    def test_matches_failure_keeps_unsaved_selection(self):
        self.run_case(r'''data.drafts=[draft('a')];timer();await flush();toggle('c2',true);failNext='matches';event($('save-matches'),'click');await flush();assert.deepEqual(selected(),['c1','c2']);assert.equal($('save-matches').disabled,false);assert.ok($('toast-region').children[0].textContent.includes('failure'));''')

    def test_compact_schema_error_keeps_unsaved_selection(self):
        self.run_case(r'''data.drafts=[draft('a')];timer();await flush();toggle('c2',true);failNext='matches-schema';event($('save-matches'),'click');await flush();assert.deepEqual(selected(),['c1','c2']);assert.equal($('save-matches').disabled,false);assert.equal($('match-save-status').textContent,'画面选择未保存');assert.ok($('toast-region').children[0].textContent.includes('schema error'));''')

    def test_settings_save_preserves_new_inputs_and_key(self):
        self.run_case(r'''hold='settings';settingsEdit('set-output_dir','E:/submitted');event($('settings-form'),'submit');await flush();settingsEdit('set-output_dir','E:/newer');settingsEdit('set-glm_api_key','synthetic-new');release();await flush();assert.equal($('set-output_dir').value,'E:/newer');assert.equal($('set-glm_api_key').value,'synthetic-new');assert.equal($('settings-status').textContent,'有未保存的设置');assert.equal(writes().filter(c=>c.path==='/api/settings').length,1);''')

    def test_native_path_result_counts_as_new_setting_edit(self):
        self.run_case(r'''hold='settings';settingsEdit('set-output_dir','E:/old-output');event($('settings-form'),'submit');await flush();window.pywebview={api:{choose_folder:async()=>'E:/new-output'}};const picker=document.querySelectorAll('.choose-setting-folder').find(button=>button.dataset.for==='set-output_dir');event(picker,'click');await flush();release();await flush();assert.equal($('set-output_dir').value,'E:/new-output');assert.equal($('settings-status').textContent,'有未保存的设置');''')

    def test_settings_clean_save_clears_only_submitted_keys(self):
        self.run_case(r'''settingsEdit('set-glm_api_key','synthetic-saved');settingsEdit('set-output_dir','E:/saved');event($('settings-form'),'submit');await flush();assert.equal($('set-output_dir').value,'E:/saved');assert.equal($('set-glm_api_key').value,'');assert.equal($('settings-status').textContent,'设置已与本机同步');''')

    def test_settings_failure_preserves_dirty_fields(self):
        self.run_case(r'''settingsEdit('set-output_dir','E:/unsaved');settingsEdit('set-glm_api_key','synthetic-unsaved');failNext='settings';event($('settings-form'),'submit');await flush();assert.equal($('set-output_dir').value,'E:/unsaved');assert.equal($('set-glm_api_key').value,'synthetic-unsaved');assert.equal($('settings-status').textContent,'有未保存的设置');''')

    def test_reset_hidden_when_credentials_healthy(self):
        self.run_case(r'''assert.ok($('credential-recovery'));assert.equal($('credential-recovery').hidden,true);event($('reset-credentials'),'click');await flush();assert.equal($('confirm-dialog').open,false);assert.equal(writes().length,0);''')

    def test_reset_requires_explicit_confirmation(self):
        self.run_case(r'''await broken();event($('reset-credentials'),'click');await flush();assert.equal(writes().length,0);assert.equal($('confirm-dialog').open,true);assert.match($('confirm-description').textContent,/备份/);assert.match($('confirm-description').textContent,/空凭据/);assert.match($('confirm-description').textContent,/重新输入/);event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);assert.equal($('credential-recovery').hidden,false);''')

    def test_reset_success_preserves_dirty_settings_and_keys(self):
        self.run_case(r'''await broken();settingsEdit('set-output_dir','E:/newer');settingsEdit('set-glm_api_key','synthetic-pending');event($('reset-credentials'),'click');await flush();event($('confirm-accept'),'click');await flush();assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/settings/reset-credentials');assert.deepEqual(writes()[0].body,{confirm:true});assert.equal($('set-output_dir').value,'E:/newer');assert.equal($('set-glm_api_key').value,'synthetic-pending');assert.equal($('settings-status').textContent,'有未保存的设置');assert.equal($('credential-recovery').hidden,true);for(const id of ['deepseek','fish','glm'])assert.equal($(id+'-key-status').textContent,'未配置');''')

    def test_reset_pending_keeps_later_input(self):
        self.run_case(r'''await broken();hold='reset';event($('reset-credentials'),'click');await flush();event($('confirm-accept'),'click');await flush();assert.ok(release);settingsEdit('set-glm_api_key','synthetic-later');release();await flush();assert.equal($('set-glm_api_key').value,'synthetic-later');assert.equal($('settings-status').textContent,'有未保存的设置');assert.equal($('credential-recovery').hidden,true);''')

    def test_reset_failure_keeps_recovery_and_draft_settings(self):
        self.run_case(r'''await broken();settingsEdit('set-glm_api_key','synthetic-pending');failNext='reset';event($('reset-credentials'),'click');await flush();event($('confirm-accept'),'click');await flush();assert.equal($('credential-recovery').hidden,false);assert.equal($('set-glm_api_key').value,'synthetic-pending');assert.equal($('settings-status').textContent,'有未保存的设置');assert.ok($('toast-region').children[0].textContent.includes('failure'));''')

    def test_reset_and_settings_save_are_mutually_exclusive(self):
        self.run_case(r'''await broken();hold='reset';event($('reset-credentials'),'click');await flush();event($('confirm-accept'),'click');await flush();assert.ok(release);assert.equal($('save-settings').disabled,true);event($('settings-form'),'submit');await flush();assert.equal(writes().length,1);release();await flush();assert.equal($('save-settings').disabled,false);''')

    def test_save_in_progress_blocks_credential_reset(self):
        self.run_case(r'''await broken();hold='settings';settingsEdit('set-glm_api_key','synthetic-pending');event($('settings-form'),'submit');await flush();assert.ok(release);assert.equal($('reset-credentials').disabled,true);event($('reset-credentials'),'click');await flush();assert.equal($('confirm-dialog').open,false);assert.equal(writes().length,1);release();await flush();assert.equal($('reset-credentials').disabled,false);''')

    def test_invalid_assets_are_removed_without_losing_other_folder_selection(self):
        self.run_case(r'''let cb=$('asset-grid').children[0].querySelector('input[type="checkbox"]');cb.checked=true;event(cb,'change');event($('library-tab-douyin'),'click');cb=$('asset-grid').children.find(n=>n.dataset.id==='remote').querySelector('input[type="checkbox"]');cb.checked=true;event(cb,'change');assert.equal($('selection-count').textContent,'已选 2 个视频');data.assets.find(x=>x.id==='remote').selectable=false;data.assets.find(x=>x.id==='remote').availability='missing';data.folder='E:/another';timer();await flush();assert.equal($('selection-count').textContent,'已选 1 个视频');assert.ok(!$('asset-grid').children.some(n=>n.dataset.id==='remote'));event($('library-tab-local'),'click');assert.equal($('asset-grid').children.length,0);assert.equal($('selection-count').textContent,'已选 1 个视频');''')

    def test_matches_request_uses_shared_table_and_keeps_order(self):
        self.run_case(r'''const d=draft('a');d.segments.push({segment_id:'s2',text:'好'.repeat(10)});d.match_candidates.push({segment_id:'s2',clip_ids:['c2','c1']});data.drafts=[d];timer();await flush();toggle('c1',false);event($('save-matches'),'click');await flush();const b=writes().find(c=>c.path.endsWith('/matches')).body;assert.deepEqual(b.clip_table,['c2','c1']);assert.deepEqual(b.matches,[{segment_id:'s1',clip_indices:[]},{segment_id:'s2',clip_indices:[0,1]}]);assert.equal('clip_ids' in b.matches[0],false);''')

    def test_compact_maximum_pool_roundtrips_under_route_limit(self):
        code=r'''const assert=require('node:assert/strict'),u=require(process.argv[1]);const ids=Array.from({length:4500},(_,i)=>i.toString(16).padStart(24,'0'));const rows=Array.from({length:64},(_,i)=>({segment_id:'s'+i,clip_ids:i%2?[...ids].reverse():ids}));const body=u.encodeMatches(rows);assert.equal(body.clip_table.length,4500);assert.ok(Buffer.byteLength(JSON.stringify(body))<8*1024*1024);assert.deepEqual(body.matches.map(r=>({segment_id:r.segment_id,clip_ids:r.clip_indices.map(i=>body.clip_table[i])})),rows);console.log('COMPACT_4500_X_64=PASS');'''
        result=subprocess.run([NODE,'-e',code,str(SOURCE/'web/app.js')],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
