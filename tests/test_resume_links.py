import json
from pathlib import Path
import tempfile
import unittest
from email import policy
from email.message import EmailMessage

from test_qq_imap_export import FakeSession
import resume_export as r
from resume_links import attach, web_url


def mail(with_attachment=False):
    msg = EmailMessage(policy=policy.SMTP)
    msg['Subject'] = '转发：候选人简历'
    msg['From'] = 'recruiter@example.test'
    msg.set_content('简历 https://jobs.example.test/cv?id=1&token=private')
    msg.add_alternative('<p>转发邮件 <a href="https://jobs.example.test/cv?id=1&amp;token=private">查看简历</a>'
                        '<a href="https://jobs.example.test/unsubscribe">退订</a><a href="javascript:alert(1)">无效</a></p>', subtype='html')
    if with_attachment:
        msg.add_attachment(b'portfolio', maintype='application', subtype='zip', filename='作品.zip')
    return msg.as_bytes()


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.job = {'root': str(self.base/'final'), 'query': {'all': True}, 'follow_links': True}

    def prepare(self, attachment=False):
        report = r.prepare(self.job, self.base/'work', FakeSession({'1': mail(attachment)}))
        return json.loads(Path(report['snapshot']).read_text(encoding='utf-8')), report

    def resolve(self, snapshot):
        path = self.base/'resume.pdf'
        path.write_bytes(b'%PDF-1.7 original resume bytes')
        items = []
        for link in snapshot['links']:
            if link['text'] == '查看简历':
                items.append({'id': link['id'], 'state': 'downloaded', 'reason': 'browser downloaded actual file',
                              'files': [{'source_path': str(path), 'original_name': '候选人.pdf', 'kind': 'original_download'}]})
            else:
                items.append({'id': link['id'], 'state': 'not_resume', 'reason': 'unsubscribe navigation'})
        return {'snapshot_id': r.fingerprint(snapshot), 'items': items}

    def decisions(self, snapshot):
        return {'snapshot_id': r.fingerprint(snapshot),
                'resume_ids': [v['id'] for v in r.inventory(snapshot) if '候选人.pdf' in v['names']],
                'other_ids': [v['id'] for v in r.inventory(snapshot) if '作品.zip' in v['names']]}

    def test_forwarded_html_links_and_plain_duplicate_with_no_attachment(self):
        snapshot, report = self.prepare()
        self.assertEqual(report['phase'], 'awaiting_links')
        self.assertEqual(len(report['links']), 2)
        self.assertEqual(report['links'][0]['text'], '查看简历')
        self.assertEqual(report['links'][0]['url'], 'https://jobs.example.test/cv?id=1&token=private')
        self.assertEqual(snapshot['candidates'], [])
        self.assertEqual(r.finish(snapshot, {})['phase'], 'awaiting_links')
        self.assertFalse((self.base/'final').exists())

    def test_link_resume_saved_flat_original_bytes_and_rerun(self):
        snapshot, _ = self.prepare()
        manifest = self.resolve(snapshot)
        linked = attach(snapshot, manifest, r.fingerprint(snapshot))
        (self.base/'resume.pdf').unlink()
        result = r.finish(linked, self.decisions(linked))
        path = Path(result['results'][0]['path'])
        self.assertEqual(path, self.base/'final'/'候选人.pdf')
        self.assertEqual(path.read_bytes(), b'%PDF-1.7 original resume bytes')
        self.assertEqual(r.finish(linked, self.decisions(linked))['counts']['skipped'], 1)

    def test_link_resume_and_real_attachment_group_together_and_apply_date_names(self):
        self.job['query']['name_template'] = '{date}{ext}'
        self.job['folder_template'] = '{date}'
        snapshot, _ = self.prepare(True)
        linked = attach(snapshot, self.resolve(snapshot), r.fingerprint(snapshot))
        result = r.finish(linked, self.decisions(linked))
        paths = [Path(row['path']) for row in result['results']]
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0].parent, paths[1].parent)
        self.assertEqual(paths[0].parent, self.base/'final'/'2026-10-29')
        self.assertEqual({p.name for p in paths}, {'2026-10-29.zip', '2026-10-29.pdf'})

    def test_blocked_login_is_incomplete_and_stale_manifest_rejected(self):
        snapshot, _ = self.prepare()
        manifest = {'snapshot_id': r.fingerprint(snapshot), 'items':
                    [{'id': link['id'], 'state': 'blocked', 'reason': 'login required'} for link in snapshot['links']]}
        linked = attach(snapshot, manifest, r.fingerprint(snapshot))
        self.assertFalse(r.finish(linked, {})['complete'])
        with self.assertRaises(r.ArchiveError):
            attach(linked, manifest, r.fingerprint(linked))

    def test_login_html_masquerading_as_pdf_and_wrong_page_export_rejected(self):
        for content, kind in [(b'<html>login</html>', 'original_download'), (b'not PDF', 'page_pdf')]:
            snapshot, _ = self.prepare()
            manifest = self.resolve(snapshot)
            entry = next(i for i in manifest['items'] if i['state'] == 'downloaded')
            Path(entry['files'][0]['source_path']).write_bytes(content)
            entry['files'][0]['kind'] = kind
            with self.assertRaises(r.ArchiveError):
                attach(snapshot, manifest, r.fingerprint(snapshot))

    def test_page_pdf_records_origin_and_not_original_file_claim(self):
        snapshot, _ = self.prepare()
        manifest = self.resolve(snapshot)
        next(i for i in manifest['items'] if i['state'] == 'downloaded')['files'][0]['kind'] = 'page_pdf'
        linked = attach(snapshot, manifest, r.fingerprint(snapshot))
        self.assertEqual(linked['candidates'][0]['origin'], 'page_pdf')

    def test_follow_links_opt_in_and_invalid_schemes(self):
        self.job.pop('follow_links')
        snapshot, report = self.prepare()
        self.assertNotIn('links', snapshot)
        self.assertNotIn('links', report)
        for value in ('file:///etc/passwd', 'javascript:alert(1)', 'https://user:secret@example.test/x', 'https://example.test/\nsecret'):
            self.assertIsNone(web_url(value))


if __name__ == '__main__':
    unittest.main()
