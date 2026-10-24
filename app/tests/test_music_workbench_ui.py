"""Workbench-managed music library; no real clipboard/files/API calls."""
import shutil
import unittest
import test_simple_mix_ui as fixture


@unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
class MusicWorkbenchUiTests(unittest.TestCase):
    run_node=fixture.SimpleMixUiTests.run_node

    def prefix(self):
        prefix=fixture.SimpleMixUiTests.prefix(self)
        return prefix.replace('const context={document,window',r'''
const music={tracks:[{id:'song-a',name:'轻快.mp3',size:100,duration:20,available:true,selected:true},
{id:'song-b',name:'舒缓.wav',size:200,duration:30,available:true,selected:false}],
current:{id:'song-a',name:'轻快.mp3',path:'R:/runtime/music-library/a.mp3',available:true,managed:true},gain:.15,error:''};
data.music={current:structuredClone(music.current),gain:music.gain,error:''};
let releaseMusic=null,holdMusic='',failMusic='',scanCalls=0,hiddenImportDetail=null,importDetailReads=0;
const musicFetch=async(path,options)=>{
 const body=options.body?JSON.parse(options.body):null;
 if(path==='/api/jobs/import-job'&&hiddenImportDetail){
  calls.push({path,method:options.method,body});importDetailReads++;
  return {ok:true,json:async()=>({ok:true,data:structuredClone(hiddenImportDetail)})};
 }
 if(!path.startsWith('/api/music'))return fetch(path,options);
 calls.push({path,method:options.method,body});
 if(failMusic===path){failMusic='';return {ok:false,status:400,json:async()=>({ok:false,error:'歌曲文件已丢失'})};}
 let result=music;
 if(path==='/api/music/scan'){scanCalls++;result={scan_id:'scan-one',candidates:[{id:'candidate-a',name:'待导入.mp3',size:300}],skipped:1};}
 if(path==='/api/music/import')result={id:'import-job',kind:'music_import',status:'queued'};
 if(path==='/api/music/select'){
  const chosen=music.tracks.find(song=>song.id===body.track_id);
  music.current=chosen?{id:chosen.id,name:chosen.name,path:'R:/runtime/music-library/'+chosen.id,available:true,managed:true}:null;
  music.tracks.forEach(song=>song.selected=song.id===body.track_id);
 }
 if(path==='/api/music/volume')music.gain=body.gain;
 if(path==='/api/music/delete'){
  music.tracks=music.tracks.filter(song=>!body.track_ids.includes(song.id));
  if(music.current&&body.track_ids.includes(music.current.id))music.current=null;
  result={...music,removed_ids:body.track_ids,failed:[]};
 }
 const snapshot=structuredClone(result);
 data.music={current:structuredClone(music.current),gain:music.gain,error:music.error};
 if(holdMusic===path)await new Promise(resolve=>{releaseMusic=()=>{holdMusic='';resolve();};});
 return {ok:true,json:async()=>({ok:true,data:snapshot})};
};
const context={document,window''').replace('history:{replaceState(){}},fetch,URL','history:{replaceState(){}},fetch:musicFetch,URL')

    def script(self,body):
        return self.prefix()+"\n(async()=>{await flush();\n"+body+"\n})().catch(e=>{console.error(e);process.exitCode=1;});"

    def test_music_is_between_parameters_and_copy_and_summary_is_name_only(self):
        self.run_node(self.script(r'''
assert.ok(html.indexOf('id="prepare-form"')<html.indexOf('id="music-panel"'));
assert.ok(html.indexOf('id="music-panel"')<html.indexOf('id="draft-workspace"'));
const settingsMarkup=html.split('id="settings-form"')[1].split('</form>')[0];
assert.ok(!settingsMarkup.includes('set-bgm_path'));assert.ok(!settingsMarkup.includes('set-bgm_gain'));
assert.equal($('music-current-name').textContent,'轻快.mp3');assert.equal($('music-panel').open,false);
assert.equal(writes().length,0);
event($('settings-form'),'submit');await flush();
const body=writes().at(-1).body;assert.ok(!('bgm_path' in body));assert.ok(!('bgm_gain' in body));
'''))

    def test_folder_scan_requires_explicit_checks_before_import(self):
        self.run_node(self.script(r'''
window.pywebview={api:{choose_music_folder:async()=>'E:/music-source'}};
event($('choose-bgm'),'click');await flush();assert.equal(scanCalls,1);
assert.equal($('music-folder').value,'E:/music-source');assert.equal($('import-music').disabled,true);
assert.equal(writes().filter(call=>call.path==='/api/music/import').length,0);
const box=$('music-candidates').querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
assert.equal($('import-music').disabled,false);event($('import-music'),'click');await flush();
assert.deepEqual(writes().find(call=>call.path==='/api/music/import').body,{scan_id:'scan-one',candidate_ids:['candidate-a']});
assert.equal($('import-music').disabled,true);
data.jobs=[{id:'import-job',kind:'music_import',status:'completed',created_at:2,result:{imported:1,duplicates:0,failed:[]}}];
timer();await flush();assert.ok(calls.some(call=>call.path==='/api/music'&&call.method==='GET'));
'''))

    def test_select_saves_immediately_and_poll_does_not_change_song(self):
        self.run_node(self.script(r'''
$('music-panel').open=true;event($('music-panel'),'toggle');await flush();
const radio=$('music-tracks').querySelectorAll('input').find(node=>node.value==='song-b');radio.checked=true;event(radio,'change');await flush();
assert.deepEqual(writes().find(call=>call.path==='/api/music/select').body,{track_id:'song-b'});
assert.equal($('music-current-name').textContent,'舒缓.wav');timer();await flush();
assert.equal($('music-current-name').textContent,'舒缓.wav');
assert.equal(writes().filter(call=>call.path==='/api/music/select').length,1);
'''))

    def test_delete_confirmation_cancel_and_current_song_disables_music(self):
        self.run_node(self.script(r'''
$('music-panel').open=true;event($('music-panel'),'toggle');await flush();
let remove=$('music-tracks').children.find(row=>row.dataset.id==='song-a').querySelector('button');
event(remove,'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.match($('confirm-description').textContent,/原始歌曲/);assert.match($('confirm-description').textContent,/关闭背景音乐/);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
event(remove,'click');await flush();event($('confirm-accept'),'click');await flush();
assert.deepEqual(writes().at(-1).body,{track_ids:['song-a'],confirm:true});
assert.equal($('music-current-name').textContent,'未选择');
assert.equal(writes().filter(call=>call.path==='/api/music/select').length,0);
assert.ok($('music-tracks').children.some(row=>row.dataset.id==='song-b'));
'''))

    def test_missing_song_reports_inside_panel_without_automatic_replacement(self):
        self.run_node(self.script(r'''
data.music.current.available=false;data.music.error='当前默认歌曲已丢失，请重新导入或关闭背景音乐';
timer();await flush();assert.equal($('music-error').hidden,false);
assert.match($('music-error').textContent,/已丢失/);assert.equal($('music-current-name').textContent,'轻快.mp3');
assert.equal(writes().length,0);
'''))

    def test_volume_saves_independently_and_newer_edit_survives_older_response(self):
        self.run_node(self.script(r'''
holdMusic='/api/music/volume';$('set-bgm_gain').value='0.2';event($('set-bgm_gain'),'input');event($('set-bgm_gain'),'change');await flush();
assert.ok(releaseMusic);$('set-bgm_gain').value='0.35';event($('set-bgm_gain'),'input');
releaseMusic();await flush();assert.equal($('set-bgm_gain').value,'0.35');
event($('set-bgm_gain'),'change');await flush();
assert.deepEqual(writes().filter(call=>call.path==='/api/music/volume').map(call=>call.body.gain),[.2,.35]);
assert.equal(music.gain,.35);assert.equal(writes().filter(call=>call.path==='/api/settings').length,0);
'''))

    def test_empty_volume_does_not_turn_music_silent(self):
        self.run_node(self.script(r'''
$('set-bgm_gain').value='';event($('set-bgm_gain'),'input');event($('set-bgm_gain'),'change');await flush();
assert.equal(writes().length,0);assert.match($('music-error').textContent,/音量/);assert.equal(music.gain,.15);
'''))

    def test_failed_selection_preserves_previous_default_and_shows_local_error(self):
        self.run_node(self.script(r'''
$('music-panel').open=true;event($('music-panel'),'toggle');await flush();failMusic='/api/music/select';
const radio=$('music-tracks').querySelectorAll('input').find(node=>node.value==='song-b');radio.checked=true;event(radio,'change');await flush();
assert.equal($('music-current-name').textContent,'轻快.mp3');assert.match($('music-error').textContent,/已丢失/);
assert.equal(writes().length,1);
'''))

    def test_late_folder_scan_cannot_import_from_a_newer_field_value(self):
        self.run_node(self.script(r'''
$('music-folder').value='E:/first';event($('music-folder'),'input');holdMusic='/api/music/scan';
event($('scan-music'),'click');await flush();assert.ok(releaseMusic);
$('music-folder').value='E:/newer';event($('music-folder'),'input');releaseMusic();await flush();
assert.equal($('music-candidates').querySelector('input'),null);assert.equal($('import-music').disabled,true);
assert.equal(writes().filter(call=>call.path==='/api/music/import').length,0);
'''))

    def test_queued_volume_commits_remain_in_user_order(self):
        self.run_node(self.script(r'''
holdMusic='/api/music/volume';$('set-bgm_gain').value='0.2';event($('set-bgm_gain'),'input');event($('set-bgm_gain'),'change');await flush();
$('set-bgm_gain').value='0.4';event($('set-bgm_gain'),'input');event($('set-bgm_gain'),'change');
releaseMusic();await flush();await flush();
assert.deepEqual(writes().filter(call=>call.path==='/api/music/volume').map(call=>call.body.gain),[.2,.4]);
assert.equal(music.gain,.4);assert.equal($('set-bgm_gain').value,'0.4');
'''))

    def test_completed_import_outside_recent_jobs_is_reconciled_without_permanent_busy(self):
        self.run_node(self.script(r'''
hiddenImportDetail={id:'import-job',kind:'music_import',status:'running',result:null};
$('music-folder').value='E:/music';event($('music-folder'),'input');event($('scan-music'),'click');await flush();
let box=$('music-candidates').querySelector('input');box.checked=true;event(box,'change');
event($('import-music'),'click');await flush();assert.equal($('scan-music').disabled,true);
// The completed import was pushed outside compact state's latest download jobs.
data.jobs=Array.from({length:20},(_,i)=>({id:'newer-download-'+i,kind:'download',status:'completed',created_at:100+i}));
hiddenImportDetail={id:'import-job',kind:'music_import',status:'completed',result:{imported_count:2,duplicate_count:1,failed_count:0}};
timer();await flush();await flush();
assert.ok(importDetailReads>0);assert.equal($('scan-music').disabled,false);assert.equal($('choose-bgm').disabled,false);
assert.match($('music-status').textContent,/新增 2 首/);assert.match($('music-status').textContent,/已有 1 首/);
box=$('music-candidates').querySelector('input');box.checked=true;event(box,'change');
assert.equal($('import-music').disabled,false);
assert.ok(calls.some(call=>call.path==='/api/music'&&call.method==='GET'));
'''))


if __name__=='__main__':unittest.main()
