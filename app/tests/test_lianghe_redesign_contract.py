"""Portable presentation contract for the approved Lianghe redesign.

DOM/forms/CSP/brand identity remain stable while authorized bug fixes evolve
the implementation. Behavioral regressions belong in executable workflow tests,
not permanent byte-for-byte locks on every business module.
"""
import ast
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = Path(__file__).with_name('fixtures')/'lianghe_frontend_contract.json'
OLD_BRAND = '京东自动混剪'
NEW_BRAND = '量禾混剪'
VOID_TAGS = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
BEHAVIOR_CLASSES = {'page','nav-button','choose-setting-folder','workflow'}
EMPTY_TARGETS = ('asset-grid','draft-segments','match-rows','timeline-summary',
    'music-candidates','music-tracks','download-jobs','mix-jobs','toast-region','storage-usage')


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf8')).hexdigest()


class DOM(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = {'tag':tag,'attrs':dict(attrs),'ancestors':list(self.stack),'children':[],'text':''}
        if self.stack:
            self.stack[-1]['children'].append(node)
        self.nodes.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index]['tag'] == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, value):
        for node in self.stack:
            node['text'] += value

    def by_id(self, identifier):
        return next(node for node in self.nodes if node['attrs'].get('id') == identifier)


def html_contract(html):
    dom = DOM(html)
    identified = [node for node in dom.nodes if node['attrs'].get('id')]
    controls = {}
    for node in identified:
        if node['tag'] in {'input','select','textarea','button'}:
            form = next((parent['attrs'].get('id') for parent in reversed(node['ancestors']) if parent['tag']=='form'),None)
            controls[node['attrs']['id']] = {'tag':node['tag'],'form':form,
                'attrs':{key:value for key,value in node['attrs'].items() if key not in {'class','style'}}}
    bindings = []
    for node in dom.nodes:
        attrs = node['attrs']
        classes = set(attrs.get('class','').split()) & BEHAVIOR_CLASSES
        data = {key:value for key,value in attrs.items() if key in {'data-page','data-library','data-for'}}
        if classes or data:
            bindings.append({'tag':node['tag'],'id':attrs.get('id'),'classes':sorted(classes),'data':data})
    workflow = next(node for node in dom.nodes if 'workflow' in node['attrs'].get('class','').split())
    steps = [re.sub(r'^\s*\d+\s*','',node['text']).strip() for node in dom.nodes if node['tag']=='li' and workflow in node['ancestors']]
    return {
        'ids':{node['attrs']['id']:node['tag'] for node in identified},
        'controls':controls,
        'bindings':sorted(bindings,key=lambda item:json.dumps(item,sort_keys=True)),
        'workflow':steps,
        'form_ids':sorted(node['attrs']['id'] for node in identified if node['tag']=='form'),
        'native_defaults':{node['attrs']['id']:{key:node['attrs'][key] for key in ('hidden','open','role','aria-live','aria-labelledby','aria-describedby') if key in node['attrs']} for node in identified},
        'labels':sorted(node['attrs']['for'] for node in dom.nodes if node['tag']=='label' and 'for' in node['attrs']),
        'script_assets':[node['attrs'] for node in dom.nodes if node['tag']=='script' and 'src' in node['attrs']],
        'stylesheet_assets':[node['attrs'] for node in dom.nodes if node['tag']=='link' and node['attrs'].get('rel')=='stylesheet'],
    }


def native_contract(text):
    tree = ast.parse(text)
    functions = {node.name:ast.dump(node.args,include_attributes=False) for node in ast.walk(tree)
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    cli = [ast.dump(node,include_attributes=False) for node in ast.walk(tree) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Attribute) and node.func.attr=='add_argument']
    return {'functions':functions,'cli':cli}


