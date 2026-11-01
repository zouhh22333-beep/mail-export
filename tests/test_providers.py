import contextlib
import io
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

from test_qq_imap_export import FakeSession, mail, q
import resume_export as resume
from mail_provider import profile_for


class ProtocolClient:
    def __init__(self, raws, events):
        self.fake = FakeSession(raws)
        self.events = events
        self.abort_next = False
        self.id_status = 'OK'
        self.caps = b'IMAP4rev1 ID'

    def login(self, account, secret):
        self.events.append(('login', account))
        return 'OK', []

    def capability(self):
        self.events.append(('capability',))
        return 'OK', [self.caps]

    def xatom(self, name, fields):
        self.events.append((name, fields))
        return self.id_status, []

    def select(self, folder, readonly=False):
        self.events.append(('select', folder, readonly))
        return 'OK', []

    def response(self, code):
        return code, [b'123']

    def uid(self, command, *args):
        self.events.append((command, *args))
        if command == 'SEARCH':
            return self.fake.client.uid(command, *args)
        if command != 'FETCH':
            raise AssertionError('Unexpected mutating IMAP command: ' + command)
        if self.abort_next:
            self.abort_next = False
            raise q.imaplib.IMAP4.abort('connection lost')
        return 'OK', self.fake.fetch(*args)

    def logout(self):
        self.events.append(('logout',))
        return 'BYE', []

    def list(self):
        chinese = q.imap_folder('已发送').encode()
        return 'OK', [b'(\\Sent) "/" ' + chinese, b'(\\Noselect) "/" "Parent"',
                      (b'() "/" {5}', b'INBOX'), b'() "/" "A &- B"']


