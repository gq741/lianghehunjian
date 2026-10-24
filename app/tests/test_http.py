from pathlib import Path
import tempfile
import unittest
from mixstudio.service import Studio
from mixstudio.http_app import create_app


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.studio = Studio(self.root / "data")
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text('<meta name="local-token" content="__LOCAL_TOKEN__">')
        self.app = create_app(self.studio, self.web, token="test-session-token")
        self.client = self.app.test_client()
        self.headers = {"X-Local-Token": "test-session-token"}

    def tearDown(self):
        self.studio.close()
        self.temp.cleanup()

    def test_token_required_and_no_keys(self):
        self.assertEqual(self.client.get("/api/state").status_code, 403)
        response = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertNotIn("deepseek_api_key", response.get_data(as_text=True))

    def test_host_and_origin_checked(self):
        self.assertEqual(self.client.get("/api/state", headers={**self.headers, "Host": "evil.test"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={**self.headers, "Origin": "https://evil.test"}).status_code, 403)

    def test_index_session_and_known_media_only(self):
        response = self.client.get("/")
        self.assertIn("test-session-token", response.get_data(as_text=True))
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertNotEqual(self.client.get("/media/assets/../../secret").status_code, 200)
        self.assertNotEqual(self.client.get("/.env").status_code, 200)

    def test_invalid_settings_and_paid_request(self):
        response = self.client.put("/api/settings", headers=self.headers, json={"fps": 0})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/prepare", headers=self.headers, json={"asset_ids": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("确认", response.json["error"])

    def test_download_rejects_arbitrary_host_and_profile(self):
        for url in ["https://example.com/a.mp4", "http://127.0.0.1/private", "https://www.douyin.com/user/123"]:
            response = self.client.post("/api/downloads", headers=self.headers,
                json={"links": [url], "folder": str(self.root)})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.studio.store.jobs(), [])

    def test_transcript_validates_source_bounds(self):
        self.studio.store.put_asset({"id": "x", "path": str(self.root / "x.mp4"), "duration": 5})
        for segments in [[{"start": 4, "end": 8, "text": "test"}],
                         [{"start": 0, "end": 3, "text": "a"}, {"start": 2, "end": 4, "text": "b"}]]:
            response = self.client.post("/api/assets/x/transcript", headers=self.headers, json={"segments": segments})
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/assets/x/transcript", headers=self.headers,
            json={"segments": [{"start": 0, "end": 3, "text": "真实原文"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["clips"][0]["text"], "真实原文")


if __name__ == "__main__":
    unittest.main()
