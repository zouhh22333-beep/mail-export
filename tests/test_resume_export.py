import json
from pathlib import Path
import tempfile
import unittest
from test_qq_imap_export import FakeSession, mail, q
import resume_export as r


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.job = dict(root=str(self.base / 'final'), query={'all': True, 'name_template': '{date}{ext}'}, include_companions=True)
        self.session = FakeSession({'1': mail(), '2': mail('Other mail')})

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        report = r.prepare(self.job, self.base / 'work', self.session)
        snapshot = json.loads(Path(report['snapshot']).read_text(encoding='utf-8'))
        decisions = dict(snapshot_id=report['snapshot_id'], items=[dict(id=row['id'], sha256=row['sha256'],
            label='resume' if i == 0 else 'other', reason='Synthetic fixture classification') for i,row in enumerate(snapshot['candidates'])])
        return snapshot, decisions

    def test_group_companions_and_rerun(self):
        snapshot, decisions = self.prepare()
        first = r.finish(snapshot, decisions)
        self.assertEqual(first['counts'], dict(saved=2, skipped=0, failed=0))
        paths = [Path(row['path']) for row in first['results']]
        self.assertEqual(paths[0].parent, paths[1].parent)
        self.assertEqual([p.name for p in paths], ['2026-10-29.pdf', '2026-10-29 (2).pdf'])
        self.assertEqual(r.finish(snapshot, decisions)['counts']['skipped'], 2)
        self.assertEqual(len(self.session.bodies), 2)

    def test_mixed_root_and_named_folder_default_layout(self):
        self.job['folder_template'] = '{date}'
        snapshot, decisions = self.prepare()
        # First email: resume + companion; second: only one resume is kept.
        # Remove the second fixture attachment to represent a single-resume email.
        removed = snapshot['candidates'].pop()
        decisions['items'] = [d for d in decisions['items'] if d['id'] != removed['id']]
        decisions['items'][-1]['label'] = 'resume'
        decisions['snapshot_id'] = r.fingerprint(snapshot)
        result = r.finish(snapshot, decisions)
        paths = [Path(v['path']) for v in result['results']]
        self.assertEqual(paths[0].parent, self.base / 'final' / '2026-10-29')
        self.assertEqual(paths[1].parent, paths[0].parent)
        self.assertEqual(paths[2], self.base / 'final' / '2026-10-28.pdf')
        self.assertEqual(r.finish(snapshot, decisions)['counts']['skipped'], 3)

    def test_multiple_resumes_without_companion_stay_in_root(self):
        snapshot, decisions = self.prepare()
        for d in decisions['items']:
            d['label'] = 'resume'
        result = r.finish(snapshot, decisions)
        self.assertTrue(all(Path(v['path']).parent == self.base / 'final' for v in result['results']))

    def test_same_date_folders_are_separate_and_stable(self):
        self.job['folder_template'] = '统一目录'
        snapshot, decisions = self.prepare()
        decisions['items'][2]['label'] = 'resume'
        result = r.finish(snapshot, decisions)
        parents = [Path(v['path']).parent.name for v in result['results']]
        self.assertEqual(parents, ['统一目录', '统一目录', '统一目录 (2)', '统一目录 (2)'])
        self.assertEqual(r.finish(snapshot, decisions)['counts']['skipped'], 4)

    def test_unknown_companion_in_auto_requires_classification(self):
        snapshot, decisions = self.prepare()
        decisions['items'][1]['label'] = 'unknown'
        self.assertFalse(r.finish(snapshot, decisions)['complete'])

    def test_folder_template_validation(self):
        for template in ('', '{subject.__class__}', '{date:bad}'):
            with self.assertRaises(r.ArchiveError):
                r.validate_job(dict(self.job, folder_template=template))

    def test_resume_only_flat_original_name(self):
        self.job.update(layout='flat', include_companions=False)
        self.job['query'].pop('name_template')
        snapshot, decisions = self.prepare()
        result = r.finish(snapshot, decisions)
        self.assertEqual(result['counts']['saved'], 1)
        path = Path(result['results'][0]['path'])
        self.assertEqual(path.name, '报价.pdf')
        self.assertEqual(path.parent, self.base / 'final')

    def test_unknown_outside_positive_mail_blocks_completion(self):
        snapshot, decisions = self.prepare()
        decisions['items'][-1]['label'] = 'unknown'
        self.assertFalse(r.finish(snapshot, decisions)['complete'])
        self.assertFalse((self.base / 'final').exists())

    def test_changed_candidate_and_stale_manifest_rejected(self):
        snapshot, decisions = self.prepare()
        decisions['snapshot_id'] = 'stale'
        with self.assertRaises(r.ArchiveError):
            r.finish(snapshot, decisions)
        decisions['snapshot_id'] = r.fingerprint(snapshot)
        Path(snapshot['candidates'][0]['source_path']).write_bytes(b'changed')
        with self.assertRaises(r.ArchiveError):
            r.finish(snapshot, decisions)

    def test_incomplete_download_cannot_finish(self):
        snapshot, decisions = self.prepare()
        snapshot['download_complete'] = False
        with self.assertRaises(r.ArchiveError):
            r.finish(snapshot, decisions)

    def test_prepare_reuses_downloads(self):
        self.prepare()
        self.prepare()
        self.assertEqual(len(self.session.bodies), 2)

    def test_month_boundaries(self):
        for month, end in [('2026-08', '2026-09-01'), ('2026-12', '2027-01-01'), ('2024-02', '2024-03-01')]:
            self.assertEqual(q.validate({'month': month})['before'], end)
        for value in ['2026-13', '2026-00', '2026-8']:
            with self.assertRaises(q.ExportError):
                q.validate({'month': value})


if __name__ == '__main__':
    unittest.main()

