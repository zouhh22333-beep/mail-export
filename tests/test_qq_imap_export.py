import importlib.util
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'scripts'))
import qq_imap_export as q


def mail(subject='中文报价', attachments=True):
    message = EmailMessage(policy=policy.SMTP)
    message['From'] = '供应商 <vendor@example.test>'
    message['Subject'] = subject
    message.set_content('这是完整正文')
    if attachments:
        message.add_attachment(b'%PDF-1.7\n\x00\xfforiginal bytes', maintype='application', subtype='pdf', filename='报价.pdf')
        message.add_attachment(b'second file', maintype='application', subtype='pdf', filename='报价.pdf')
    return message.as_bytes()


class FakeClient:
    def __init__(self, raws):
        self.raws = raws
        self.calls = []

    def uid(self, operation, *args):
        self.calls.append((operation, args))
        assert operation == 'SEARCH'
        return 'OK', [' '.join(self.raws).encode()]


class FakeSession:
    account, mailbox, validity = 'unit@qq.com', 'INBOX', '123'

    def __init__(self, raws=None):
        self.raws = raws or {'1': mail()}
        self.client = FakeClient(self.raws)
        self.bodies = []
        self.headers = []

    def fork(self):
        other = FakeSession(self.raws)
        other.bodies = self.bodies
        other.headers = self.headers
        other.validity, other.mailbox = self.validity, self.mailbox
        return other

    def close(self):
        pass

    def fetch(self, uid, fields):
        if 'HEADER.FIELDS' in fields:
            if ',' in uid:
                return [value for key in uid.split(',') for value in self.fetch(key, fields)]
            self.headers.append(uid)
            # Highest UID deliberately older to ensure ordering by INTERNALDATE.
            day = '29' if uid == '1' else '28'
            header = self.raws[uid].split(b'\r\n\r\n')[0] + b'\r\n\r\n'
            return [(f'1 (UID {uid} INTERNALDATE "{day}-Oct-2026 12:00:00 +0800" BODY[HEADER] {{{len(header)}}}'.encode(), header), b')']
        assert fields == '(UID BODY.PEEK[])'
        self.bodies.append(uid)
        return [(b'1 (BODY[] {100}', self.raws[uid]), b')']


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = str(Path(self.temp.name) / 'archive')

    def tearDown(self):
        self.temp.cleanup()

    def run_export(self, config=None, session=None):
        return q.export(q.validate(config or {'limit': 1}), self.root, session or FakeSession())

    def test_real_mime_binary_names_duplicates_and_rerun(self):
        session = FakeSession()
        result = self.run_export(session=session)
        self.assertEqual(result['counts']['saved'], 2)
        files = [Path(v['path']) for v in result['results']]
        self.assertEqual([v.name for v in files], ['报价.pdf', '报价 (2).pdf'])
        self.assertEqual(files[0].read_bytes(), b'%PDF-1.7\n\x00\xfforiginal bytes')
        self.assertEqual(files[1].read_bytes(), b'second file')
        self.assertEqual(self.run_export(session=session)['counts']['skipped'], 2)
        self.assertEqual(session.bodies, ['1'])

    def test_raw_exact_bytes_and_missing_file_resume(self):
        session = FakeSession()
        config = {'limit': 1, 'mode': 'both'}
        result = self.run_export(config, session)
        raw = next(Path(v['path']) for v in result['results'] if v['part_id'] == 'raw')
        self.assertEqual(raw.read_bytes(), session.raws['1'])
        raw.unlink()
        again = self.run_export(config, session)
        self.assertEqual(again['counts'], {'saved': 1, 'skipped': 2, 'failed': 0})

    def test_latest_order_and_unicode_filter(self):
        session = FakeSession({'1': mail(), '2': mail('旧中文报价')})
        result = self.run_export({'limit': 1, 'subject': '中文', 'from': '供应商'}, session)
        self.assertEqual(result['messages'][0]['uid'], '1')
        self.assertEqual(result['matched_messages'], 2)

    def test_user_template_and_directory(self):
        result = self.run_export({'limit': 1, 'name_template': '{date}_{index}{ext}', 'directory': ['指定目录']})
        self.assertEqual([Path(r['path']).name for r in result['results']], ['2026-10-29_1.pdf', '2026-10-29_2.pdf'])
        self.assertTrue(all(Path(v['path']).parent.name == '指定目录' for v in result['results']))

    def test_empty_attachments_cache_and_case_insensitive_filename(self):
        session = FakeSession({'1': mail(attachments=False)})
        first = self.run_export(session=session)
        self.assertEqual(first['messages'][0]['state'], 'no_selected_files')
        self.run_export(session=session)
        self.assertEqual(session.bodies, ['1'])
        self.assertEqual(self.run_export({'limit': 1, 'filename': '*.PDF'})['counts']['saved'], 2)

    def test_partial_failure_is_not_completed_or_cached(self):
        raw = b'From: vendor@example.test\r\nSubject: broken\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=bad.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\n%%%broken\r\n--x--\r\n'
        session = FakeSession({'1': mail(), '2': raw})
        result = self.run_export({'all': True}, session)
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['saved'], 2)
        self.assertEqual(result['counts']['failed'], 1)
        again = self.run_export({'all': True}, session)
        self.assertEqual(again['counts']['skipped'], 2)
        self.assertEqual(session.bodies.count('1'), 1)
        self.assertEqual(session.bodies.count('2'), 2)

    def test_uidvalidity_changes_isolate_new_files(self):
        session = FakeSession()
        self.run_export(session=session)
        session.validity = '999'
        result = self.run_export(session=session)
        self.assertEqual(result['counts']['saved'], 2)

    def test_validation_scope_and_credential_redaction(self):
        for config in ({}, {'limit': 0}, {'all': 'true'}, {'uids': ['1 OR ALL']}, {'limit': 1, 'password': 'secret'}, {'limit': 1, 'directory': ['..']}, {'limit': 1, 'name_template': '{subject.__class__}'}):
            with self.subTest(config=config), self.assertRaises(q.ExportError):
                q.validate(config)
        with patch.dict(q.os.environ, {'QQ_EMAIL_ACCOUNT': 'unit@qq.com', 'QQ_EMAIL_AUTH_CODE': 'SECRET'}):
            self.assertEqual(q.credentials(), ('unit@qq.com', 'SECRET'))
        with patch.dict(q.os.environ, {}, clear=True), self.assertRaises(q.ExportError) as context:
            q.credentials()
        self.assertNotIn('SECRET', str(context.exception))

    def test_single_part_attachment_and_missing_name(self):
        raw = b'From: a@example.test\r\nSubject: one\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n'
        result = self.run_export(session=FakeSession({'1': raw}))
        item = result['results'][0]
        self.assertIsNone(item['original_name'])
        self.assertEqual(Path(item['path']).name, '附件_1')
        self.assertEqual(Path(item['path']).read_bytes(), b'%PDF')

    def test_readonly_tls_and_uidvalidity_guard(self):
        client = MagicMock()
        client.login.return_value = ('OK', [])
        client.select.return_value = ('OK', [])
        client.response.return_value = ('UIDVALIDITY', [b'99'])
        session = q.Session('unit@qq.com', 'SECRET', '中文文件夹')
        with patch.object(q.imaplib, 'IMAP4_SSL', return_value=client) as factory:
            session.connect()
        self.assertTrue(factory.call_args.kwargs['ssl_context'].check_hostname)
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertEqual(session.validity, '99')
        self.assertTrue(q.imap_folder('中文文件夹').isascii())
        client.response.return_value = ('UIDVALIDITY', [b'100'])
        with patch.object(q.imaplib, 'IMAP4_SSL', return_value=client), self.assertRaises(q.ExportError):
            session.connect()
        self.assertIsNone(session.client)


if __name__ == '__main__':
    unittest.main(verbosity=2)
