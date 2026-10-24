"""Portability-only caption/launcher/build integrity tests; no live APIs."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import media

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('portable_builder_test',ROOT/'tools/build_portable.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class PortableDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_portable_font_is_opt_in_and_default_style_is_unchanged(self):
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('JDMIX_FONT_FILE',None)
            self.assertIsNone(media._portable_font())
            output=self.root/'default.ass';media._write_captions(output,[{'text':'中文','start':0,'end':1}],320,240,20,200)
            self.assertIn('Default,Microsoft YaHei,',output.read_text('utf8'))

    def test_portable_font_uses_fixed_safe_family(self):
        font=self.root/'NotoSansCJKsc-Regular.otf';font.write_bytes(b'fixture not used for rendering')
        with patch.dict(os.environ,{'JDMIX_FONT_FILE':str(font)}):
            self.assertEqual(media._portable_font(),font)
            output=self.root/'portable.ass';media._write_captions(output,[{'text':'中文','start':0,'end':1}],320,240,20,200)
        self.assertIn('Default,Noto Sans CJK SC,',output.read_text('utf8'))

    def test_missing_or_arbitrary_portable_font_fails_before_render(self):
        for path in (self.root/'NotoSansCJKsc-Regular.otf',self.root/'arbitrary.otf'):
            if path.name=='arbitrary.otf':path.write_bytes(b'fixture')
            with patch.dict(os.environ,{'JDMIX_FONT_FILE':str(path)}),self.assertRaises(media.MediaError):media._portable_font()

    def test_safe_paths_cannot_escape_or_use_absolute_paths(self):
        for value in ('../other','C:/other','/other','folder\\file','file:stream',None):
            with self.subTest(path=value),self.assertRaises(ValueError):builder.safe_path(self.root,value)
        self.assertEqual(builder.safe_path(self.root,'app/app.py'),self.root/'app/app.py')

    def test_hash_verification_detects_changed_payload(self):
        path=self.root/'payload.txt';path.write_bytes(b'original')
        rows=[{'path':'payload.txt','bytes':8,'sha256':builder.digest(path)}]
        (self.root/'FILES.sha256.json').write_text(json.dumps(rows),encoding='utf8')
        self.assertEqual(builder.verify_package(self.root)['files_verified'],1)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):builder.verify_package(self.root)

    def test_launcher_does_not_reference_sender_or_venv(self):
        builder.launchers(self.root)
        text=(self.root/'启动量禾混剪.vbs').read_text('utf-8-sig')
        self.assertIn('WScript.ScriptFullName',text);self.assertIn('runtime\\pythonw.exe',text)
        self.assertNotIn('20136',text);self.assertNotIn('.venv',text)

    def test_offline_installers_are_signature_checked_and_no_cloud_call_is_added(self):
        builder.launchers(self.root)
        text=(self.root/'安装系统组件.ps1').read_text('utf-8-sig')
        self.assertIn('Get-AuthenticodeSignature',text)
        for word in ('Invoke-WebRequest','api.deepseek.com','open.bigmodel.cn','api.fish.audio'):self.assertNotIn(word,text)

    def test_recipient_instructions_override_machine_bound_agent_instructions(self):
        builder.documents(self.root)
        text=(self.root/'app/AGENTS.md').read_text('utf8')
        self.assertIn('../WORKBUDDY.md',text);self.assertNotIn('E:\\京东自动混剪',text)
        self.assertTrue((self.root/'WORKBUDDY.md').is_file())


if __name__=='__main__':unittest.main()