def message(files):
    msg = EmailMessage(policy=policy.SMTP)
    msg['Subject'] = '应聘材料'
    msg['From'] = 'candidate@example.test'
    msg.set_content('请查看随附材料。')
    for name, content in files:
        msg.add_attachment(content, maintype='application', subtype='octet-stream', filename=name)
    return msg.as_bytes()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.base = Path(self.temp.name)
        self.events, self.clients, self.endpoints = [], [], []
        self.raws = {'1': mail(), '2': mail('another')}

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, host, port, *, ssl_context, timeout):
        self.assertTrue(ssl_context.check_hostname)
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.endpoints.append((host, port))
        client = ProtocolClient(self.raws, self.events)
        self.clients.append(client)
        return client

    def test_netease_domains_tls_parallel_and_cache(self):
        for domain in ('163.com', '126.com', 'yeah.net'):
            with self.subTest(domain=domain), patch.object(q.imaplib, 'IMAP4_SSL', side_effect=self.factory):
                config = q.validate({'provider': 'netease', 'all': True})
                session = q.Session('unit@' + domain, 'not-a-real-secret', 'INBOX', config)
                try:
                    session.connect()
                    root = str(self.base / domain)
                    first = q.export(config, root, session)
                    second = q.export(config, root, session)
                    self.assertEqual(first['counts']['saved'], 4)
                    self.assertEqual(first['performance']['workers_used'], 2)
                    self.assertEqual(second['counts']['skipped'], 4)
                    self.assertEqual(second['performance']['body_fetches'], 0)
                    self.assertEqual(second['performance']['headers_fetched'], 0)
                    self.assertEqual(first['mode'], 'netease-imap')
                    self.assertEqual(self.endpoints[-2:], [('imap.' + domain, 993)] * 2)
                    for row in first['results']:
                        self.assertTrue(Path(row['path']).is_file())
                finally:
                    session.close()
        self.assertEqual(sum(e[0] == 'ID' for e in self.events), 6)
        self.assertTrue(all(e[2] for e in self.events if e[0] == 'select'))

    def test_reconnect_repeats_ID_before_readonly_select(self):
        with patch.object(q.imaplib, 'IMAP4_SSL', side_effect=self.factory):
            session = q.Session('unit@163.com', 'secret', '已发送', {'provider': 'netease'})
            session.connect()
            self.clients[0].abort_next = True
            data = session.fetch('1', '(UID BODY.PEEK[])')
            self.assertEqual(q.literal(data), self.raws['1'])
            self.assertEqual([e[0] for e in self.events],
                             ['login', 'capability', 'ID', 'select', 'FETCH', 'logout',
                              'login', 'capability', 'ID', 'select', 'FETCH'])
            session.close()

    def test_missing_ID_capability_and_rejected_ID(self):
        client = ProtocolClient(self.raws, self.events)
        with patch.object(q.imaplib, 'IMAP4_SSL', return_value=client):
            session = q.Session('unit@126.com', 'secret', 'INBOX', {'provider': 'netease'})
            client.caps = b'IMAP4rev1'
            session.connect()
            self.assertFalse(any(e[0] == 'ID' for e in self.events))
            session.close()
            client.caps = b'IMAP4rev1 ID'
            client.id_status = 'NO'
            with self.assertRaisesRegex(q.ExportError, 'client_ID_failed'):
                session.connect()
            self.assertIsNone(session.client)

    def test_list_chinese_sent_folder_without_select_or_body(self):
        with patch.object(q.imaplib, 'IMAP4_SSL', side_effect=self.factory):
            session = q.Session('unit@yeah.net', 'secret', 'INBOX', {'provider': 'netease'})
            session.connect(select=False)
            folders = q.list_mailboxes(session)
            self.assertEqual([f['name'] for f in folders], ['已发送', 'Parent', 'INBOX', 'A & B'])
            self.assertFalse(folders[1]['selectable'])
            self.assertFalse(any(e[0] in ('select', 'FETCH') for e in self.events))
            session.close()

    def test_complete_netease_resume_workflow_grouping_and_original_bytes(self):
        self.raws = {
            '1': message([('简历.pdf', b'resume-a'), ('作品.zip', b'companion')]),
            '2': message([('简历.pdf', b'resume-b')]),
            '3': message([('甲简历.docx', b'resume-c'), ('乙简历.docx', b'resume-d')]),
        }
        job = {'root': str(self.base / 'final'), 'query': {'provider': 'netease', 'all': True},
               'folder_template': '{date}'}
        with patch.object(q.imaplib, 'IMAP4_SSL', side_effect=self.factory):
            session = q.Session('unit@163.com', 'secret', '已发送', job['query'])
            session.connect()
            try:
                prepared = resume.prepare(job, self.base / 'work', session)
                snapshot = json.loads(Path(prepared['snapshot']).read_text(encoding='utf-8'))
                decisions = {'snapshot_id': prepared['snapshot_id'], 'resume_ids': [], 'other_ids': []}
                for candidate in prepared['candidates']:
                    field = 'other_ids' if '作品.zip' in candidate['names'] else 'resume_ids'
                    decisions[field].append(candidate['id'])
                report = resume.finish(snapshot, decisions)
                self.assertEqual(report['counts'], {'saved': 5, 'skipped': 0, 'failed': 0})
                self.assertEqual(report['companion_files'], 1)
                paths = [Path(row['path']) for row in report['results']]
                self.assertEqual(paths[0].parent, self.base / 'final' / '2026-10-29')
                self.assertEqual(paths[1].parent, paths[0].parent)
                self.assertTrue(all(p.parent == self.base / 'final' for p in paths[2:]))
                self.assertEqual({p.read_bytes() for p in paths},
                                 {b'resume-a', b'companion', b'resume-b', b'resume-c', b'resume-d'})
                self.assertEqual(paths[0].name, '简历.pdf')
                self.assertEqual(resume.finish(snapshot, decisions)['counts']['skipped'], 5)
                again = resume.prepare(job, self.base / 'work', session)
                self.assertEqual(again['performance']['body_fetches'], 0)
                self.assertEqual(again['snapshot_id'], prepared['snapshot_id'])
            finally:
                session.close()

    def test_custom_endpoint_cache_isolation_and_QQ_legacy_index(self):
        root = str(self.base / 'archive')
        # Old QQ identities from an already exported job must still be skipped.
        old = q.export(q.validate({'all': True}), root, FakeSession(self.raws))
        with patch.object(q.imaplib, 'IMAP4_SSL', side_effect=self.factory):
            configs = [{}, {'provider': 'custom', 'connection': {'host': 'imap.one.test'}},
                       {'provider': 'custom', 'connection': {'host': 'imap.two.test'}}]
            for index, config in enumerate(configs):
                config = q.validate(dict(config, all=True))
                session = q.Session('unit@qq.com', 'secret', 'INBOX', config)
                session.connect()
                try:
                    result = q.export(config, root, session)
                    expected = 'skipped' if index == 0 else 'saved'
                    self.assertEqual(result['counts'][expected], 4)
                    self.assertEqual(result['performance']['headers_fetched'], 0 if index == 0 else 2)
                    self.assertEqual(q.export(config, root, session)['counts']['skipped'], 4)
                finally:
                    session.close()
        self.assertEqual(old['counts']['saved'], 4)

    def test_credentials_explicit_provider_and_custom_environment(self):
        env = {'QQ_EMAIL_ACCOUNT': 'unit@qq.com', 'QQ_EMAIL_AUTH_CODE': 'qq-secret',
               'NETEASE_EMAIL_ACCOUNT': 'unit@126.com', 'NETEASE_EMAIL_AUTH_CODE': 'netease-secret',
               'COMPANY_MAIL': 'hr@example.test', 'COMPANY_CODE': 'enterprise-secret'}
        with patch.dict(q.os.environ, env, clear=True):
            self.assertEqual(q.credentials()[0], 'unit@qq.com')
            self.assertEqual(q.credentials({'provider': 'netease'})[0], 'unit@126.com')
            conf = {'provider': 'custom', 'connection': {'host': 'imap.example.test',
                    'account_env': 'COMPANY_MAIL', 'auth_code_env': 'COMPANY_CODE'}}
            self.assertEqual(q.credentials(conf), ('hr@example.test', 'enterprise-secret'))
        with patch.dict(q.os.environ, {'QQ_EMAIL_ACCOUNT': 'unit@qq.com', 'QQ_EMAIL_AUTH_CODE': 'qq-secret'}, clear=True):
            with self.assertRaisesRegex(q.ExportError, 'missing_NETEASE'):
                q.credentials({'provider': 'netease'})

    def test_invalid_provider_settings_fail_before_network(self):
        configs = [{'provider': []}, {'provider': 'gmail'}, {'provider': 'netease', 'connection': {'host': 'evil.test'}},
                   {'provider': 'custom'}, {'provider': 'custom', 'connection': {'host': 'https://imap.example.test'}},
                   {'provider': 'custom', 'connection': {'host': 'imap.example.test', 'port': True}},
                   {'provider': 'custom', 'connection': {'host': 'imap.example.test', 'password': 'SECRET'}},
                   {'provider': 'custom', 'connection': {'host': 'imap.example.test', 'send_id': 'yes'}}]
        for config in configs:
            with self.subTest(config=config), self.assertRaises(q.ExportError) as ctx:
                q.validate(dict(config, all=True))
            self.assertNotIn('SECRET', str(ctx.exception))
        with self.assertRaises(q.ExportError):
            profile_for('unit@163.com.attacker.test', {'provider': 'netease'})

    def test_check_config_cli_no_secret_or_network(self):
        config = self.base / 'config.json'
        config.write_text('{"provider":"netease"}', encoding='utf-8')
        output = io.StringIO()
        with patch.dict(q.os.environ, {'NETEASE_EMAIL_ACCOUNT': 'unit@163.com', 'NETEASE_EMAIL_AUTH_CODE': 'SECRET'}, clear=True), \
                patch.object(q.sys, 'argv', ['imap_export.py', '--check-config', '--config', str(config)]), \
                patch.object(q.imaplib, 'IMAP4_SSL') as factory, contextlib.redirect_stdout(output):
            self.assertEqual(q.main(), 0)
            factory.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['network_checked'])
        self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
