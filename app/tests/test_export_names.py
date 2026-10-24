"""Temporary export-name reservations; no user media or model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

try:
    from mixstudio import output_names
except ImportError:
    output_names=None
from mixstudio.jobs import Cancelled


class ExportNameTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(output_names,'Product/date export naming has not been implemented')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=patch.object(output_names,'datetime');self.now=self.clock.start();self.addCleanup(self.clock.stop)
        self.now.now.return_value=datetime(2026,10,24,4,59,58)

    def marker(self,path):return path.with_name(path.name+'.reserve')

    def test_confirmed_product_example(self):
        with output_names.reserve_output(self.root,'小天鹅（Little Swan）洗衣机') as output:
            self.assertEqual(output.name,'小天鹅（L 10.24.4.mp4')

    def test_month_day_hour_are_not_zero_padded(self):
        self.now.now.return_value=datetime(2026,1,2,3,7,9)
        with output_names.reserve_output(self.root,'米家商品') as output:
            self.assertEqual(output.name,'米家商品 1.2.3.mp4')

    def test_first_five_unicode_codepoints_not_utf16_units_or_bytes(self):
        with output_names.reserve_output(self.root,'😀小天鹅（Little Swan）') as output:
            self.assertEqual(output.name,'😀小天鹅（ 10.24.4.mp4')

    def test_only_outer_whitespace_is_stripped(self):
        with output_names.reserve_output(self.root,'  小 天 鹅洗衣机  ') as output:
            self.assertEqual(output.name,'小 天 鹅 10.24.4.mp4')

    def test_short_and_empty_titles(self):
        for title,prefix in [('米家','米家'),('','商品'),(' \t\n ','商品')]:
            with self.subTest(title=title),output_names.reserve_output(self.root,title) as output:
                self.assertEqual(output.name,prefix+' 10.24.4.mp4')

    def test_slice_precedes_windows_character_replacement(self):
        for title,prefix in [('A/B:C后续商品','A_B_C'),('1234/后续商品','1234_'),('<>:"/后续商品','_____'),('A\0B:C后续','A_B_C')]:
            with self.subTest(title=title),output_names.reserve_output(self.root,title) as output:
                self.assertEqual(output.name,prefix+' 10.24.4.mp4')

    def test_timestamp_has_no_year_minute_or_second(self):
        with output_names.reserve_output(self.root,'测试') as output:
            self.assertEqual(output.stem,'测试 10.24.4')
            self.now.now.assert_called_once_with()

    def test_windows_device_name_before_first_dot_is_escaped(self):
        for title in ('COM1.商品', 'CON.X商品', 'AUX .商品', 'LPT¹.商品'):
            with self.subTest(title=title),output_names.reserve_output(self.root,title) as output:
                self.assertEqual(output.name,'_'+title[:5]+' 10.24.4.mp4')

    def test_existing_video_is_never_replaced(self):
        old=self.root/'商品 10.24.4.mp4';original=b'EXISTING USER OUTPUT';old.write_bytes(original)
        with output_names.reserve_output(self.root,'商品') as output:
            self.assertEqual(output.name,'商品 10.24.4 (2).mp4');output.write_bytes(b'NEW OUTPUT')
        self.assertEqual(old.read_bytes(),original)

    def test_same_hour_exports_increment_two_then_three(self):
        expected=['商品 10.24.4.mp4','商品 10.24.4 (2).mp4','商品 10.24.4 (3).mp4']
        names=[]
        for i in range(3):
            with output_names.reserve_output(self.root,'商品') as output:
                names.append(output.name);output.write_bytes(str(i).encode())
        self.assertEqual(names,expected)
        self.assertEqual([self.root.joinpath(n).read_bytes() for n in names],[b'0',b'1',b'2'])

    def test_crash_reservation_is_skipped_and_retained(self):
        stale=self.root/'商品 10.24.4.mp4.reserve';stale.write_bytes(b'OLDER CRASH RESERVATION')
        with output_names.reserve_output(self.root,'商品') as output:
            self.assertEqual(output.name,'商品 10.24.4 (2).mp4')
        self.assertEqual(stale.read_bytes(),b'OLDER CRASH RESERVATION')

    def test_reservation_exists_only_while_renderer_uses_path(self):
        folder=self.root/'new'/'outputs'
        with output_names.reserve_output(folder,'商品') as output:
            self.assertTrue(self.marker(output).is_file());self.assertFalse(output.exists())
            output.write_bytes(b'RENDERED')
        self.assertFalse(self.marker(output).exists());self.assertEqual(output.read_bytes(),b'RENDERED')

    def test_render_exception_releases_own_claim(self):
        with self.assertRaisesRegex(ValueError,'render failure'):
            with output_names.reserve_output(self.root,'商品') as output:
                self.assertTrue(self.marker(output).exists());raise ValueError('render failure')
        self.assertFalse(self.marker(output).exists())
        with output_names.reserve_output(self.root,'商品') as retry:self.assertEqual(retry,output)

    def test_cancel_releases_own_claim_without_touching_old_file(self):
        old=self.root/'商品 10.24.4.mp4';old.write_bytes(b'KEEP')
        with self.assertRaises(Cancelled):
            with output_names.reserve_output(self.root,'商品') as output:raise Cancelled('cancelled')
        self.assertFalse(self.marker(output).exists());self.assertEqual(old.read_bytes(),b'KEEP')

    def test_parallel_exports_reserve_distinct_names(self):
        count=12;barrier=threading.Barrier(count)
        def export(i):
            with output_names.reserve_output(self.root,'商品') as output:
                self.assertTrue(self.marker(output).exists())
                barrier.wait(timeout=5)
                output.write_bytes(str(i).encode());return output
        with ThreadPoolExecutor(max_workers=count) as pool:paths=list(pool.map(export,range(count)))
        self.assertEqual(len(set(paths)),count)
        self.assertEqual({p.name for p in paths},{'商品 10.24.4.mp4'}|{f'商品 10.24.4 ({i}).mp4' for i in range(2,count+1)})
        self.assertEqual({p.read_bytes() for p in paths},{str(i).encode() for i in range(count)})
        self.assertFalse(list(self.root.glob('*.reserve')))


if __name__=='__main__':unittest.main()
