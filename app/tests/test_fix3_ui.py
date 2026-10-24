"""Regression tests for late snapshots and keyed asset DOM updates (offline only)."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("FIX3_SOURCE", ROOT))
NODE = shutil.which("node")


def harness_prefix():
    tree = ast.parse((ROOT / "tests/test_frontend.py").read_text("utf-8"))
    harness = next(
        node.value.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "harness" for target in node.targets)
    )
    prefix = harness.split("(async()=>{", 1)[0]
    prefix = prefix.replace("const context={document,window", r"""
let holdState=false,releaseState=null,holdWrite=false,releaseWrite=null,stateCalls=0;
const delayed=async(path,options)=>{
 if(path==='/api/state'){
  stateCalls++;
  if(holdState){
   holdState=false;const snapshot=structuredClone(data);
   await new Promise(resolve=>{releaseState=resolve;});
   return {ok:true,json:async()=>({ok:true,data:snapshot})};
  }
 }
 if(options.method!=='GET'&&holdWrite){
  holdWrite=false;await new Promise(resolve=>{releaseWrite=resolve;});
 }
 if(path==='/api/settings'&&options.method==='PUT'){
  const body=JSON.parse(options.body);calls.push({path,method:options.method,body});Object.assign(data.settings,body);
  return {ok:true,json:async()=>({ok:true,data:structuredClone(data.settings)})};
 }
 return fetch(path,options);
};
const context={document,window""").replace(
        "history:{replaceState(){}},fetch,URL", "history:{replaceState(){}},fetch:delayed,URL"
    )
    return prefix


@unittest.skipUnless(NODE, "Node is needed for offline DOM tests")
class Fix3UiTests(unittest.TestCase):
    def run_js(self, body):
        code = harness_prefix() + r"""
(async()=>{
await flush();
const draft=id=>({id,title:id,workflow:'script_first_v2',voiceover:'旧'.repeat(100),target_seconds:25,revision:1,match_status:'pending',segments:[{segment_id:'s1',text:'旧'.repeat(100)}],clips:[],matches:[]});
""" + body + r"""
console.log('FIX3_UI=PASS; REAL_NETWORK_CALLS=0');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(
            [NODE, "-e", code, str(SOURCE / "web/app.js"), str(SOURCE / "web/index.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_saved_draft_survives_late_state_snapshot(self):
        self.run_js(r"""
data.drafts=[draft('a'),draft('b')];timer();await flush();
data.drafts[1].title='background completed';holdState=true;timer();await flush();assert.ok(releaseState);
let input=document.getElementById('draft-voiceover');input.value='新'.repeat(100);event(input,'input');
event($('save-draft'),'click');await flush();assert.equal(data.drafts[0].voiceover,'新'.repeat(100));
releaseState();await flush();
assert.equal(document.getElementById('draft-voiceover').value,'新'.repeat(100),'old GET must not replace the saved editor');
assert.equal($('save-draft').disabled,true);
assert.equal($('draft-select').children[1].textContent.includes('background completed'),true,'deferred fresh snapshot must still update other drafts');
input=document.getElementById('draft-voiceover');input.value+='追加';event(input,'input');event($('save-draft'),'click');await flush();
assert.equal(data.drafts[0].voiceover,'新'.repeat(100)+'追加');
""")

    def test_saved_settings_survive_late_state_snapshot(self):
        self.run_js(r"""
$('set-output_dir').value='E:/old';event($('settings-form'),'input');event($('settings-form'),'submit');await flush();
holdState=true;timer();await flush();assert.ok(releaseState);
$('set-output_dir').value='E:/new';event($('settings-form'),'input');event($('settings-form'),'submit');await flush();
releaseState();await flush();assert.equal($('set-output_dir').value,'E:/new','late GET must not revert saved settings');
$('set-transition_seconds').value='0.35';event($('settings-form'),'input');event($('settings-form'),'submit');await flush();
assert.equal(data.settings.output_dir,'E:/new');
""")

    def test_poll_waits_for_write_and_preserves_later_edits(self):
        self.run_js(r"""
holdWrite=true;$('set-output_dir').value='E:/saved';event($('settings-form'),'input');event($('settings-form'),'submit');await flush();assert.ok(releaseWrite);
const before=stateCalls;timer();timer();timer();await flush();
assert.equal(stateCalls,before,'polling during a mutation must coalesce instead of reading old server state');
$('set-output_dir').value='E:/unsaved';event($('settings-form'),'input');
releaseWrite();await flush();await flush();
assert.equal(data.settings.output_dir,'E:/saved');assert.equal($('set-output_dir').value,'E:/unsaved');
assert.equal($('settings-status').textContent,'有未保存的设置');
assert.ok(stateCalls>before&&stateCalls<=before+2,'pending reads must run without an unbounded refresh loop');
assert.equal(calls.filter(c=>c.path==='/api/settings'&&c.method==='PUT').length,1);
""")

    def test_refresh_requests_during_read_are_coalesced(self):
        self.run_js(r"""
holdState=true;timer();await flush();const before=stateCalls;
data.jobs.push({id:'new-job',kind:'script',status:'completed',created_at:123});
timer();timer();timer();await flush();assert.equal(stateCalls,before);
releaseState();await flush();await flush();assert.equal(stateCalls,before+1);
assert.match($('task-summary').textContent,/1 个任务/);
""")

    def test_stale_read_finishing_during_write_waits_for_commit(self):
        self.run_js(r"""
data.drafts=[draft('a')];timer();await flush();holdState=true;timer();await flush();
const before=stateCalls;holdWrite=true;
const input=document.getElementById('draft-voiceover');input.value='新'.repeat(100);event(input,'input');event($('save-draft'),'click');await flush();
releaseState();await flush();assert.equal(stateCalls,before,'stale read must not trigger a new GET before the pending write finishes');
assert.equal(input.value,'新'.repeat(100));releaseWrite();await flush();await flush();
assert.equal(document.getElementById('draft-voiceover').value,'新'.repeat(100));assert.equal(data.drafts[0].voiceover,'新'.repeat(100));
assert.ok(stateCalls>before&&stateCalls<=before+2);
""")

    def test_concurrent_mutations_wait_for_all_writes(self):
        self.run_js(r"""
data.drafts=[draft('a')];timer();await flush();holdWrite=true;
$('set-output_dir').value='E:/saved';event($('settings-form'),'input');event($('settings-form'),'submit');await flush();
const before=stateCalls;
const input=document.getElementById('draft-voiceover');input.value='新'.repeat(100);event(input,'input');event($('save-draft'),'click');await flush();
assert.equal(data.drafts[0].voiceover,'新'.repeat(100));assert.equal(stateCalls,before,'one completed write must not unblock state GET while another write is pending');
releaseWrite();await flush();await flush();assert.equal($('set-output_dir').value,'E:/saved');
assert.equal(document.getElementById('draft-voiceover').value,'新'.repeat(100));assert.ok(stateCalls>before&&stateCalls<=before+2);
""")

    def test_added_asset_keeps_existing_card_and_thumbnail(self):
        self.run_js(r"""
data.assets.forEach(a=>{a.thumbnail_url='/media/thumbs/'+a.id;});timer();await flush();
const old=$('asset-grid').children[0],oldImg=old.querySelector('img');assert.ok(oldImg);
const box=old.querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
const added=item('new','fourth','local','E:/source/new.mp4');added.imported_at=200;data.assets.push(added);timer();await flush();
const again=$('asset-grid').children.find(n=>n.dataset.id===old.dataset.id);
assert.equal(again,old,'adding one asset must not replace existing cards');assert.equal(again.querySelector('img'),oldImg);
assert.equal($('asset-grid').children[0].dataset.id,'new');assert.equal(box.checked,true);
assert.equal($('selection-count').textContent,'已选 1 个视频');
""")

    def test_asset_metadata_updates_in_place_and_removed_cards_leave(self):
        self.run_js(r"""
data.assets.forEach(a=>{a.thumbnail_url='/media/thumbs/'+a.id;});timer();await flush();
const old=$('asset-grid').children[0],oldImg=old.querySelector('img');
data.assets[0].name='renamed.mp4';data.assets[0].duration=62;data.assets[0].width=720;data.assets[0].height=1280;data.assets[0].transcript_status='ready';
timer();await flush();assert.equal($('asset-grid').children[0],old);assert.equal(old.querySelector('img'),oldImg);
assert.equal(old.querySelector('.asset-name').textContent,'renamed.mp4');assert.equal(old.querySelector('.asset-duration').textContent,'01:02');
assert.equal(old.querySelector('.asset-info').children[0].textContent,'720 × 1280');
assert.equal(old.querySelector('.asset-info').children[1].textContent,'有转写参考');
data.assets=data.assets.filter(a=>a.id!==old.dataset.id);timer();await flush();assert.equal($('asset-grid').children.length,1);assert.equal(old.parentNode,null);
""")

    def test_changed_thumbnail_replaces_only_its_image(self):
        self.run_js(r"""
data.assets.forEach(a=>{a.thumbnail_url='/media/thumbs/'+a.id;});timer();await flush();
const old=$('asset-grid').children[0],oldImg=old.querySelector('img');
data.assets[0].thumbnail_url='/media/thumbs/revised';timer();await flush();
assert.equal($('asset-grid').children[0],old);assert.notEqual(old.querySelector('img'),oldImg);assert.equal(old.querySelector('img').src,'/media/thumbs/revised');
""")


if __name__ == "__main__":
    unittest.main()
