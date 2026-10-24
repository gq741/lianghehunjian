"""Bounded polling and settings/storage UI regression tests; no paid calls."""
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import test_simple_mix_ui as simple

from mixstudio.service import Studio
from mixstudio.http_app import create_app


class CompactStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.studio=Studio(self.root/'runtime')
    def tearDown(self):self.studio.close();self.temp.cleanup()

    def seed_jobs(self, count=1000):
        with self.studio.store.connection() as db:
            rows=[]
            for i in range(count):
                job={'id':f'j-{i}', 'created_at':i, 'kind':'render' if i%2 else 'download',
                    'payload':{'draft_id':'d1'},'status':'completed','progress':1,'error':'','message':'处理完成',
                    'result':{'duration':25,'published':True,'timeline_plan':{'windows':[{'text':'x'*80}]*100}}}
                rows.append((job['id'],json.dumps(job)))
            db.executemany('INSERT INTO jobs(id,data) VALUES(?,?)',rows)

    def test_compact_jobs_bounded_sql_summaries_and_authenticated_details(self):
        self.seed_jobs()
        self.studio.store.update_job('j-1',status='running')
        original=self.studio.store._all
        with patch.object(self.studio.store,'_all',side_effect=lambda table:original(table) if table!='jobs' else (_ for _ in ()).throw(AssertionError('unbounded read'))):
            state=self.studio.state(compact=True)
        self.assertEqual(len(state['jobs']),41)
        self.assertTrue(any(j['id']=='j-1' for j in state['jobs']))
        self.assertTrue(all('timeline_plan' not in (j.get('result') or {}) for j in state['jobs']))
        self.assertLess(len(json.dumps(state).encode()),35000)
        app=create_app(self.studio);client=app.test_client()
        self.assertEqual(client.get('/api/jobs/j-999').status_code,403)
        full=client.get('/api/jobs/j-999',headers={'X-Local-Token':app.config['LOCAL_TOKEN']})
        self.assertIn('timeline_plan',full.json['data']['result']);self.assertNotIn('payload',full.json['data'])

    def test_unchanged_drafts_not_sent_and_active_older_draft_preserved(self):
        for i in range(16):
            self.studio.store._put('drafts',f'd-{i}',{'id':f'd-{i}','created_at':i,'title':f'文案{i}'})
        first=self.studio.state(compact=True)
        self.assertEqual(len(first['drafts']),10)
        next_state=self.studio.state(compact=True,drafts_token=first['drafts_token'])
        self.assertNotIn('drafts',next_state)
        editing=self.studio.state(compact=True,drafts_token=first['drafts_token'],draft_id='d-0')
        self.assertEqual(len(editing['drafts']),11)
        self.assertIn('d-0',[d['id'] for d in editing['drafts']])
        changed=self.studio.store.draft('d-15');changed['title']='新内容';self.studio.store.put_draft(changed)
        refreshed=self.studio.state(compact=True,drafts_token=first['drafts_token'])
        self.assertEqual(refreshed['drafts'][0]['title'],'新内容')
        self.assertEqual(len(self.studio.state()['drafts']),16)

    def test_compact_http_opt_in_and_cleaning_guard(self):
        app=create_app(self.studio);client=app.test_client();headers={'X-Local-Token':app.config['LOCAL_TOKEN']}
        normal=client.get('/api/state',headers=headers).json['data'];self.assertNotIn('compact',normal)
        first=client.get('/api/state',headers={**headers,'X-State-Mode':'compact'}).json['data']
        self.assertTrue(first['compact'])
        second=client.get('/api/state',headers={**headers,'X-State-Mode':'compact','X-Drafts-Token':first['drafts_token']}).json['data']
        self.assertNotIn('drafts',second)
        self.studio.storage_cleaning=True
        response=client.post('/api/v2/generate',json={'allow_paid':True},headers=headers)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.studio.store.jobs(),[])

    def test_progress_updates_throttled_without_losing_final_state(self):
        def work(event, progress):
            for i in range(1000):progress(i/1000,'相同渲染阶段')
            return {'fixture':True}
        with patch.object(self.studio.store,'update_job',wraps=self.studio.store.update_job) as update:
            job=self.studio.jobs.submit('scan',{},work)
            deadline=time.time()+3
            while self.studio.store.job(job['id'])['status'] in {'queued','running'} and time.time()<deadline:
                time.sleep(.01)
            self.studio.jobs.shutdown()
            progress=[call for call in update.call_args_list if 'progress' in call.kwargs and 'status' not in call.kwargs]
        self.assertLess(len(progress),10)
        self.assertEqual(self.studio.store.job(job['id'])['status'],'completed')
        self.assertEqual(self.studio.store.job(job['id'])['progress'],1)

    def test_draft_token_changes_when_busy_task_ends_without_a_draft_write(self):
        self.studio.store.put_draft({'id':'d1','created_at':1,'title':'已有文案'})
        self.studio.store.create_job('busy','match',{'draft_id':'d1'})
        before=self.studio.state(compact=True)
        self.studio.store.update_job('busy',status='failed')
        after=self.studio.state(compact=True,drafts_token=before['drafts_token'])
        self.assertIn('drafts',after)
        self.assertNotEqual(before['drafts_token'],after['drafts_token'])

    def test_produce_budget_reuses_saved_candidates_and_match_errors_remain_specific(self):
        from mixstudio.providers import ProviderError
        self.studio.store.put_asset({'id':'source','sha256':'a'*64,'selectable':True})
        draft={'id':'d1','created_at':1,'workflow':'script_first_v2','asset_ids':['source'],
            'revision':1,'product_name':'测试商品','segments':[{'segment_id':'s1','text':'测试'}],
            'clips':[{'usable':True}],'analysis_summary':{'complete':True},'match_candidates':[]}
        self.studio.store.put_draft(draft)
        with patch('mixstudio.script_engine.match_budget',side_effect=AssertionError('unneeded semantic cache read')):
            result=self.studio.matching_budget('d1','produce')
        self.assertEqual(result['new_request_count'],0)
        app=create_app(self.studio);headers={'X-Local-Token':app.config['LOCAL_TOKEN']}
        with patch('mixstudio.script_engine.match_budget',side_effect=ProviderError('语义匹配缓存校验失败',code='invalid_cache')):
            response=app.test_client().get('/api/v2/drafts/d1/matching-budget',headers=headers)
        self.assertEqual(response.status_code,400);self.assertIn('缓存校验失败',response.json['error'])

    def test_write_admission_shares_cleanup_lock_and_always_releases(self):
        app=create_app(self.studio);headers={'X-Local-Token':app.config['LOCAL_TOKEN']}
        entered=threading.Event();result=[]
        def work():
            response=app.test_client().post('/api/v2/generate',json={'allow_paid':True},headers=headers)
            result.append(response.status_code)
        with patch.object(self.studio,'generate_v2',side_effect=lambda payload:entered.set() or {'id':'fixture'}):
            with self.studio.action_lock:
                thread=threading.Thread(target=work);thread.start()
                self.assertFalse(entered.wait(.05))
            thread.join(2)
        self.assertEqual(result,[200]);self.assertTrue(entered.is_set())
        with self.studio.action_lock:pass


