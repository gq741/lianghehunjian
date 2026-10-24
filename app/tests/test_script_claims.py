"""Offline fact-review fixtures, not recovered historical model responses."""
import copy
import json
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine as engine


TITLE = "小米（MI）REDMI 23.8英寸显示器 144Hz IPS技术 专业级色准 低蓝光 电竞电脑办公显示器显示屏 A24 2026款"


def fixture(text="支持144Hz刷新率。", hint="清晰展示显示器正面", quotes=None, title="显示器选择"):
    return {"drafts": [{"title": title, "voiceover": text, "segments": [{
        "segment_id": "s1", "text": text, "visual_hint": hint,
        "fact_quotes": ["144Hz"] if quotes is None else quotes,
    }]}]}


def wire(value):
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(value, ensure_ascii=False)}}]}, ensure_ascii=False).encode(), "application/json"


class ClaimReviewTests(unittest.TestCase):
    def validate(self, value=None, title=TITLE, retain=True):
        return engine._validate_scripts(value or fixture(), title, 25, 1,
                                        retain_claim_issues=retain)[0]

    def test_monitor_hz_parameter_label_and_camera_adverb_pass(self):
        draft = self.validate()
        self.assertEqual(draft["fact_status"], "ready")
        self.assertEqual(draft["fact_issues"], [])
        self.assertEqual(draft["voiceover"], "支持144Hz刷新率。")

    def test_tv_hz_parameter_label_passes_with_exact_quote(self):
        draft = self.validate(title="测试电视 144Hz")
        self.assertEqual(draft["fact_status"], "ready")

    def test_hz_allowance_is_not_general_or_high_refresh_claim(self):
        for title, text, quotes in (("测试电源 144Hz", "支持144Hz刷新率。", ["144Hz"]),
                                    (TITLE, "支持144Hz高刷。", ["144Hz"]),
                                    (TITLE, "支持144Hz刷新率。", []),
                                    (TITLE, "支持240Hz刷新率。", [])):
            with self.subTest(title=title, text=text, quotes=quotes):
                result = self.validate(fixture(text=text, quotes=quotes), title=title)
                self.assertEqual(result["fact_status"], "needs_review")

    def test_every_unbacked_refresh_occurrence_is_reviewed(self):
        result = self.validate(fixture(text="支持144Hz刷新率，刷新率表现更好。"))
        self.assertIn("刷新率", {i["term"] for i in result["fact_issues"]})

    def test_refresh_label_before_value_is_supported_but_disconnected_label_is_not(self):
        self.assertEqual(self.validate(fixture(text="刷新率为144Hz。"))["fact_status"], "ready")
        result = self.validate(fixture(text="支持144Hz。刷新率表现不错。"))
        self.assertIn("刷新率", {i["term"] for i in result["fact_issues"]})

    def test_bare_numeric_quote_does_not_authorize_hz_label(self):
        result = self.validate(fixture(quotes=["144"]))
        self.assertIn("刷新率", {i["term"] for i in result["fact_issues"]})

    def test_low_blue_light_does_not_expand_to_eye_claims(self):
        result = self.validate(fixture(text="低蓝光，护眼、防蓝光、无频闪。", quotes=["低蓝光"]))
        self.assertTrue({"护眼", "防蓝光", "无频闪"}.issubset({i["term"] for i in result["fact_issues"]}))

    def test_camera_directions_are_narrow_and_do_not_skip_remaining_claims(self):
        for hint in ("清晰展示显示器正面", "清晰拍摄显示器正面", "清晰呈现显示器正面", "清晰拍到显示器正面", "请清晰地拍摄显示器正面"):
            with self.subTest(hint=hint):
                self.assertEqual(self.validate(fixture(hint=hint))["fact_status"], "ready")
        for hint, expected in (("画质清晰", "清晰"), ("显示器画面流畅", "流畅"),
                               ("清晰展示无线连接", "无线"), ("清晰展示画质清晰", "清晰")):
            with self.subTest(hint=hint):
                issues = self.validate(fixture(hint=hint))["fact_issues"]
                self.assertTrue(any(i["field"] == "visual_hint" and i["term"] == expected for i in issues))

    def test_narration_clarity_does_not_receive_camera_exception(self):
        issues = self.validate(fixture(text="支持144Hz，画质清晰。"))["fact_issues"]
        self.assertTrue(any(i["field"] == "voiceover" and i["term"] == "清晰" for i in issues))

    def test_product_clarity_phrase_is_not_camera_prefix(self):
        result = self.validate(fixture(hint="显示器清晰展示商品画面"))
        self.assertTrue(any(i["term"] == "清晰" for i in result["fact_issues"]))

    def test_all_distinct_issues_have_locations_and_fixed_messages(self):
        value = fixture(text="支持144Hz，无线连接，护眼，续航100小时，亲测好用。", hint="画质清晰", title="无线显示器")
        result = self.validate(value)
        self.assertEqual(result["voiceover"], value["drafts"][0]["voiceover"])
        issues = result["fact_issues"]
        self.assertTrue({"无线", "护眼", "续航", "100", "亲测", "清晰"}.issubset({i["term"] for i in issues}))
        self.assertEqual({i["field"] for i in issues}, {"voiceover", "visual_hint", "title"})
        for item in issues:
            self.assertEqual(set(item), {"field", "segment_id", "term", "message"})
            self.assertLessEqual(len(item["term"]), 64)
            self.assertEqual(item["segment_id"], "" if item["field"] == "title" else "s1")

    def test_forged_and_nonmatching_quote_become_review_not_evidence(self):
        for quote in ("永久保修", "低蓝光"):
            with self.subTest(quote=quote):
                result = self.validate(fixture(quotes=["144Hz", quote]))
                self.assertEqual(result["fact_status"], "needs_review")
                self.assertTrue(any(i["term"] == quote for i in result["fact_issues"]))

    def test_bad_quote_does_not_authorize_fabricated_feature(self):
        result = self.validate(fixture(text="支持144Hz无线连接。", quotes=["144Hz", "无线连接"]))
        self.assertIn("无线", {i["term"] for i in result["fact_issues"]})

    def test_strict_mode_still_rejects_and_names_specific_term(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            self.validate(fixture(text="支持144Hz无线连接。"), retain=False)
        self.assertEqual(ctx.exception.code, "unsupported_claim")
        self.assertIn("无线", str(ctx.exception))

    def test_missing_quotes_preserved_for_review(self):
        result = self.validate(fixture(text="看看这台显示器的外观。", quotes=[]))
        self.assertEqual(result["fact_status"], "needs_review")
        self.assertTrue(result["fact_issues"])

    def test_structural_issues_never_retained(self):
        values = []
        for bad in ("C:/private/movie.mp4", "[商品名称]", "```json"):
            values.append(fixture(text=bad))
        value = fixture(); value["drafts"][0]["segments"][0]["start"] = 0; values.append(value)
        value = fixture(); value["drafts"][0]["voiceover"] += "不一致"; values.append(value)
        value = fixture(); value["drafts"][0]["segments"][0]["fact_quotes"] = [123]; values.append(value)
        value = fixture(); value["drafts"][0]["segments"][0]["fact_quotes"] = ["C:/private/file.mp4"]; values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(providers.ProviderError):
                self.validate(value)

    def test_all_issues_across_segments_are_preserved_without_input_mutation(self):
        value = fixture(text="支持144Hz无线连接。", hint="显示器正面")
        value["drafts"][0]["segments"].append({"segment_id": "s2", "text": "低蓝光护眼。",
            "visual_hint": "流畅播放画面", "fact_quotes": ["低蓝光"]})
        value["drafts"][0]["voiceover"] += "低蓝光护眼。"
        before = copy.deepcopy(value)
        result = self.validate(value)
        self.assertEqual(value, before)
        self.assertEqual(result["voiceover"], before["drafts"][0]["voiceover"])
        self.assertEqual({i["segment_id"] for i in result["fact_issues"]}, {"s1", "s2"})
        self.assertTrue({"无线", "护眼", "流畅"}.issubset({i["term"] for i in result["fact_issues"]}))

    def test_generate_retains_claim_issues_without_retry(self):
        value = fixture(text="支持144Hz无线连接。")
        with patch.object(providers, "_post", return_value=wire(value)) as post:
            result = engine.generate_scripts({"title": TITLE, "sku": "12345", "status": "read"},
                api_key="test-placeholder", model="deepseek-chat", target_seconds=25, count=1)
        post.assert_called_once()
        self.assertEqual(result[0]["voiceover"], value["drafts"][0]["voiceover"])
        self.assertEqual(result[0]["fact_status"], "needs_review")

    def test_mixed_batch_preserves_valid_and_review_drafts_once(self):
        value = fixture()
        value["drafts"].extend(fixture(text="支持144Hz无线连接。")["drafts"])
        with patch.object(providers, "_post", return_value=wire(value)) as post:
            results = engine.generate_scripts({"title": TITLE, "sku": "12345", "status": "read"},
                api_key="test-placeholder", model="deepseek-chat", target_seconds=25, count=2)
        post.assert_called_once()
        self.assertEqual([d["fact_status"] for d in results], ["ready", "needs_review"])
        self.assertEqual([d["voiceover"] for d in results], [d["voiceover"] for d in value["drafts"]])

    def test_edit_review_uses_title_locally_and_can_clear_issues(self):
        with patch.object(providers, "_post") as post:
            bad = engine.review_copy("支持144Hz刷新率，无线连接，护眼。", TITLE)
            good = engine.review_copy("支持144Hz刷新率，标题标注低蓝光。", TITLE)
        post.assert_not_called()
        self.assertEqual(bad["fact_status"], "needs_review")
        self.assertTrue({"无线", "护眼"}.issubset({i["term"] for i in bad["fact_issues"]}))
        self.assertEqual(good, {"fact_status": "ready", "fact_issues": []})

    def test_edit_review_reports_all_segments_and_respects_negation(self):
        result = engine.review_copy("显示器支持蓝牙。它还支持无线连接，护眼。", "测试显示器 不支持蓝牙")
        self.assertEqual(result["fact_status"], "needs_review")
        self.assertEqual({i["segment_id"] for i in result["fact_issues"]}, {"s1", "s2"})
        self.assertTrue(any("限制" in i["message"] for i in result["fact_issues"]))

    def test_edit_review_rejects_structure_markers_without_http(self):
        with patch.object(providers, "_post") as post:
            for text in ("", "[商品名称]", "C:/private/file.mp4", "https://example.org"):
                with self.subTest(text=text), self.assertRaises(providers.ProviderError):
                    engine.review_copy(text, TITLE)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
