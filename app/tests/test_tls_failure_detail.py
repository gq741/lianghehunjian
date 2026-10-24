"""Fixed TLS failure categories survive wrappers, never raw exception text."""
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from mixstudio import diagnostics,providers,tasklog
from mixstudio.jobs import JobManager
from mixstudio.store import Store


class TLSFailureDetailTests(unittest.TestCase):
    def failure(self,reason,*,wrapped=False,read=False):
        error=urllib.error.URLError(reason) if wrapped else reason
        class Response:
            headers={'Content-Type':'application/json','Content-Length':'2'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,size):raise error
        with patch.object(providers,'_http_open',**({'return_value':Response()} if read else {'side_effect':error})) as request:
            with self.assertRaises(providers.ProviderError) as caught:
                providers._post(providers.DEEPSEEK_URL,{},'offline-placeholder-key')
        self.assertEqual(request.call_count,1)
        self.assertEqual(caught.exception.code,'tls')
        self.assertTrue(caught.exception.result_unknown)
        self.assertEqual(caught.exception.request_phase,'read' if read else 'connect')
        self.assertNotIn('PRIVATE',str(caught.exception)+json.dumps(caught.exception.diagnostic))
        return caught.exception

    def test_certificate_category_and_numeric_verify_code(self):
        for wrapped in (False,True):
            reason=ssl.SSLCertVerificationError('PRIVATE certificate details')
            reason.verify_code=20;reason.verify_message='PRIVATE verify message'
            result=self.failure(reason,wrapped=wrapped)
            self.assertEqual(result.diagnostic['tls_reason'],'certificate_verify_failed')
            self.assertEqual(result.diagnostic['tls_verify_code'],20)

    def test_eof_and_close_categories_follow_types(self):
        for cls,expected in ((ssl.SSLEOFError,'unexpected_eof'),(ssl.SSLZeroReturnError,'connection_closed')):
            for wrapped in (False,True):
                with self.subTest(type=cls.__name__,wrapped=wrapped):
                    result=self.failure(cls('PRIVATE'),wrapped=wrapped)
                    self.assertEqual(result.diagnostic['tls_reason'],expected)

    def test_known_protocol_reasons_are_mapped_to_fixed_categories(self):
        for raw,expected in (('SSLV3_ALERT_HANDSHAKE_FAILURE','handshake_failure'),
                ('TLSV1_ALERT_PROTOCOL_VERSION','protocol_version'),('UNSUPPORTED_PROTOCOL','protocol_version'),
                ('WRONG_VERSION_NUMBER','wrong_version'),('UNEXPECTED_EOF_WHILE_READING','unexpected_eof')):
            reason=ssl.SSLError('PRIVATE');reason.reason=raw
            self.assertEqual(self.failure(reason).diagnostic['tls_reason'],expected)

    def test_unknown_reason_is_other_not_unfiltered_metadata(self):
        for raw in ('PRIVATE secret',None,{},['PRIVATE']):
            reason=ssl.SSLError('PRIVATE exception message');reason.reason=raw
            result=self.failure(reason)
            self.assertEqual(result.diagnostic['tls_reason'],'other')

    def test_read_phase_eof_is_not_mislabeled_as_pre_submit(self):
        result=self.failure(ssl.SSLEOFError('PRIVATE'),read=True)
        self.assertEqual(result.diagnostic['tls_reason'],'unexpected_eof')
        self.assertEqual(result.diagnostic['request_phase'],'read')

    def test_verify_code_rejects_nonintegers_and_out_of_range(self):
        for value in (True,False,-1,65536,'20',20.,None,float('nan')):
            reason=ssl.SSLCertVerificationError('PRIVATE');reason.verify_code=value
            self.assertNotIn('tls_verify_code',self.failure(reason).diagnostic)
        for value in (0,65535):
            self.assertEqual(diagnostics.normalize_diagnostic({'tls_reason':'certificate_verify_failed','tls_verify_code':value}),
                {'tls_reason':'certificate_verify_failed','tls_verify_code':value})

    def test_normalizer_rejects_arbitrary_reason_and_noncertificate_verify_code(self):
        for reason in ('PRIVATE',{},True,None):
            self.assertEqual(diagnostics.normalize_diagnostic({'tls_reason':reason,'tls_verify_code':20}),{})
        self.assertEqual(diagnostics.normalize_diagnostic({'tls_reason':'unexpected_eof','tls_verify_code':20}),{'tls_reason':'unexpected_eof'})

    def test_wrapped_exception_keeps_only_fixed_metadata(self):
        source=self.failure(ssl.SSLEOFError('PRIVATE'));source.diagnostic['raw']='PRIVATE'
        target=RuntimeError('wrapper');diagnostics.copy_diagnostic(source,target)
        self.assertEqual(target.diagnostic['tls_reason'],'unexpected_eof')
        self.assertNotIn('PRIVATE',json.dumps(target.diagnostic))

    def test_database_and_task_log_keep_reason_not_raw_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite');manager=JobManager(store,workers=1)
            try:
                store.create_job('tls-job','script',{})
                error=self.failure(ssl.SSLEOFError('PRIVATE'))
                def work(event,progress):raise error
                manager._run('tls-job',threading.Event(),work)
                job=store.job('tls-job');self.assertEqual(job['status'],'failed')
                self.assertEqual(job['diagnostic']['tls_reason'],'unexpected_eof')
                log=next((Path(folder)/'logs').glob('*.jsonl')).read_text('utf8')
                self.assertNotIn('PRIVATE',log)
                self.assertEqual(json.loads(log.splitlines()[-1])['diagnostic'],job['diagnostic'])
            finally:manager.shutdown()


if __name__=='__main__':unittest.main()
