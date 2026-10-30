import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import test_qq_imap_export as fixtures
from test_qq_imap_export import FakeSession, mail, q


class OptimizedTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp
    tearDown = fixtures.ExportTests.tearDown
    run_export = fixtures.ExportTests.run_export
    def test_incremental_headers_filter_changes_new_mail_and_deletion(self):
        session = FakeSession({'1': mail(), '2': mail('另一个主题')})
        self.run_export({'all': True, 'subject': '中文'}, session)
        self.assertEqual(len(session.headers), 2)
        second = self.run_export({'all': True, 'subject': '另一个'}, session)
        self.assertEqual(second['counts']['saved'], 2)
        self.assertEqual(len(session.headers), 2)
        session.raws['3'] = mail('新邮件')
        del session.raws['1']
        third = self.run_export({'all': True}, session)
        self.assertEqual(third['performance']['headers_fetched'], 1)
        self.assertEqual({m['uid'] for m in third['messages']}, {'2', '3'})
        self.assertEqual(session.headers, ['1', '2', '3'])

    def test_cache_partition_folder_and_validity(self):
        session = FakeSession()
        self.run_export(session=session)
        session.mailbox = 'Sent Messages'
        self.assertEqual(self.run_export(session=session)['counts']['saved'], 2)
        session.validity = '456'
        self.assertEqual(self.run_export(session=session)['counts']['saved'], 2)
        self.assertEqual(len(session.headers), 3)

    def test_same_size_corruption_detected(self):
        session = FakeSession()
        result = self.run_export(session=session)
        file = Path(result['results'][0]['path'])
        file.write_bytes(b'X' * file.stat().st_size)
        rerun = self.run_export(session=session)
        self.assertEqual(rerun['counts'], dict(saved=1, skipped=1, failed=0))
        self.assertTrue(file.read_bytes().startswith(b'X'))
        saved = next(r for r in rerun['results'] if r['state'] == 'saved')
        self.assertTrue(Path(saved['path']).read_bytes().startswith(b'%PDF'))

    def test_cache_verifies_each_file_once(self):
        import archive_store
        session = FakeSession()
        self.run_export(session=session)
        with patch.object(archive_store, 'digest_file', wraps=archive_store.digest_file) as digest:
            result = self.run_export(session=session)
        self.assertEqual(digest.call_count, 2)
        self.assertEqual(result['performance']['body_fetches'], 0)
        self.assertEqual(result['performance']['headers_fetched'], 0)

    def test_connection_limit_falls_back_without_losing_files(self):
        session = FakeSession({'1': mail(), '2': mail()})
        with patch.object(session, 'fork', side_effect=q.ExportError('connection_limit')):
            result = self.run_export({'all': True}, session)
        self.assertTrue(result['complete'])
        self.assertEqual(result['counts']['saved'], 4)
        self.assertEqual(result['performance']['workers_used'], 1)
        self.assertTrue(result['warnings'])

    def test_worker_uidvalidity_change_stops_before_writes(self):
        session = FakeSession({'1': mail(), '2': mail()})
        with patch.object(session, 'fork', side_effect=q.ExportError('UIDVALIDITY_changed_restart_selection')):
            with self.assertRaises(q.ExportError):
                self.run_export({'all': True}, session)
        self.assertFalse(list(Path(self.root).rglob('*.pdf')))

    def test_concurrent_downloads_deterministic_paths_bytes(self):
        active, peak = 0, 0
        guard = threading.Lock()
        original = FakeSession.fetch
        def slow(session, uid, fields):
            nonlocal active, peak
            if 'HEADER.FIELDS' in fields:
                return original(session, uid, fields)
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04 if uid == '1' else 0.01)
                return original(session, uid, fields)
            finally:
                with guard:
                    active -= 1
        config = {'all': True, 'name_template': '{date}_{index}{ext}', 'directory': [], 'mode': 'both'}
        payloads = {'1': mail(), '2': mail(), '3': mail()}
        with patch.object(FakeSession, 'fetch', slow):
            serial = self.run_export(dict(config, workers=1), FakeSession(payloads))
            first_root = self.root
            self.root = str(Path(first_root).parent / 'parallel')
            parallel = self.run_export(dict(config, workers=2), FakeSession(payloads))
        def signature(result):
            return [(r['message_id'], r['part_id'], Path(r['path']).name, Path(r['path']).read_bytes()) for r in result['results']]
        self.assertEqual(signature(serial), signature(parallel))
        self.assertEqual(peak, 2)

    def test_bytes_import_hash_and_no_staging(self):
        result = self.run_export()
        for item in result['results']:
            self.assertEqual(item['sha256'], hashlib.sha256(Path(item['path']).read_bytes()).hexdigest())
        self.assertFalse(list(Path(self.root).rglob('imap-staging-*')))

    def test_workers_validation(self):
        for workers in (0, 5, True, '2', 1.5):
            with self.assertRaises(q.ExportError):
                q.validate({'all': True, 'workers': workers})


if __name__ == '__main__':
    unittest.main(verbosity=2)

