"""Consent and resume UI/HTTP boundary, offline DOM and authenticated Flask test client."""
import shutil, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from mixstudio.service import Studio
from mixstudio.http_app import create_app
import test_simple_mix_ui as ui

class BudgetRouteTests(unittest.TestCase):
    def test_read_only_budget_and_expected_token_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Studio(Path(tmp)/'r')
            try:
                app=create_app(s); c=app.test_client(); h={'X-Local-Token':app.config['LOCAL_TOKEN']}
                path='/api/v2/drafts/d/production-budget'
                with patch.object(s,'production_budget',create=True,return_value={'token':'ok'}) as budget:
                    self.assertEqual(c.get(path).status_code,403)
                    self.assertEqual(c.get(path+'?retry=1',headers=h).status_code,200)
                    budget.assert_called_once_with('d',retry=True)
                for url,method in [('/api/v2/drafts/d/produce','produce_whole'),('/api/v2/jobs/j/retry','retry_whole_job')]:
                    with self.subTest(url=url),patch.object(s,method,return_value={'id':'j'}) as call:
                        self.assertEqual(c.post(url,headers=h,json={'allow_paid':False,'expected_budget':'token'}).status_code,200)
                        self.assertEqual(call.call_args.kwargs['expected_budget'],'token')
                        self.assertIs(call.call_args.args[1],False)
                        self.assertEqual(c.post(url,headers=h,json={'allow_paid':True,'expected_budget':None}).status_code,400)
            finally:s.close()

@unittest.skipUnless(shutil.which('node'),'Node required for desktop UI fixture')
class BudgetUiTests(unittest.TestCase):
    prefix=ui.SimpleMixUiTests.prefix
    run_node=ui.SimpleMixUiTests.run_node
    base=r"""(async()=>{await flush();data.settings.fish_key_set=false;data.settings.deepseek_key_set=false;data.settings.glm_key_set=false;
data.drafts=[{id:'w',workflow:'script_first_v2',production_mode:'whole_voice_v1',revision:3,target_seconds:25,voiceover:'看看商品。',segments:[{segment_id:'s1',text:'看看商品。'}],match_status:'pending',clips:[],matches:[]}];
const budget={token:'token-xyz',revision:3,local_only:true,paid_steps:[],voice_state:'cached',matching_state:'cached',blocked:false,message:'配音和画面已保留，本次只在本地继续。'};
data.budget=budget; timer();await flush();"""
    end=r"""})().catch(e=>{console.error(e);process.exitCode=1;});"""
    def test_local_no_key_cancel_then_submit_free(self):
        self.run_node(self.prefix()+self.base+r"""
event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);assert.match($('confirm-description').textContent,/本地/);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
event($('render-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();
const req=writes()[0];assert.equal(req.path,'/api/v2/drafts/w/produce');assert.equal(req.body.allow_paid,false);assert.equal(req.body.expected_budget,'token-xyz');"""+self.end)
    def test_paid_step_list_no_unrelated_key_and_reject_bad_budget(self):
        self.run_node(self.prefix()+self.base+r"""
data.budget={...budget,token:null};event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,false);assert.equal(writes().length,0);
data.budget={...budget,local_only:false,paid_steps:['deepseek'],matching_state:'semantic',semantic_new_requests:1,semantic_cached_batches:2,message:'仅补 DeepSeek 匹配。'};
data.settings.deepseek_key_set=true;timer();await flush();event($('render-draft'),'click');await flush();
assert.equal($('confirm-dialog').open,true);assert.match($('confirm-description').textContent,/仅补 DeepSeek/);
assert.match($('confirm-description').textContent,/已缓存 2 批，本次最多新增 1 次/);assert.ok(!$('confirm-description').textContent.includes('GLM'));event($('confirm-accept'),'click');await flush();assert.equal(writes()[0].body.allow_paid,true);"""+self.end)
    def test_missing_audio_on_completed_task_requires_two_explicit_confirmations(self):
        self.run_node(self.prefix()+self.base+r"""
data.settings.fish_key_set=true;
data.budget={...budget,local_only:false,voice_state:'missing',blocked:true,paid_steps:['fish'],message:'文件缺失'};
data.retryBudget={...data.budget,token:'replace-token',blocked:false,message:'重新生成可能再次计费'};timer();await flush();
event($('render-draft'),'click');await flush();assert.match($('confirm-title').textContent,/缺失/);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
event($('render-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();
assert.match($('confirm-description').textContent,/再次计费/);assert.equal(writes().length,0);
event($('confirm-accept'),'click');await flush();const b=writes()[0].body;
assert.ok(b.allow_paid&&b.replace_missing_audio);assert.equal(b.expected_budget,'replace-token');"""+self.end)
    def test_archived_draft_retry_uses_budget_token(self):
        self.run_node(self.prefix()+self.base+r"""
data.drafts=[];data.jobs=[{id:'jobx',kind:'render',pipeline:'whole_voice_v1',draft_id:'w',status:'failed',progress:.5,created_at:1,error:'测试'}];timer();await flush();
const actions=$('mix-jobs').querySelectorAll('button');const retry=actions.find(x=>/继续|重试/.test(x.textContent));assert.ok(retry);event(retry,'click');await flush();
assert.ok(calls.some(x=>x.path==='/api/v2/drafts/w/production-budget?retry=1'));event($('confirm-accept'),'click');await flush();assert.deepEqual(writes()[0].body,{allow_paid:false,expected_budget:'token-xyz'});assert.equal(writes()[0].path,'/api/v2/jobs/jobx/retry');"""+self.end)
