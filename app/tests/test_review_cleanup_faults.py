"""Cleanup fault injection: terminal task status must never leak render slots."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from mixstudio import providers as real_providers
from mixstudio.jobs import Cancelled
from mixstudio.providers import ProviderError
import test_service_review as fixture

class CleanupFaultReviewTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.ServiceReviewTests(methodName='test_parallel_shared_sentence_posts_once_and_reuses_validated_cache')
        self.case.setUp();self.addCleanup(self.case.tearDown)
        self.case.drafts();self.case.studio.settings.update({'render_concurrency':1})

    def run_fault(self,mode,exception_type=OSError):
        c=self.case;work_root=c.studio.root/'work';original_resolve=Path.resolve
        fault={'ready':False,'count':0}
        def resolve(path,*args,**kwargs):
            if fault['ready'] and (path==work_root or path.parent==work_root):
                fault['count']+=1
                raise exception_type('injected cleanup path resolution failure')
            return original_resolve(path,*args,**kwargs)
        def voice(text,output,**kwargs):
            if mode=='unknown':
                fault['ready']=True
                raise ProviderError('primary unknown result fixture',code='network',result_unknown=True)
            Path(output).write_bytes(b'mock-voice')
            return {'path':str(output),'duration':1.}
        def render(clips,voice,output,settings,**kwargs):
            fault['ready']=True
            if mode=='failed':raise ValueError('primary render failure fixture')
            if mode=='cancelled':
                kwargs['cancel'].set();raise Cancelled('primary cancel fixture')
            Path(output).write_bytes(b'mock-output')
            return {'path':str(output),'duration':1.,'warnings':[]}
        media=SimpleNamespace(probe=lambda path:{'duration':1.,'has_audio':True},render_video=render)
        providers=SimpleNamespace(synthesize_voice=voice,ProviderError=ProviderError)
        with patch.object(c.studio,'_modules',return_value=(media,providers)),patch.object(c.studio,'_concat_voice',side_effect=c.concat_fixture),patch.object(Path,'resolve',new=resolve),patch.object(real_providers,'_http_open',side_effect=AssertionError('network forbidden')) as network:
            job=c.wait_job(c.studio.render('draft-1',True)['id'])
            self.assertEqual(c.studio.render_active,0,'cleanup faults must release the admission slot')
            self.assertGreater(fault['count'],0,'must exercise cleanup path failure')
            self.assertNotIn('cleanup path',job.get('error',''),'must not replace primary result/error')
            network.assert_not_called()
        self.assertEqual(job['status'],{'success':'completed','failed':'failed','cancelled':'cancelled','unknown':'failed'}[mode])
        if mode=='success':self.assertTrue(Path(job['result']['path']).is_file())
        if mode=='failed':self.assertIn('primary render failure fixture',job['error'])
        if mode=='unknown':
            self.assertTrue(job.get('result_unknown'))
            self.assertIn('primary unknown result fixture',job['error'])
            self.assertEqual(len(list((c.studio.root/'voice-cache').glob('*.pending.json'))),1)
        else:self.assertEqual(len(list((c.studio.root/'voice-cache').glob('*.wav'))),1)
        self.assertTrue(c.source.is_file())

    def test_success_kept_when_work_resolve_raises_oserror(self):self.run_fault('success')
    def test_primary_failure_kept_when_work_resolve_raises_oserror(self):self.run_fault('failed')
    def test_cancel_kept_when_work_resolve_raises_oserror(self):self.run_fault('cancelled')
    def test_unknown_provider_fence_kept_when_work_resolve_raises_oserror(self):self.run_fault('unknown')
    def test_primary_failure_kept_when_work_resolve_raises_runtimeerror(self):self.run_fault('failed',RuntimeError)

if __name__=='__main__':unittest.main()
