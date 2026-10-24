import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio.store import Store
from mixstudio.http_app import create_app


class LocalTaskLogsTests(unittest.TestCase):
    def test_two_task_areas_retain_independent_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'state.sqlite')
            store.create_job('oldscript', 'script', {})
            store.update_job('oldscript', status='completed')
            for i in range(201):
                store.create_job(f'down{i}', 'download', {})
                store.update_job(f'down{i}', status='completed')
            self.assertIn('oldscript', [j['id'] for j in store.jobs()])
            records = [json.loads(line) for p in (Path(folder)/'logs').glob('*.jsonl') for line in p.read_text('utf-8').splitlines()]
            self.assertEqual(records[0]['kind'], 'script')

    def test_append_only_history_uses_allowlisted_metadata_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'state.sqlite')
            secret = 'secret-value-never-log-this-credential'
            for i in range(25):
                identifier = f'job{i:03}'
                store.create_job(identifier, 'render', {'token': secret, 'path': secret})
                store.update_job(identifier, status='failed', error=secret, message=secret, progress=.2)
            paths = list((Path(folder) / 'logs').glob('tasks-*.jsonl'))
            self.assertEqual(len(paths), 1)
            raw = paths[0].read_text('utf-8')
            self.assertNotIn(secret, raw)
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(records), 50)
            self.assertEqual(records[-1]['status'], 'failed')
            self.assertEqual(records[-1]['error_present'], True)
            self.assertIsNotNone(store.job('job000'))
            self.assertEqual(len(store.jobs()), 25)

    def test_progress_is_logged_and_log_failure_does_not_lose_task(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'state.sqlite')
            store.create_job('one', 'match', {})
            with patch('mixstudio.tasklog.append_event', side_effect=OSError('disk error')):
                store.update_job('one', status='running', progress=.4)
            self.assertEqual(store.job('one')['progress'], .4)

    def test_open_logs_has_local_auth_and_fixed_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            class Studio:
                root = Path(folder)
            client = create_app(Studio(), token='local-test').test_client()
            self.assertEqual(client.post('/api/open-logs').status_code, 403)
            target = 'os.startfile'
            with patch(target, create=True) as opened:
                response = client.post('/api/open-logs', json={'path': 'C:/arbitrary'},
                                       headers={'X-Local-Token': 'local-test'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Path(response.json['data']['folder']), Path(folder) / 'logs')
            if os.name == 'nt': opened.assert_called_once_with(Path(folder) / 'logs')