def csp_value(text):
    for node in ast.walk(ast.parse(text)):
        if isinstance(node,ast.Assign) and any(isinstance(target,ast.Subscript)
                and isinstance(target.slice,ast.Constant) and target.slice.value=='Content-Security-Policy'
                for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('CSP assignment missing')


def build_contract(source):
    source = Path(source)
    http = (source/'mixstudio/http_app.py').read_text('utf8')
    return {'version':2,'html':html_contract((source/'web/index.html').read_text('utf8')),
        'brand_assets':{name:sha((source/'web'/name).read_bytes()) for name in ('brand-logo.jpg','brand-icon.ico')},
        'csp':csp_value(http),
        'native':{name:native_contract((source/name).read_text('utf8')) for name in ('app.py','mixstudio/desktop.py')}}


class LiangheRedesignContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(SNAPSHOT.read_text('utf8'))
        cls.html = (ROOT/'web/index.html').read_text('utf8')
        cls.css = (ROOT/'web/styles.css').read_text('utf8')
        cls.js = (ROOT/'web/app.js').read_text('utf8')
        cls.dom = DOM(cls.html)
        cls.current = html_contract(cls.html)

    def test_all_original_dom_ids_and_element_types_are_unchanged(self):
        ids = [node['attrs']['id'] for node in self.dom.nodes if node['attrs'].get('id')]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(self.current['ids'],self.baseline['html']['ids'])

    def test_forms_controls_original_attributes_and_ownership_are_unchanged(self):
        self.assertEqual(self.current['form_ids'],self.baseline['html']['form_ids'])
        self.assertEqual(self.current['controls'],self.baseline['html']['controls'])
        self.assertEqual(self.current['labels'],self.baseline['html']['labels'])

    def test_navigation_bindings_and_three_workflow_steps_are_preserved(self):
        self.assertEqual(self.current['bindings'],self.baseline['html']['bindings'])
        self.assertEqual(len(self.current['workflow']),3)
        self.assertEqual(self.current['workflow'],self.baseline['html']['workflow'])

    def test_hidden_dialog_details_and_accessibility_defaults_are_preserved(self):
        self.assertEqual(self.current['native_defaults'],self.baseline['html']['native_defaults'])
        self.assertEqual(self.dom.by_id('confirm-dialog')['tag'],'dialog')
        self.assertEqual(self.dom.by_id('job-detail-dialog')['tag'],'dialog')
        self.assertEqual(self.dom.by_id('music-panel')['tag'],'details')
        self.assertRegex(self.css,r'\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important')

    def test_dynamic_targets_stay_empty_and_music_stays_in_workbench(self):
        for identifier in EMPTY_TARGETS:
            with self.subTest(identifier=identifier):
                node = self.dom.by_id(identifier)
                self.assertEqual(node['children'],[])
                self.assertEqual(node['text'].strip(),'')
        self.assertLess(self.html.index('id="prepare-form"'),self.html.index('id="music-panel"'))
        self.assertLess(self.html.index('id="music-panel"'),self.html.index('id="draft-workspace"'))
        card = self.dom.by_id('product-card')
        self.assertEqual([(node['tag'],node['attrs'].get('id')) for node in card['children']],[('strong','product-title')])

    def test_javascript_brand_and_literal_dom_bindings_remain_valid(self):
        self.assertIn(NEW_BRAND,self.js)
        self.assertNotIn(OLD_BRAND,self.js)
        bindings = set(re.findall(r'''\$\(["']([^"']+)["']\)''', self.js))
        self.assertTrue(bindings)
        self.assertEqual(bindings-set(self.current['ids']),set())

    def test_brand_assets_remain_the_exact_approved_user_images(self):
        actual = {name:sha((ROOT/'web'/name).read_bytes()) for name in self.baseline['brand_assets']}
        self.assertEqual(actual,self.baseline['brand_assets'])

    def test_http_authentication_and_csp_remain_enforced(self):
        from mixstudio.http_app import create_app
        text = (ROOT/'mixstudio/http_app.py').read_text('utf8')
        self.assertEqual(csp_value(text),self.baseline['csp'])
        calls = []
        studio = SimpleNamespace(storage_cleaning=False,
            scan_music=lambda folder: calls.append(folder) or {'candidates':[]})
        client = create_app(studio,web_root=ROOT/'web',token='visual-contract').test_client()
        for headers in ({}, {'X-Local-Token':'wrong'},
                        {'X-Local-Token':'visual-contract','Host':'evil.test'},
                        {'X-Local-Token':'visual-contract','Origin':'https://evil.test'}):
            response = client.post('/api/music/scan',json={'folder':'fixture'},headers=headers)
            self.assertEqual(response.status_code,403)
        self.assertEqual(calls,[])
        response = client.post('/api/music/scan',json={'folder':'fixture'},headers={'X-Local-Token':'visual-contract'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(calls,['fixture'])
        self.assertEqual(response.headers['Content-Security-Policy'],self.baseline['csp'])

    def test_only_the_two_approved_local_brand_assets_are_added(self):
        files = {str(path.relative_to(ROOT/'web')).replace('\\','/') for path in (ROOT/'web').rglob('*') if path.is_file()}
        self.assertEqual(files,{'index.html','styles.css','app.js','brand-logo.jpg','brand-icon.ico'})
        self.assertTrue((ROOT/'web/brand-logo.jpg').read_bytes().startswith(b'\xff\xd8\xff'))
        self.assertTrue((ROOT/'web/brand-icon.ico').read_bytes().startswith(b'\0\0\1\0'))
        self.assertIn('/brand-logo.jpg',self.html+self.css)
        icons = [node['attrs'].get('href') for node in self.dom.nodes if node['tag']=='link' and 'icon' in node['attrs'].get('rel','').split()]
        self.assertIn('/brand-icon.ico',icons)

    def test_brand_assets_are_served_without_opening_arbitrary_static_files(self):
        from mixstudio.http_app import create_app
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(SimpleNamespace(root=Path(tmp)),web_root=ROOT/'web')
            client = app.test_client()
            for name in ('brand-logo.jpg','brand-icon.ico'):
                with self.subTest(asset=name):
                    response = client.get('/'+name)
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.data,(ROOT/'web'/name).read_bytes())
                    self.assertTrue(response.content_type.startswith('image/'))
                    self.assertEqual(response.headers['Content-Security-Policy'],self.baseline['csp'])
                    self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                    response.close()
            for path in ('/settings.json','/state.sqlite','/brand-logo.svg','/../brand-logo.jpg','/brand-logo.jpg/anything'):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code,404)

    def test_new_brand_is_present_in_document_and_native_entry_points(self):
        self.assertIn(NEW_BRAND,self.html)
        self.assertNotIn(OLD_BRAND,self.html)
        for name,expected in self.baseline['native'].items():
            with self.subTest(file=name):
                text = (ROOT/name).read_text('utf8')
                self.assertIn(NEW_BRAND,text)
                self.assertNotIn(OLD_BRAND,text)
                actual = native_contract(text)
                for function,arguments in expected['functions'].items():
                    self.assertEqual(actual['functions'][function],arguments)
                self.assertEqual(actual['cli'],expected['cli'])

    def test_no_new_scripts_stylesheet_dependencies_or_remote_assets(self):
        self.assertEqual(self.current['script_assets'],self.baseline['html']['script_assets'])
        self.assertEqual(self.current['stylesheet_assets'],self.baseline['html']['stylesheet_assets'])
        self.assertNotRegex(self.html,r'(?:src|href)=[\"\']https?://')
        self.assertNotRegex(self.css,r'url\(\s*[\"\']?https?://')
        self.assertNotIn('@import',self.css)
        self.assertIn('prefers-reduced-motion',self.css)
        self.assertIn(':focus-visible',self.css)
        self.assertIn('object-fit:contain',re.sub(r'\s+','',self.css))
        self.assertRegex(self.css,r'\.job-inline-detail\s*\{[^}]*text-overflow\s*:\s*ellipsis')


if __name__=='__main__':unittest.main()
