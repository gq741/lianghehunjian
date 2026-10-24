"""Offline UI structure and helper tests; not a browser or native-window claim."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.inputs = {}
        self.assets = []
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if "id" in item:
            self.ids.append(item["id"])
        if tag == "label" and "for" in item:
            self.labels.append(item["for"])
        if tag in {"input", "textarea", "select"}:
            self.inputs[item.get("id")] = item
        if tag == "script" and "src" in item:
            self.assets.append(item["src"])
        if tag == "link" and item.get("rel") == "stylesheet":
            self.assets.append(item["href"])
        if tag == "meta" and "name" in item:
            self.meta[item["name"]] = item.get("content")


class FrontendStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text("utf-8")
        cls.js = (WEB / "app.js").read_text("utf-8")
        cls.css = (WEB / "styles.css").read_text("utf-8")
        cls.markup = Markup()
        cls.markup.feed(cls.html)

    def test_local_assets_exist_at_contract_routes(self):
        self.assertEqual(set(self.markup.assets), {"/app.js", "/styles.css"})
        self.assertTrue(all((WEB / name.lstrip("/")).is_file() for name in self.markup.assets))

    def test_session_token_is_only_placeholder_in_source(self):
        self.assertEqual(self.markup.meta["local-token"], "__LOCAL_TOKEN__")
        self.assertIn('"X-Local-Token": token', self.js)

    def test_no_duplicate_ids_or_broken_explicit_labels(self):
        self.assertEqual(len(self.markup.ids), len(set(self.markup.ids)))
        self.assertTrue(set(self.markup.labels).issubset(self.markup.ids))

    def test_static_javascript_element_references_exist(self):
        referenced = set(re.findall(r'\$\("([^"]+)"\)', self.js))
        self.assertEqual(referenced - set(self.markup.ids), set())

    def test_secret_inputs_never_echo_or_persist_client_side(self):
        for name in ("deepseek", "fish", "glm"):
            item = self.markup.inputs[f"set-{name}_api_key"]
            self.assertEqual(item["type"], "password")
            self.assertNotIn("value", item)
        self.assertNotIn("localStorage", self.js)
        self.assertNotIn("sessionStorage", self.js)
        self.assertNotIn("console.log", self.js)

    def test_no_remote_assets_or_unsafe_html_injection(self):
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)
        self.assertNotIn("@import", self.css)
        self.assertNotRegex(self.html, r'(?:src|href)="https?://')

    def test_native_bridge_and_manual_paths_exist(self):
        self.assertIn('"pywebviewready"', self.js)
        self.assertIn('"choose_folder"', self.js)
        self.assertIn('choose_music_folder', self.js)
        self.assertIn("library-folder", self.markup.inputs)

    def test_default_duration_quantity_and_accessibility(self):
        self.assertEqual(self.markup.inputs["target-seconds"]["value"], "25")
        self.assertEqual(self.markup.inputs["output-count"]["value"], "1")
        self.assertIn('lang="zh-CN"', self.html)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn('aria-live="polite"', self.html)

    def test_paid_stages_and_draft_identity_are_explicit(self):
        self.assertIn('confirmAction("生成口播文案？"', self.js)
        self.assertIn('confirmAction("分析素材并匹配画面？"', self.js)
        self.assertIn('confirmAction("确认文案并生成视频？"', self.js)
        self.assertIn("clip_id: input.dataset.clipId", self.js)
        self.assertIn("batchDrafts()", self.js)
        self.assertNotIn("renderVideos(state.drafts)", self.js)

    def test_form_ranges_match_backend_settings(self):
        from mixstudio.settings import Settings
        validator = object.__new__(Settings)
        for identifier, attrs in self.markup.inputs.items():
            if not identifier or not identifier.startswith("set-") or attrs.get("type") != "number":
                continue
            for bound in ("min", "max"):
                if bound in attrs:
                    value = float(attrs[bound])
                    validator._validate(identifier[4:], int(value) if value.is_integer() else value)

    def test_download_paste_and_pending_input_are_preserved(self):
        self.assertIn('$("download-link").addEventListener("paste"', self.js)
        self.assertIn('input.setRangeText(normalizeLinkPaste(text)', self.js)
        self.assertIn('if ($("download-link").value === submittedText)', self.js)
        self.assertIn("state.assets = Array.isArray(data.assets) ? data.assets : []", self.js)

    def test_v2_tabs_multiline_download_and_limits(self):
        self.assertIn('role="tablist"', self.html)
        self.assertIn('id="download-link" rows="4"', self.html)
        self.assertIn('id="clear-download" type="button"', self.html)
        self.assertIn('id="queue-download" type="submit"', self.html)
        self.assertIn('解析并下载', self.html)
        self.assertNotIn('product-name', self.markup.ids)
        self.assertEqual(self.markup.inputs['target-seconds']['max'], '60')
        self.assertEqual(self.markup.inputs['output-count']['max'], '5')
        self.assertIn('const folder = state.settings.download_dir;', self.js)
        self.assertIn('state.product = data.product || null', self.js)

    def test_v2_endpoints_and_script_hidden(self):
        for route in ('/api/product/read', '/api/v2/generate', '/match', '/matches'):
            self.assertIn(route, self.js)
        self.assertNotIn('api("/api/prepare"', self.js)
        self.assertIn('draft.voiceover', self.js)
        self.assertNotIn('segment.visual_hint', self.js)
        self.assertIn('clip.usable === true', self.js)
        self.assertIn('state.matchesDirty', self.js)
        self.assertIn('state.productInputDirty', self.js)
        self.assertIn('["prepare", "product", "script", "match", "render", "media_prepare", "preflight"]', self.js)



@unittest.skipUnless(shutil.which("node"), "Node is needed only for optional frontend helper tests")
class FrontendHelperTests(unittest.TestCase):
    def run_js(self, expression):
        code = "const assert=require('node:assert/strict'); const u=require(process.argv[1]);" + expression
        result = subprocess.run(
            ["node", "-e", code, str(WEB / "app.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_javascript_syntax(self):
        result = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_time_format(self):
        self.run_js("assert.equal(u.formatTime(65.9),'01:05');assert.equal(u.formatTime(-1),'--:--');assert.equal(u.formatTime(NaN),'--:--');")

    def test_transcript_parses_and_preserves_pipes(self):
        self.run_js("assert.deepEqual(u.parseTranscript('0 | 2.5 | a|b\\n2.5|4|c',5),[{start:0,end:2.5,text:'a|b'},{start:2.5,end:4,text:'c'}]);")

    def test_transcript_rejects_invalid_ranges(self):
        self.run_js("""
            for(const value of ['', '0|0|text','-1|2|text','0|NaN|text','0|7|text',
            '0|2|','|2|text','0|3|a\\n2|4|b']) assert.throws(()=>u.parseTranscript(value,5));
        """)

    def test_links_accept_share_text_and_dedupe(self):
        self.run_js("""
            assert.deepEqual(u.extractDouyinLinks('分享 https://v.douyin.com/test/ 复制 https://v.douyin.com/test/'),
            ['https://v.douyin.com/test/']);
        """)

    def test_links_reject_wrong_hosts_and_homepages(self):
        self.run_js("""
            for(const value of ['https://douyin.com.evil.test/video/1','https://evil.test/',
            'https://www.douyin.com/user/abc','https://a:b@v.douyin.com/x','not a URL'])
            assert.throws(()=>u.extractDouyinLinks(value));
        """)

    def test_multiline_paste_and_tracking_query_deduplicate(self):
        self.run_js("""
            for(const separator of ['\\n', '\\r\\n', '\\r']) {
                const pasted=u.normalizeLinkPaste(
                  'https://v.douyin.com/abc/?share=1'+separator+
                  'https://v.douyin.com/def/'+separator+
                  'https://v.douyin.com/abc/#tracking');
                assert.equal(pasted.split("\\n").length, 3);
                assert.ok(!/\\r/.test(pasted));
                assert.deepEqual(u.extractDouyinLinks(pasted),
                    ['https://v.douyin.com/abc/','https://v.douyin.com/def/']);
            }
        """)

    def test_share_text_exact_user_format_and_strict_paths(self):
        self.run_js("""
            assert.equal(u.normalizeDouyinUrl(
                '9.79 复制打开抖音，看看【张百万的家🏠的作品】小米电视 ... '+
                'https://v.douyin.com/-M7p3VkY91I/ :1pm 12/03 H@V.YZ yGV:/'),
                'https://v.douyin.com/-M7p3VkY91I/');
            for(const value of ['http://v.douyin.com/abc/',
                'https://www.douyin.com/note/123456',
                'https://www.douyin.com/video/123456/extra',
                'https://v.douyin.com/abc/../def/',
                'https://v.douyin.com/abc/ https://evil.test/x']) {
                assert.throws(()=>u.extractDouyinLinks(value));
            }
        """)

    def test_sort_newest_without_mutation(self):
        self.run_js("""
            const input=[{id:'a',imported_at:100},{id:'b',imported_at:200}];
            assert.equal(u.sortedAssets(input)[0].id,'b');assert.equal(input[0].id,'a');
            assert.equal(u.timestamp(100),100000);
        """)

    def test_media_urls_are_same_origin_and_allowlisted(self):
        self.run_js("""
            const base='http://127.0.0.1:4200/';
            assert.equal(u.safeMediaUrl('/media/assets/abc','/media/assets/',base),'/media/assets/abc');
            for(const url of ['https://evil.test/media/assets/x','javascript:alert(1)','/api/settings','file:///C:/secret'])
            assert.equal(u.safeMediaUrl(url,'/media/assets/',base),'');
        """)

    def test_library_filter_current_folder_and_download_independence(self):
        self.run_js(r"""
            const assets=[{id:'local',path:'E:/clips/a.mp4',libraries:['local']},
                {id:'other',path:'E:/other/b.mp4',libraries:['local']},
                {id:'dl',path:'F:/downloads/c.mp4',libraries:['douyin']},
                {id:'both',path:'E:/clips/d.mp4',libraries:['local','douyin']}];
            assert.deepEqual(u.libraryAssets(assets,'local','e:\\CLIPS\\').map(a=>a.id),['local','both']);
            assert.deepEqual(u.libraryAssets(assets,'douyin','E:/other').map(a=>a.id),['dl','both']);
            assert.deepEqual(u.libraryAssets(assets,'local',''),[]);
        """)

    def test_selection_survives_tabs_and_deduplicates_content(self):
        self.run_js("""
            const assets=[{id:'local',sha256:'same'},{id:'dl',sha256:'same'},{id:'other',sha256:'other'}];
            let ids=u.changeSelection(assets,new Set(),'local',true);
            ids=u.changeSelection(assets,ids,'other',true);
            assert.equal(ids.size,2);
            ids=u.changeSelection(assets,ids,'dl',true);
            assert.deepEqual([...ids],['other','dl']);
            assert.equal(u.selectedAssets(assets,new Set(['local','dl','other'])).length,2);
            ids=u.changeSelection(assets,ids,'local',false);
            assert.deepEqual([...ids],['other']);
            assert.deepEqual(u.selectedAssets(assets,new Set(['missing'])),[]);
        """)

    def test_soft_reference_bounds_and_ready_gate(self):
        self.run_js("""
            assert.deepEqual(u.durationBounds(25),{min:.1,max:60});
            assert.deepEqual(u.durationBounds(60),{min:.1,max:60});
            assert.deepEqual(u.durationBounds(5),{min:.1,max:60});
            for(const v of [4,61,25.5,NaN,'']) assert.throws(()=>u.durationBounds(v));
            assert.equal(u.canRenderDraft({workflow:'script_first_v2',match_status:'needs_review'}),false);
            assert.equal(u.canRenderDraft({workflow:'script_first_v2',match_status:'ready',timeline_plan:{version:1,status:'ready',shortages:[]},target_seconds:25,voiceover:'好'.repeat(100)}),true);
            assert.equal(u.canRenderDraft({workflow:'script_first_v2',match_status:'ready',target_seconds:25,voiceover:'短文',estimated_seconds:25,timing_status:'ready'}),false);
            assert.equal(u.canRenderDraft(null),false);
            assert.equal(u.canRenderDraft({segments:[]}),true);
        """)

    def test_timing_recounts_unicode_and_ignores_cached_estimate(self):
        self.run_js(r"""
            const draft={workflow:'script_first_v2',target_seconds:25,voiceover:'中A2\u{20000}Ⅲ²'.repeat(20)+'， 。！ \n👟',estimated_seconds:25,timing_status:'ready'};
            const timing=u.narrationTiming(draft);
            assert.equal(timing.units,120);assert.equal(timing.estimated,30);assert.equal(timing.ready,true);
            assert.equal(timing.minUnits,1);assert.equal(timing.maxUnits,240);
            assert.equal(u.narrationTiming(draft,'中'.repeat(121)).ready,true);
            assert.equal(u.narrationTiming(draft,'中'.repeat(121)).mode,'reference_only');
            assert.equal(u.narrationTiming(draft,'中'.repeat(181)).ready,true);
            assert.ok(u.narrationTiming(draft,'中'.repeat(181)).message.includes('仅作提示'));
            assert.ok(u.narrationTiming(draft,'中'.repeat(79)).message.includes('仅作提示'));
            assert.equal(u.narrationTiming({...draft,voiceover:'',segments:[{text:'中'.repeat(100)}]}).ready,false);
            assert.equal(u.narrationTiming({...draft,voiceover:undefined,segments:[{text:'中'.repeat(100)}]}).ready,true);
            assert.equal(u.canMatchDraft({...draft,target_seconds:60,voiceover:'中'.repeat(241)}),true);
            assert.equal(u.canMatchDraft({...draft,target_seconds:60,voiceover:'中'.repeat(361)}),true);
            assert.equal(u.canMatchDraft({...draft,target_seconds:60,voiceover:'中'.repeat(240)}),true);
            assert.equal(u.canMatchDraft({...draft,target_seconds:5,voiceover:''}),false);
            assert.equal(u.canMatchDraft({...draft,target_seconds:5,voiceover:'中'}),true);
            assert.equal(u.canMatchDraft({...draft,target_seconds:0}),false);
            assert.equal(u.canMatchDraft(null),false);
        """)


@unittest.skipUnless(shutil.which("node"), "Node is needed for isolated DOM event tests")
class FrontendEventTests(unittest.TestCase):
    """Minimal DOM + fake localhost API; executes production event handlers, not live APIs."""

    def test_v2_real_handlers_with_fake_dom_and_api(self):
        harness = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Node {
  constructor(tag='div', attrs={}) {
    this.tagName=tag;this.children=[];this.listeners={};this.attrs={...attrs};this.dataset={};
    this.id=attrs.id||'';this.className=attrs.class||'';this.value=attrs.value||'';this.checked=false;
    this.hidden='hidden' in attrs;this.disabled='disabled' in attrs;this.textContent='';this.open=false;
    for(const [key,value] of Object.entries(attrs)) if(key.startsWith('data-')) this.dataset[key.slice(5)]=value;
    this.classList={toggle:(name,force)=>{let set=new Set(this.className.split(' ').filter(Boolean));if(force===undefined)force=!set.has(name);force?set.add(name):set.delete(name);this.className=[...set].join(' ');},add:(name)=>this.classList.toggle(name,true),remove:(name)=>this.classList.toggle(name,false)};
  }
  append(...nodes){for(const node of nodes){if(node.tagName==='#fragment')this.append(...[...node.children]);else {node.remove();this.children.push(node);node.parentNode=this;}}}
  replaceChildren(...nodes){for(const child of [...this.children])child.remove();this.append(...nodes);}
  insertBefore(node,reference){if(node===reference)return node;node.remove();const index=reference?this.children.indexOf(reference):this.children.length;if(index<0)throw new Error('reference is not a child');this.children.splice(index,0,node);node.parentNode=this;return node;}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  removeAttribute(k){delete this.attrs[k];}
  addEventListener(k,f,opts){(this.listeners[k]||=[]).push({f,once:opts?.once});}
  dispatchEvent(event){for(const entry of [...(this.listeners[event.type]||[])]){entry.f(event);if(entry.once)this.listeners[event.type]=this.listeners[event.type].filter(x=>x!==entry);}return !event.defaultPrevented;}
  matches(selector){let m;if(selector.startsWith('.'))return this.className.split(' ').includes(selector.slice(1));if((m=/^\[data-([^\]]+)\]$/.exec(selector)))return m[1] in this.dataset;if(selector==='input[type="checkbox"]')return this.tagName==='input'&&(this.type==='checkbox'||this.attrs.type==='checkbox');if(selector==='fieldset.match-row')return this.tagName==='fieldset'&&this.className.includes('match-row');return this.tagName===selector;}
  querySelectorAll(selector){const out=[];for(const child of this.children){if(child.matches(selector))out.push(child);out.push(...child.querySelectorAll(selector));}return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  focus(){document.activeElement=this;}
  pause(){} load(){} play(){return Promise.resolve();} scrollIntoView(){} remove(){if(this.parentNode){const siblings=this.parentNode.children;siblings.splice(siblings.indexOf(this),1);}this.parentNode=null;}
  showModal(){this.open=true;}
  close(value){this.open=false;this.returnValue=value;this.dispatchEvent({type:'close'});}
  setRangeText(text,start,end){this.value=this.value.slice(0,start)+text+this.value.slice(end);}
}
const html=fs.readFileSync(process.argv[2],'utf8'), all=[], ids=new Map();
for(const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)){
  const attrs={};for(const m of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[m[1]]=m[2]||'';
  const node=new Node(match[1],attrs);all.push(node);if(node.id)ids.set(node.id,node);
}
const document={hidden:false,activeElement:null,documentElement:{dataset:{}},getElementById:(id)=>ids.get(id)||all.flatMap(n=>n.querySelectorAll('textarea')).find(n=>n.id===id),createElement:(tag)=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createDocumentFragment:()=>new Node('#fragment'),querySelector:(s)=>s.startsWith('meta')?{content:'test-token'}:document.querySelectorAll(s)[0],querySelectorAll:(selector)=>[...all.filter(n=>n.matches(selector)),...all.flatMap(n=>n.querySelectorAll(selector))],addEventListener:()=>{}};
const item=(id,sha,library,path)=>({id,name:id+'.mp4',sha256:sha,libraries:[library],path,duration:10,imported_at:100});
const data={assets:[item('local','shared','local','E:/source/a.mp4'),item('other','second','local','E:/source/b.mp4'),item('duplicate','shared','douyin','F:/download/a.mp4'),item('remote','third','douyin','F:/download/c.mp4')],folder:'E:/source',settings:{download_dir:'F:/download',deepseek_key_set:true,fish_key_set:true,glm_key_set:true},product:{title:'测试商品标题',sku:'123456',source:'https://item.jd.com/123456.html',status:'read'},jobs:[],drafts:[]};
const calls=[], window={scrollTo(){},addEventListener(){}};let timer, jobCounter=0, lastJobId='';
const fetch=async(path,options)=>{const body=options.body?JSON.parse(options.body):null;calls.push({path,method:options.method,body});let result={id:'fake-job'};if(path==='/api/product/read'||path.endsWith('/retry')){lastJobId='job-'+(++jobCounter);result={id:lastJobId};}if(options.method==='PUT'&&/^\/api\/v2\/drafts\/[^/]+$/.test(path)){const draft=data.drafts.find(d=>path.endsWith('/'+d.id));draft.voiceover=body.voiceover;draft.segments=[{segment_id:'s1',text:body.voiceover}];draft.match_status='pending';draft.matches=[];result=structuredClone(draft);}return {ok:true,json:async()=>({ok:true,data:path==='/api/state'?structuredClone(data):result})};};
const context={document,window,location:{hash:'',href:'http://127.0.0.1:12345/'},history:{replaceState(){}},fetch,URL,AbortController,Event:class {constructor(type){this.type=type;}},setTimeout:()=>1,clearTimeout(){},setInterval:(f)=>{timer=f;},console,Map,Set,Promise};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const $=id=>ids.get(id), event=(node,type,extra={})=>node.dispatchEvent({type,preventDefault(){this.defaultPrevented=true;},...extra});
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const writes=()=>calls.filter(c=>c.method!=='GET');
(async()=>{
 await flush();assert.equal($('connection-text').textContent,'本地服务已连接');
 assert.equal($('asset-grid').children.length,2);
 let box=$('asset-grid').children[0].querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
 event($('library-tab-douyin'),'click');assert.equal($('library-panel-local').hidden,true);assert.equal($('asset-grid').children.length,2);
 box=$('asset-grid').children[0].querySelector('input[type="checkbox"]');assert.equal(box.checked,true);
 box=$('asset-grid').children[1].querySelector('input[type="checkbox"]');box.checked=true;event(box,'change');
 assert.equal($('selection-count').textContent,'已选 2 个视频');
 event($('library-tab-local'),'click');assert.equal($('asset-grid').children[0].querySelector('input[type="checkbox"]').checked,true);
 $('select-all').checked=false;event($('select-all'),'change');assert.equal($('selection-count').textContent,'已选 1 个视频');
 // Enter has no submitting listener, paste preserves lines, clear never calls API.
 $('download-link').value='https://v.douyin.com/abc/\nhttps://v.douyin.com/def/';
 event($('download-link'),'keydown',{key:'Enter'});await flush();assert.equal(writes().length,0);
 event($('clear-download'),'click');assert.equal($('download-link').value,'');assert.equal(writes().length,0);assert.equal($('selection-count').textContent,'已选 1 个视频');
 $('download-link').value='https://v.douyin.com/abc/\nhttps://v.douyin.com/def/';
 event($('download-form'),'submit');await flush();assert.equal(writes().at(-1).path,'/api/downloads');assert.equal(writes().at(-1).body.folder,'F:/download');assert.equal(writes().at(-1).body.links.length,2);
 const prior=writes().length; $('target-seconds').value='60';event($('target-seconds'),'input');assert.match($('duration-hint').textContent,/参考时长.*最长 60 秒/);assert.equal(writes().length,prior);
 event($('prepare-form'),'submit');await flush();assert.equal($('confirm-dialog').open,true);assert.equal(writes().length,prior);
 event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,prior);
 event($('prepare-form'),'submit');await flush();event($('confirm-accept'),'click');await flush();assert.equal(writes().at(-1).path,'/api/v2/generate');assert.equal(writes().at(-1).body.allow_paid,true);assert.equal(writes().at(-1).body.count,1);
 // A reference-duration difference is advisory; a visual plan is still required.
 const overBudget='商品'.repeat(90)+'好。', readyVoice='商品'.repeat(50)+'。';
 data.drafts=[{id:'draft1',workflow:'script_first_v2',voiceover:overBudget,target_seconds:25,estimated_seconds:25,timing_status:'ready',match_status:'pending',segments:[{segment_id:'s1',text:overBudget,visual_hint:'INTERNAL_HINT_MUST_NOT_RENDER'}],clips:[],matches:[]}];
 data.jobs.push({id:'script-job',kind:'script',status:'completed',created_at:100,result:{draft_ids:['draft1'],needs_adjustment_ids:['draft1']}});
 timer();await flush();assert.equal($('draft-workspace').hidden,false);assert.equal($('render-draft').disabled,true);
 assert.equal(document.getElementById('draft-voiceover').value,overBudget);
 assert.equal($('draft-timing-status').hidden,false);assert.ok($('draft-timing-status').textContent.includes('仅作提示'));
 assert.ok(!$('draft-timing-status').textContent.includes('删减'));assert.ok($('draft-meta').textContent.includes('45.25 秒'));
 assert.equal($('mix-jobs').querySelector('.job-state').textContent,'已完成');
 assert.equal($('match-draft').disabled,false);const blockedWrites=writes().length;
 event($('match-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);event($('confirm-cancel'),'click');await flush();event($('render-draft'),'click');await flush();
 assert.equal(writes().length,blockedWrites);assert.equal($('confirm-dialog').open,false);
 // Editing recomputes immediately but paid matching waits for a successful save.
 let editor=document.getElementById('draft-voiceover');editor.value=readyVoice;event(editor,'input');
 assert.ok($('draft-meta').textContent.includes('25.00 秒'));assert.ok($('draft-timing-status').textContent.includes('仅作提示'));
 assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);
 timer();await flush();assert.equal(document.getElementById('draft-voiceover').value,readyVoice);
 event($('save-draft'),'click');await flush();assert.equal($('match-draft').disabled,false);
 assert.equal($('mix-jobs').querySelector('.job-state').textContent,'已完成');
 assert.ok(!$('draft-segments').textContent.includes('INTERNAL_HINT_MUST_NOT_RENDER'));
 const beforeMatch=writes().length;event($('match-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);assert.equal(writes().length,beforeMatch);
 assert.match($('confirm-description').textContent,/每条源视频最多 45 次视觉请求/);
 assert.match($('confirm-description').textContent,/一次比较全部文案段落（最多64段）/);assert.match($('confirm-description').textContent,/128 个候选窗/);assert.match($('confirm-description').textContent,/不会自动重试收费请求/);
 assert.match($('confirm-description').textContent,/有效缓存会复用/);
 assert.match($('confirm-description').textContent,/每批最多 2 个片段、6 张图/);
 event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,beforeMatch);
 event($('match-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();assert.equal(writes().at(-1).path,'/api/v2/drafts/draft1/match');
 // Retrying a failed paid analysis discloses the same batch budget before submission.
 data.jobs.unshift({id:'failed-match',kind:'match',status:'failed',created_at:500,error:'测试超时',result_unknown:true});timer();await flush();
 const retryMatch=$('mix-jobs').children[0].querySelectorAll('button').find(n=>n.textContent==='重试');assert.ok(retryMatch);
 const beforeRetry=writes().length;event(retryMatch,'click');await flush();assert.equal(writes().length,beforeRetry);
 assert.match($('confirm-description').textContent,/每条源视频最多 45 次视觉请求/);
 assert.match($('confirm-description').textContent,/一次比较全部文案段落（最多64段）/);assert.match($('confirm-description').textContent,/128 个候选窗/);assert.match($('confirm-description').textContent,/不会自动重试收费请求/);
 assert.match($('confirm-description').textContent,/有效缓存会复用/);
 assert.match($('confirm-description').textContent,/先核对服务端状态/);
 assert.match($('confirm-description').textContent,/本次不调用 Fish 配音/);
 event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,beforeRetry);
 event(retryMatch,'click');await flush();event($('confirm-accept'),'click');await flush();
 assert.equal(writes().at(-1).path,'/api/jobs/failed-match/retry');assert.equal(writes().at(-1).body.allow_paid,true);
 data.jobs=data.jobs.filter(job=>job.id!=='failed-match');timer();await flush();
 data.drafts[0].match_status='ready';data.drafts[0].timeline_plan={version:1,status:'ready',shortages:[]};data.drafts[0].clips=[{clip_id:'c1',asset_id:'remote',start:0,end:6,text:'商品特写',usable:true},{clip_id:'bad',asset_id:'remote',start:6,end:10,usable:false,reason:'黑屏'}];data.drafts[0].matches=[{segment_id:'s1',clip_ids:['c1']}];
 timer();await flush();assert.equal($('render-draft').disabled,false);assert.equal($('match-rows').querySelectorAll('input[type="checkbox"]').length,1);
 let matchBox=$('match-rows').querySelector('input[type="checkbox"]');matchBox.checked=false;event(matchBox,'change');
 const beforeEmptySave=writes().length;event($('save-matches'),'click');await flush();assert.equal(writes().length,beforeEmptySave+1);assert.deepEqual(writes().at(-1).body,{clip_table:[],matches:[{segment_id:'s1',clip_indices:[]}]});
 matchBox.checked=true;event(matchBox,'change');event($('save-matches'),'click');await flush();assert.equal(writes().at(-1).path,'/api/v2/drafts/draft1/matches');assert.deepEqual(writes().at(-1).body,{clip_table:['c1'],matches:[{segment_id:'s1',clip_indices:[0]}]});
 const beforeRender=writes().length;event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);assert.equal(writes().length,beforeRender);event($('confirm-cancel'),'click');await flush();
 // Unsaved voice changes inhibit render and survive polling.
 const voice=document.getElementById('draft-voiceover');voice.value='编辑后文案';event(voice,'input');assert.equal($('render-draft').disabled,true);timer();await flush();assert.equal(document.getElementById('draft-voiceover').value,'编辑后文案');
 event($('save-draft'),'click');await flush();assert.equal(writes().at(-1).path,'/api/v2/drafts/draft1');assert.equal(writes().at(-1).body.voiceover,'编辑后文案');
 assert.equal($('match-draft').disabled,false);assert.equal($('render-draft').disabled,true);assert.ok($('draft-timing-status').textContent.includes('仅作提示'));
 assert.equal($('draft-save-status').textContent,'文案已保存');
 // Editing product URL blocks accidentally generating for the preceding product.
 $('product-url').value='https://item.jd.com/999999.html';event($('product-url'),'input');assert.equal($('prepare-button').disabled,true);
 event($('product-form'),'submit');await flush();assert.equal(writes().at(-1).path,'/api/product/read');assert.equal($('prepare-button').disabled,true);
 data.jobs.push({id:lastJobId,kind:'product',status:'completed',created_at:200,result:{}});data.product={title:'新的真实标题',sku:'999999',source:'https://item.jd.com/999999.html',status:'read'};
 timer();await flush();assert.equal($('product-title').textContent,'新的真实标题');assert.equal($('prepare-button').disabled,false);
 $('product-url').value='https://item.jd.com/888888.html';event($('product-url'),'input');event($('product-form'),'submit');await flush();
 const failedId=lastJobId;data.jobs.unshift({id:failedId,kind:'product',status:'failed',created_at:300,error:'请完成商品登录'});timer();await flush();assert.equal($('prepare-button').disabled,true);
 const retry=$('mix-jobs').children[0].querySelectorAll('button').find(n=>n.textContent==='重试');assert.ok(retry);event(retry,'click');await flush();event($('confirm-accept'),'click');await flush();
 assert.equal(writes().at(-1).path,`/api/jobs/${failedId}/retry`);assert.equal(writes().at(-1).body.allow_paid,false);
 data.jobs.unshift({id:lastJobId,kind:'product',status:'completed',created_at:400});data.product={title:'重试读取的标题',sku:'888888',source:'https://item.jd.com/888888.html',status:'read'};timer();await flush();assert.equal($('prepare-button').disabled,false);assert.equal($('product-title').textContent,'重试读取的标题');
 // Old drafts still expose their original preview and edit identity, not a V2 match call.
 data.drafts.push({id:'legacy',title:'历史文案',target_seconds:25,segments:[{clip_id:'oldclip',text:'历史口播'}],clips:[{clip_id:'oldclip',asset_id:'remote',start:1,end:4}]});timer();await flush();
 $('draft-select').value='legacy';event($('draft-select'),'change');await flush();assert.equal($('match-draft').hidden,true);assert.equal($('match-review').hidden,false);assert.equal($('match-review-label').textContent,'预览历史画面');assert.ok($('match-rows').querySelectorAll('button').some(n=>n.textContent.includes('预览第 1 段')));
 assert.ok(calls.every(c=>c.path.startsWith('/api/')));console.log('V2_DOM_EVENTS=PASS; MOCK_API_ONLY; PAID_CALLS=0');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", harness, str(WEB / "app.js"), str(WEB / "index.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("V2_DOM_EVENTS=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
