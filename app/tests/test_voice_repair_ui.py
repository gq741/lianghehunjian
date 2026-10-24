"""Offline voice-first UI regressions; no provider calls or native UI required."""
import shutil
import unittest
import test_simple_mix_ui as fixture


@unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
class VoiceRepairUiTests(unittest.TestCase):
    prefix = fixture.SimpleMixUiTests.prefix
    run_node = fixture.SimpleMixUiTests.run_node

    def script(self, body):
        return self.prefix() + "\n(async()=>{await flush();\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"

    def test_preflight_result_and_checked_time_survive_compact_summary(self):
        self.run_node(self.script(r'''
data.preflight={status:'ready',checked_at:1792800000,message:'免费预检通过；不包含远端账号余额、网络可用性或整条素材无损保证'};
data.jobs=[{id:'pre',kind:'preflight',status:'completed',created_at:1,message:'处理完成',result:{}}];
timer();await flush();
assert.match($('preflight-status').textContent,/不包含远端账号余额/);
assert.match($('preflight-status').textContent,/检查时间/);
assert.ok(!$('preflight-status').textContent.endsWith('处理完成'));
assert.equal(writes().length,0);
// A newer inline generate/produce preflight replaces an old standalone failure.
data.jobs[0].status='failed';data.jobs[0].error='较旧的失败';timer();await flush();
assert.match($('preflight-status').textContent,/不包含远端账号余额/);
data.jobs[0].created_at=1792800001;timer();await flush();
assert.match($('preflight-status').textContent,/较旧的失败/);
'''))

    def test_preflight_locks_recheck_and_has_correct_reason(self):
        self.run_node(self.script(r'''
$('select-all').checked=true;event($('select-all'),'change');
data.drafts=[{id:'w',workflow:'script_first_v2',production_mode:'whole_voice_v1',target_seconds:25,
voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[]}];
data.jobs=[{id:'pre',kind:'preflight',status:'running',created_at:2,message:'检查配置'}];
timer();await flush();assert.equal($('run-preflight').disabled,true);
assert.match($('render-action-reason').textContent,/免费预检/);
assert.ok(!$('render-action-reason').textContent.includes('画面匹配正在进行'));
data.jobs[0].status='failed';timer();await flush();assert.equal($('run-preflight').disabled,false);
'''))

    def test_whole_voice_empty_timeline_and_legacy_migration_are_explicit(self):
        self.run_node(self.script(r'''
data.drafts=[{id:'old',workflow:'script_first_v2',production_mode:'legacy_v2',desktop_production_mode:'whole_voice_v1',
target_seconds:25,voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[]}];
timer();await flush();assert.equal(writes().length,0);
assert.equal($('whole-migration-notice').hidden,false);
assert.match($('whole-migration-notice').textContent,/旧分段配音缓存/);
assert.match($('whole-migration-notice').textContent,/确认/);
const timelineText=$('timeline-summary').children.map(node=>node.textContent).join(' ');
assert.ok(!timelineText.includes('点击“自动匹配画面”'));
assert.match(timelineText,/整条配音/);
event($('render-draft'),'click');await flush();assert.match($('confirm-description').textContent,/旧分段配音缓存/);
assert.equal(writes().length,0);event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
'''))

    def test_render_and_alignment_tasks_lock_editor_and_save_until_finished(self):
        self.run_node(self.script(r'''
data.drafts=[{id:'w',workflow:'script_first_v2',production_mode:'whole_voice_v1',target_seconds:25,
voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[],
whole_audio:{key:'key',duration:2,path:'ignored'}}];
for(const kind of ['render','alignment']){
 data.jobs=[{id:'job',kind,status:'running',created_at:1,draft_id:'w'}];timer();await flush();
 assert.equal(document.getElementById('draft-voiceover').readOnly,true);
 assert.equal($('save-draft').disabled,true);assert.equal($('realign-whole').disabled,true);
 assert.match($('render-action-reason').textContent,kind==='render'?/本条视频正在生成/:/本地对齐/);
}
data.jobs=[];timer();await flush();assert.equal(document.getElementById('draft-voiceover').readOnly,false);
assert.equal($('realign-whole').disabled,false);assert.equal(writes().length,0);
event($('realign-whole'),'click');await flush();
assert.equal(writes().at(-1).path,'/api/v2/drafts/w/realign');
assert.deepEqual(writes().at(-1).body,{refresh:true});
'''))

    def test_workflow_highlight_uses_current_three_step_labels(self):
        self.run_node(self.script(r'''
const originalQuery=document.querySelectorAll;
const steps=all.filter(n=>n.tagName==='li');
document.querySelectorAll=s=>s==='.workflow li'?steps:originalQuery(s);
data.drafts=[{id:'w',workflow:'script_first_v2',production_mode:'whole_voice_v1',target_seconds:25,
voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[]}];
timer();await flush();assert.equal(steps[1].className.includes('current'),true);
data.jobs=[{id:'pre',kind:'preflight',status:'running',created_at:1}];timer();await flush();
assert.equal(steps[0].className.includes('current'),true);
data.jobs=[{id:'r',kind:'render',status:'running',created_at:2,draft_id:'w'}];timer();await flush();
assert.equal(steps[2].className.includes('current'),true);
'''))

    def test_entering_workbench_navigates_before_preflight_submission(self):
        self.run_node(self.script(r'''
$('select-all').checked=true;event($('select-all'),'change');
event($('go-mix'),'click');
assert.equal($('page-mix').hidden,false);await flush();
assert.equal(writes().at(-1).path,'/api/v2/preflight');
'''))

    def test_oversized_copy_confirmation_is_advisory_and_cached_audio_is_not_rebought(self):
        self.run_node(self.script(r'''
data.drafts=[{id:'long',workflow:'script_first_v2',production_mode:'whole_voice_v1',target_seconds:60,
voiceover:'中'.repeat(300),estimated_seconds:75,segments:[{segment_id:'s1',text:'中'.repeat(300)}],match_status:'pending',clips:[],matches:[]}];
timer();await flush();assert.equal($('render-draft').disabled,false);
event($('render-draft'),'click');await flush();
assert.match($('confirm-description').textContent,/真实配音可能超过/);
assert.match($('confirm-description').textContent,/不会自动截断/);
assert.match($('confirm-description').textContent,/建议先精简/);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
data.drafts[0].whole_audio={key:'paid',duration:72,path:'cached.wav'};timer();await flush();
assert.match($('whole-duration').textContent,/超过 60 秒/);
assert.match($('whole-duration').textContent,/已保留/);
assert.equal($('render-draft').disabled,true);
assert.equal($('realign-whole').disabled,true);
'''))
