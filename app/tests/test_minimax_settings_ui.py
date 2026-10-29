"""Provider preferences and production UI; synthetic DPAPI and localhost API only."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import settings
import test_simple_mix_ui as ui_fixture
from test_frontend import Markup


ROOT = Path(__file__).resolve().parents[1]


def fixture_dpapi(raw, decrypt=False):
    """A reversible mock envelope, never the host user's DPAPI or credentials."""
    if decrypt:
        if not raw.startswith(b'fixture-cipher:'):
            raise ValueError('synthetic corrupt envelope')
        return bytes.fromhex(raw[len(b'fixture-cipher:'):].decode('ascii'))
    return b'fixture-cipher:' + raw.hex().encode('ascii')


class MiniMaxSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = settings.Settings(self.root)

    def test_defaults_keep_fish_and_do_not_create_settings_or_credentials(self):
        public = self.config.public()
        self.assertEqual(public['voice_provider'], 'fish')
        self.assertEqual(public['voice_id'], 'be40aff9ee7b4d0cba3bb35e924d85b2')
        self.assertEqual(public['minimax_model'], 'speech-2.8-hd')
        self.assertEqual(public['minimax_voice_id'], 'male-qn-qingse')
        self.assertFalse(public['fish_key_set'])
        self.assertFalse(public['minimax_key_set'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_voice_and_encrypted_key_load_read_only(self):
        original = b'{"voice_id":"legacy-fish-voice"}'
        self.config.path.write_bytes(original)
        with patch.object(settings, '_dpapi', side_effect=fixture_dpapi):
            self.config.update({'fish_api_key': 'fixture-fish-original'})
            # Keep the old-shape settings to exercise an actual legacy load.
            self.config.path.write_bytes(original)
            cipher = self.config.secret_path.read_bytes()
            loaded = settings.Settings(self.root)
            self.assertEqual(loaded.values['voice_provider'], 'fish')
            self.assertEqual(loaded.values['voice_id'], 'legacy-fish-voice')
            self.assertEqual(loaded.secret('fish'), 'fixture-fish-original')
            self.assertFalse(loaded.public()['minimax_key_set'])
        self.assertEqual(self.config.path.read_bytes(), original)
        self.assertEqual(self.config.secret_path.read_bytes(), cipher)

    def test_two_keys_save_independently_and_blank_keeps_each_saved_key(self):
        with patch.object(settings, '_dpapi', side_effect=fixture_dpapi) as crypt:
            self.config.update({'fish_api_key': 'fixture-fish-original'})
            self.config.update({'voice_provider': 'minimax', 'minimax_api_key': ' fixture-minimax-new ',
                                'voice_id': 'preserved-fish-voice'})
            self.config.update({'fish_api_key': '', 'minimax_api_key': '   '})
            reloaded = settings.Settings(self.root)
            self.assertEqual(reloaded.secret('fish'), 'fixture-fish-original')
            self.assertEqual(reloaded.secret('minimax'), 'fixture-minimax-new')
            self.assertEqual(reloaded.values['voice_id'], 'preserved-fish-voice')
            self.config.update({'voice_provider': 'fish', 'minimax_api_key': 'fixture-minimax-replaced'})
            self.assertEqual(self.config.secret('fish'), 'fixture-fish-original')
            self.assertEqual(self.config.secret('minimax'), 'fixture-minimax-replaced')
            self.assertTrue(any(call.args[0].startswith(b'{') and not call.kwargs.get('decrypt')
                                for call in crypt.call_args_list))

    def test_public_only_reports_key_presence_not_plaintext(self):
        with patch.object(settings, '_dpapi', side_effect=fixture_dpapi):
            public = self.config.update({'fish_api_key': 'fixture-fish-sensitive',
                                         'minimax_api_key': 'fixture-minimax-sensitive'})
            self.assertTrue(public['fish_key_set'])
            self.assertTrue(public['minimax_key_set'])
            serialized = json.dumps(public) + self.config.path.read_text('utf-8')
            self.assertNotIn('fixture-fish-sensitive', serialized)
            self.assertNotIn('fixture-minimax-sensitive', serialized)
            self.assertNotIn('minimax_api_key', public)
            self.assertNotIn('fish_api_key', public)

    def test_unreadable_credentials_public_and_explicit_reset_cover_both_providers(self):
        ciphertext = b'unreadable-fixture-ciphertext'
        self.config.secret_path.write_bytes(ciphertext)
        public = self.config.public()
        self.assertTrue(public['credentials_need_reset'])
        self.assertFalse(public['minimax_key_set'])
        self.assertFalse(public['fish_key_set'])
        with self.assertRaises(ValueError):
            self.config.reset_credentials(False)
        self.assertEqual(self.config.secret_path.read_bytes(), ciphertext)
        with patch.object(settings, '_dpapi', side_effect=fixture_dpapi):
            public = self.config.reset_credentials(True)
            self.assertFalse(public['credentials_need_reset'])
            self.assertFalse(public['minimax_key_set'])
            self.assertFalse(public['fish_key_set'])
            self.assertEqual(self.config.secret('minimax'), '')
        self.assertEqual(next(self.root.glob('secrets.unreadable.*.dpapi')).read_bytes(), ciphertext)

    def test_provider_and_models_are_bounded_choices(self):
        for provider in ('fish', 'minimax'):
            self.config.update({'voice_provider': provider})
        for model in ('speech-2.8-hd', 'speech-2.8-turbo'):
            self.config.update({'minimax_model': model})
        for key, bad in [('voice_provider', 'other'), ('voice_provider', None),
                         ('minimax_model', 'speech-2.6-hd'), ('minimax_model', ''),
                         ('minimax_model', None), ('minimax_endpoint', 'https://example.invalid')]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.config.update({key: bad})

    def test_official_and_custom_voice_ids_support_one_to_256_characters(self):
        for voice in ('male-qn-qingse', 'Chinese (Mandarin)_Gentle', '定制音色-2026._v1', 'x', 'x' * 256):
            with self.subTest(voice=voice):
                self.config.update({'minimax_voice_id': voice})
                self.assertEqual(settings.Settings(self.root).values['minimax_voice_id'], voice)

    def test_empty_long_nontext_and_all_unicode_control_voice_ids_rejected(self):
        for voice in ['', '   ', 'x' * 257, None, 1, ['voice']] + [f'a{chr(n)}b' for n in list(range(32)) + list(range(127, 160))]:
            with self.subTest(voice=voice), self.assertRaises(ValueError):
                self.config.update({'minimax_voice_id': voice})


@unittest.skipUnless(shutil.which('node'), 'Node required for offline frontend tests')
class MiniMaxSettingsUiTests(unittest.TestCase):
    run_node = ui_fixture.SimpleMixUiTests.run_node

    def prefix(self):
        prefix = ui_fixture.SimpleMixUiTests.prefix(self)
        # Teach the existing mock server settings PUT semantics; no real HTTP.
        return prefix.replace("let result={id:'fake-job'};", """let result={id:'fake-job'};
if(path==='/api/settings'&&options.method==='PUT'){
 for(const [key,value] of Object.entries(body)){
  if(key.endsWith('_api_key'))data.settings[key.replace('_api_key','_key_set')]=true;
  else data.settings[key]=value;
 }
 result=structuredClone(data.settings);
}
""")

    def whole_draft(self):
        return """data.drafts=[{id:'voice',workflow:'script_first_v2',production_mode:'whole_voice_v1',
target_seconds:25,voiceover:'看看商品的外观。',segments:[{segment_id:'s1',text:'看看商品的外观。'}],match_status:'pending',clips:[],matches:[]}];timer();await flush();"""

    def test_markup_uses_secret_password_and_known_provider_options_without_endpoint(self):
        html = (ROOT / 'web/index.html').read_text('utf-8')
        markup = Markup()
        markup.feed(html)
        self.assertEqual(markup.inputs['set-minimax_api_key']['type'], 'password')
        self.assertNotIn('value', markup.inputs['set-minimax_api_key'])
        self.assertEqual(markup.inputs['set-minimax_voice_id']['maxlength'], '256')
        self.assertNotIn('set-minimax_endpoint', markup.ids)
        self.assertIn('value="speech-2.8-hd"', html)
        self.assertIn('value="speech-2.8-turbo"', html)
        self.assertIn('value="minimax"', html)

    def test_change_only_reveals_provider_fields_and_keeps_other_key_input(self):
        self.run_node(self.prefix() + r'''
(async()=>{await flush();
const groups=document.querySelectorAll('[data-voice-provider]');
assert.equal($('set-voice_provider').value,'fish');
for(const field of groups)assert.equal(field.hidden,field.getAttribute('data-voice-provider')!=='fish');
assert.equal($('voice-provider-label').textContent,'Fish Audio');
$('set-fish_api_key').value='fixture-unsaved-fish';
$('set-voice_provider').value='minimax';event($('set-voice_provider'),'change');await flush();
for(const field of groups)assert.equal(field.hidden,field.getAttribute('data-voice-provider')!=='minimax');
assert.equal($('set-fish_api_key').value,'fixture-unsaved-fish');
assert.equal($('settings-status').textContent,'有未保存的设置');
assert.equal(writes().length,0);assert.equal($('confirm-dialog').open,false);
// The workbench truth is the saved backend provider, not unsaved form state.
assert.equal($('voice-provider-label').textContent,'Fish Audio');
$('set-voice_provider').value='fish';event($('set-voice_provider'),'change');
assert.equal($('set-fish_api_key').value,'fixture-unsaved-fish');assert.equal(writes().length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_explicit_save_sends_only_known_fields_preserves_blank_key_and_syncs_summary(self):
        self.run_node(self.prefix() + r'''
(async()=>{await flush();
data.settings.voice_id='legacy-fish-voice';timer();await flush();
$('set-voice_provider').value='minimax';event($('set-voice_provider'),'change');
$('set-minimax_api_key').value='fixture-minimax-input';
$('set-minimax_model').value='speech-2.8-turbo';$('set-minimax_voice_id').value='custom_voice.2026';
// An unrelated future or diagnostic field is not reflected into the save body.
data.settings.unrecognized_fixture='must-not-save';
event($('settings-form'),'submit');await flush();
const sent=writes();assert.equal(sent.length,1);assert.equal(sent[0].path,'/api/settings');
assert.equal(sent[0].body.voice_provider,'minimax');assert.equal(sent[0].body.voice_id,'legacy-fish-voice');
assert.equal(sent[0].body.minimax_model,'speech-2.8-turbo');assert.equal(sent[0].body.minimax_voice_id,'custom_voice.2026');
assert.equal(sent[0].body.minimax_api_key,'fixture-minimax-input');
for(const key of ['fish_api_key','unrecognized_fixture','minimax_endpoint','minimax_key_set'])assert.ok(!(key in sent[0].body));
assert.equal(data.settings.fish_key_set,true);assert.equal(data.settings.minimax_key_set,true);
assert.equal($('set-minimax_api_key').value,'');assert.equal($('set-fish_api_key').value,'');
assert.equal($('voice-provider-label').textContent,'MiniMax');assert.equal($('minimax-key-status').textContent,'已保存');
assert.ok($('render-cost-hint').textContent.includes('MiniMax'));assert.ok(!$('render-cost-hint').textContent.includes('Fish'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_provider_change_requires_save_before_production_without_api_call(self):
        self.run_node(self.prefix() + '(async()=>{await flush();' + self.whole_draft() + r'''
$('set-voice_provider').value='minimax';event($('set-voice_provider'),'change');
event($('render-draft'),'click');await flush();
assert.equal(writes().length,0);assert.equal($('confirm-dialog').open,false);
assert.equal(document.activeElement,$('save-settings'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_minimax_uses_only_minimax_key_and_production_confirmation_mentions_minimax(self):
        self.run_node(self.prefix() + '(async()=>{await flush();' + r'''
data.settings.voice_provider='minimax';data.settings.fish_key_set=true;data.settings.minimax_key_set=false;
''' + self.whole_draft() + r'''
event($('render-draft'),'click');await flush();
assert.equal(document.activeElement,$('set-minimax_api_key'));assert.equal(writes().length,0);
assert.equal($('confirm-dialog').open,false);
data.settings.minimax_key_set=true;data.settings.fish_key_set=false;timer();await flush();
event($('render-draft'),'click');await flush();
assert.equal($('confirm-dialog').open,true);
const description=$('confirm-description').textContent;
for(const name of ['一整条 MiniMax 配音','MiniMax 配音','GLM','DeepSeek','不会自动重复提交'])assert.ok(description.includes(name),name);
assert.ok(!description.includes('Fish'));assert.equal(writes().length,0);
event($('confirm-cancel'),'click');await flush();assert.equal(writes().length,0);
event($('render-draft'),'click');await flush();event($('confirm-accept'),'click');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/v2/drafts/voice/produce');assert.equal(writes()[0].body.allow_paid,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_fish_selected_never_accepts_minimax_key_as_fish_key(self):
        self.run_node(self.prefix() + '(async()=>{await flush();' + r'''
data.settings.voice_provider='fish';data.settings.fish_key_set=false;data.settings.minimax_key_set=true;
''' + self.whole_draft() + r'''
event($('render-draft'),'click');await flush();
assert.equal(document.activeElement,$('set-fish_api_key'));assert.equal(writes().length,0);
assert.equal($('confirm-dialog').open,false);assert.equal($('voice-provider-label').textContent,'Fish Audio');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_fish_consent_keeps_captured_voice_when_another_window_switches_to_minimax(self):
        self.run_node(self.prefix() + '(async()=>{await flush();' + r'''
data.settings.voice_provider='fish';data.settings.voice_id='fish-consented-voice';
''' + self.whole_draft() + r'''
event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.ok($('confirm-description').textContent.includes('Fish Audio 配音'));
data.settings.voice_provider='minimax';data.settings.minimax_model='speech-2.8-turbo';
data.settings.minimax_voice_id='different-voice';data.settings.minimax_key_set=true;timer();await flush();
assert.equal($('voice-provider-label').textContent,'MiniMax');
assert.ok($('confirm-description').textContent.includes('Fish Audio 配音'));
event($('confirm-accept'),'click');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/v2/drafts/voice/produce');
assert.deepEqual(writes()[0].body,{allow_paid:true,voice:{provider:'fish',model:'s2.1-pro',voice_id:'fish-consented-voice'}});
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_minimax_consent_keeps_captured_model_and_voice_across_settings_refresh(self):
        self.run_node(self.prefix() + '(async()=>{await flush();' + r'''
data.settings.voice_provider='minimax';data.settings.minimax_key_set=true;
data.settings.minimax_model='speech-2.8-hd';data.settings.minimax_voice_id='male-qn-qingse';
''' + self.whole_draft() + r'''
event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);
assert.ok($('confirm-description').textContent.includes('MiniMax 配音'));
data.settings.minimax_model='speech-2.8-turbo';data.settings.minimax_voice_id='other-custom-voice';
timer();await flush();event($('confirm-accept'),'click');await flush();
assert.equal(writes().length,1);
assert.deepEqual(writes()[0].body,{allow_paid:true,voice:{provider:'minimax',model:'speech-2.8-hd',voice_id:'male-qn-qingse'}});
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_legacy_render_also_sends_the_voice_snapshot_from_its_consent(self):
        self.run_node(self.prefix() + r'''
(async()=>{await flush();
data.settings.voice_provider='minimax';data.settings.minimax_key_set=true;
data.settings.minimax_model='speech-2.8-turbo';data.settings.minimax_voice_id='custom_legacy_voice';
data.drafts=[{id:'legacy',target_seconds:25,segments:[{clip_id:'clip-one',text:'看看商品。'}]}];timer();await flush();
event($('render-draft'),'click');await flush();assert.equal($('confirm-dialog').open,true);
data.settings.voice_provider='fish';timer();await flush();event($('confirm-accept'),'click');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/drafts/legacy/render');
assert.deepEqual(writes()[0].body,{allow_paid:true,voice:{provider:'minimax',model:'speech-2.8-turbo',voice_id:'custom_legacy_voice'}});
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
