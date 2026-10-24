import json
import unittest
from unittest.mock import patch
from mixstudio import script_engine as engine, providers
from test_script_engine import SEGMENTS, SCENES, PRODUCT, wire

class RankedMatchingTests(unittest.TestCase):
    def match(self, scenes=None, segments=None):
        return engine.match_segments(segments or SEGMENTS, scenes or SCENES, api_key='test-placeholder',model='deepseek-chat',product_title=PRODUCT['title'])
    def test_ranked_candidates_keep_short_and_shared_choices(self):
        result={'candidates':[{'segment_id':'s1','clip_ids':['clip_b'],'confidence':.9,'reason':'相关'}, {'segment_id':'s2','clip_ids':['clip_b','clip_a'],'confidence':.9,'reason':'相关'}],'unmatched':[]}
        with patch.object(engine,'_complete',return_value=result) as post:
            out=self.match(scenes=[dict(SCENES[0],start=0,end=.2),SCENES[1]])
        self.assertEqual(out['candidates'],result['candidates']);post.assert_called_once()
    def test_all_large_input_batches_evaluated_no_paths(self):
        scenes=[dict(SCENES[0],clip_id=f'c{i}',text='画'*1000,asset_id=f'a{i}') for i in range(200)]
        seen=[]
        def complete(system,body,**kw):
            seen.extend(c['clip_id'] for c in body['scenes'])
            self.assertNotIn('source_path',json.dumps(body))
            return {'candidates':[], 'unmatched':[s['segment_id'] for s in body['segments']]}
        with patch.object(engine,'_complete',side_effect=complete) as post:
            out=self.match(scenes=scenes)
        self.assertEqual(set(seen),{f'c{i}' for i in range(200)});self.assertGreater(post.call_count,1)
        self.assertEqual(out['unmatched'],['s1','s2']);self.assertEqual(out['evaluated_scene_count'],200)
    def test_4500_scenes_and_segment_groups_are_all_evaluated_with_cancel_progress(self):
        scenes=[dict(SCENES[0],clip_id=f'c{i}',text='相关画面',asset_id=f'a{i}') for i in range(4500)]
        segments=[dict(SEGMENTS[0],segment_id=f's{i}') for i in range(17)]
        seen={sid:set() for sid in (s['segment_id'] for s in segments)}
        progress=[]
        def complete(system,body,**kw):
            self.assertLessEqual(len(body['scenes']),128)
            self.assertLessEqual(len(body['segments']),64)
            for seg in body['segments']:
                seen[seg['segment_id']].update(c['clip_id'] for c in body['scenes'])
            return {'candidates':[],'unmatched':[seg['segment_id'] for seg in body['segments']]}
        with patch.object(engine,'_complete',side_effect=complete) as post:
            out=engine.match_segments(segments,scenes,api_key='test-placeholder',model='deepseek-chat',
                product_title=PRODUCT['title'],progress=lambda v,m:progress.append((v,m)))
        self.assertTrue(all(len(ids)==4500 for ids in seen.values()))
        self.assertEqual(post.call_count,36)
        self.assertEqual(out['semantic_batch_count'],36)
        self.assertIn('1/36',progress[0][1])
        self.assertEqual(progress[-1][0],1.)

    def test_unknown_id_in_ranked_result_rejected_once(self):
        with patch.object(engine,'_complete',return_value={'candidates':[{'segment_id':'s1','clip_ids':['fake'],'confidence':.9,'reason':'相关'}],'unmatched':['s2']}) as post:
            with self.assertRaises(providers.ProviderError):self.match()
        post.assert_called_once()

if __name__=='__main__':unittest.main()
