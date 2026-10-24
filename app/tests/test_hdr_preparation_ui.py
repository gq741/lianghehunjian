"""Offline UI contracts for automatic, free local media preparation."""
import unittest
import test_simple_mix_ui as fixture


class HdrPreparationUiTests(unittest.TestCase):
    prefix = fixture.SimpleMixUiTests.prefix
    run_node = fixture.SimpleMixUiTests.run_node

    def test_next_submits_selected_sources_without_paid_confirmation(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();
$('select-all').checked=true;event($('select-all'),'change');
assert.equal($('go-mix').disabled,false);event($('go-mix'),'click');await flush();
assert.equal(writes().length,1);assert.equal(writes()[0].path,'/api/v2/preflight');
assert.deepEqual(writes()[0].body.asset_ids,['local','other']);
assert.equal($('page-mix').hidden,false);assert.equal($('confirm-dialog').open,false);
assert.equal('allow_paid' in writes()[0].body,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_preparation_progress_is_a_mix_job_and_blocks_paid_actions_until_done(self):
        self.run_node(self.prefix() + r"""
(async()=>{await flush();$('select-all').checked=true;event($('select-all'),'change');
assert.equal($('prepare-button').disabled,false);
data.jobs=[{id:'local-prep',kind:'media_prepare',status:'running',created_at:500,progress:.4,message:'素材准备 1/2'}];
timer();await flush();assert.equal($('prepare-button').disabled,true);
assert.equal($('match-draft').disabled,true);assert.equal($('render-draft').disabled,true);
assert.equal($('mix-jobs').querySelector('.job-name').textContent,'素材准备 · local-pr');
assert.ok(!$('download-jobs').querySelector('.job-name'));
data.jobs[0].status='completed';timer();await flush();assert.equal($('prepare-button').disabled,false);
assert.equal(writes().length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_preview_offset_changes_only_playback_clock_not_source_range_labels(self):
        js = (fixture.WEB / 'app.js').read_text('utf-8')
        self.assertIn('const sourceOffset = Number(asset?.prepared_media?.time_offset) || 0', js)
        self.assertIn('end: Number(clip.end) - sourceOffset', js)
        self.assertIn('(Number(clip.start) || 0) - sourceOffset', js)
        self.assertIn('formatTime(clip.start)', js)
        self.assertIn('formatTime(clip.end)', js)


if __name__ == '__main__':
    unittest.main()