class SixUiTests(unittest.TestCase):
    run_node=simple.SimpleMixUiTests.run_node
    prefix=simple.SimpleMixUiTests.prefix

    def test_save_settings_does_not_overwrite_workbench_and_blank_numbers_do_not_submit(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
$('target-seconds').value='40';$('output-count').value='3';
event($('settings-form'),'submit');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/settings');
assert.equal($('target-seconds').value,'40');assert.equal($('output-count').value,'3');
$('set-fps').value='';event($('settings-form'),'submit');await flush();
assert.equal(writes().length,1);assert.match($('toast-region').children[0].textContent,/数字设置不能为空/);
console.log('SETTINGS_WORKBENCH_AND_EMPTY_NUMBERS=PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_storage_cancel_no_writes_and_recovery_notice(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
data.settings.settings_recovery={needed:true,message:'设置异常字段已恢复默认，旧文件已备份'};
timer();await flush();assert.equal($('settings-recovery').hidden,false);
assert.match($('settings-recovery').textContent,/旧文件已备份/);
event($('clean-storage'),'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.match($('confirm-description').textContent,/本次不清理配音、识别和转码缓存/);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
console.log('STORAGE_CANCEL_AND_SETTINGS_RECOVERY=PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_active_script_disables_duplicate_ui_submit(self):
        self.run_node(self.prefix()+r"""
(async()=>{await flush();
let box=$('asset-grid').children[0].querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
data.jobs=[{id:'active-script',kind:'script',status:'running',created_at:1}];timer();await flush();
assert.equal($('prepare-button').disabled,true);assert.equal(writes().length,0);
console.log('SCRIPT_DOUBLE_SUBMIT_UI=PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
