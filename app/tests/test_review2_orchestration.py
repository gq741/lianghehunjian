"""B03/B04 regressions. Local fixtures only; no network/provider calls."""
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from mixstudio.jobs import JobManager
from mixstudio.store import Store
from mixstudio.http_app import create_app

class Review2JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'state.sqlite')
        self.jobs=JobManager(self.store,workers=1)
        self.addCleanup(self.jobs.shutdown)
    def wait(self,job):
        for _ in range(300):
            current=self.store.job(job['id'])
            if current['status'] in {'completed','cancelled','failed'}:return current
            time.sleep(.01)
        self.fail('job did not terminate')
    def test_successfully_published_render_survives_late_cancel(self):
        output=self.root/'out.mp4'
        def work(event,progress):
            output.write_bytes(b'validated-result-fixture');event.set()
            return {'path':str(output),'duration':1,'published':True}
        result=self.wait(self.jobs.submit('render',{},work))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['result']['path'],str(output))
    def test_missing_output_does_not_turn_late_cancel_into_success(self):
        def work(event,progress):
            event.set();return {'path':str(self.root/'missing.mp4'),'published':True}
        self.assertEqual(self.wait(self.jobs.submit('render',{},work))['status'],'cancelled')
    def test_failed_work_does_not_become_success_even_with_existing_file(self):
        def work(event,progress):
            (self.root/'bad.mp4').write_bytes(b'incomplete');event.set();raise ValueError('failed before result')
        self.assertEqual(self.wait(self.jobs.submit('render',{},work))['status'],'cancelled')
    def test_cancel_queued_job_is_immediately_terminal_and_never_executes(self):
        gate=threading.Event();started=threading.Event();calls=[]
        self.addCleanup(gate.set)
        def work(e,p):started.set();gate.wait(5)
        self.jobs.submit('scan',{},work);self.assertTrue(started.wait(2))
        queued=self.jobs.submit('match',{'draft_id':'draft'},lambda e,p:calls.append(True))
        cancelled=self.jobs.cancel(queued['id'])
        self.assertEqual(cancelled['status'],'cancelled')
        self.assertNotIn(queued['id'],self.jobs.events)
        gate.set();self.jobs.shutdown()
        self.assertEqual(calls,[])
        self.assertEqual(self.store.job(queued['id'])['status'],'cancelled')
    def test_shutdown_cancels_pending_future_before_waiting_for_running_work(self):
        gate=threading.Event();started=threading.Event();self.addCleanup(gate.set)
        def work(e,p):started.set();gate.wait(5)
        self.jobs.submit('scan',{},work);self.assertTrue(started.wait(2))
        queued=self.jobs.submit('match',{},lambda e,p:None)
        closer=threading.Thread(target=self.jobs.shutdown);closer.start()
        try:
            deadline=time.monotonic()+1
            while self.store.job(queued['id'])['status']=='queued' and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.store.job(queued['id'])['status'],'cancelled')
        finally:gate.set();closer.join(5)

class Review2RenderCommitTests(unittest.TestCase):
    def test_queued_match_cancellation_immediately_releases_http_draft_edit_lock(self):
        from mixstudio.service import Studio
        with tempfile.TemporaryDirectory() as tmp:
            studio=Studio(Path(tmp)/'runtime');gate=threading.Event();calls=[]
            try:
                starts=[threading.Event() for _ in range(4)]
                def blocker(start):
                    def work(e,p):start.set();gate.wait(10)
                    return work
                for start in starts:studio.jobs.submit('scan',{},blocker(start))
                self.assertTrue(all(s.wait(2) for s in starts))
                text='商品画面'*25
                studio.store.put_draft(dict(id='target',workflow='script_first_v2',revision=1,status='draft',
                    voiceover=text,segments=[dict(segment_id='s1',text=text)],target_seconds=25,created_at=1,
                    asset_ids=[],clips=[],matches=[],match_status='pending'))
                queued=studio.jobs.submit('match',{'draft_id':'target','revision':1},lambda e,p:calls.append(True))
                client=create_app(studio,token='local-fixture').test_client();headers={'X-Local-Token':'local-fixture'}
                response=client.post('/api/jobs/'+queued['id']+'/cancel',headers=headers)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json['data']['status'],'cancelled')
                edit=client.put('/api/v2/drafts/target',headers=headers,json={'voiceover':text})
                self.assertEqual(edit.status_code,200,edit.json);self.assertEqual(calls,[])
            finally:gate.set();studio.close()

    def test_real_published_mp4_remains_registered_after_cancel(self):
        from test_v2_workflow import V2WorkflowTests
        from mixstudio import media
        case=V2WorkflowTests('test_script_first_real_25_second_export_uses_only_matched_windows_and_cached_voice')
        case.setUp()
        try:
            identifier=case.make_matched_draft()
            published=threading.Event();release=threading.Event();original=media._publish
            def hook(source,destination):
                original(source,destination);published.set()
                if not release.wait(30):raise RuntimeError('release timeout')
            with patch.object(media,'_publish',side_effect=hook):
                job=case.studio.render(identifier,True)
                try:
                    self.assertTrue(published.wait(40));case.studio.jobs.cancel(job['id'])
                finally:release.set()
                result=case.wait(job)
            self.assertTrue(result['result']['published'])
            self.assertTrue(Path(result['result']['path']).is_file())
            client=create_app(case.studio,token='local-fixture').test_client();client.get('/')
            response=client.get('/media/outputs/'+job['id']);self.assertEqual(response.status_code,200);response.close()
            self.assertEqual(len(list((case.root/'测试成片').glob('*.mp4'))),1)
            case.network.assert_not_called()
        finally:case.doCleanups()

if __name__=='__main__':unittest.main()
