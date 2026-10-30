#!/usr/bin/env python3
"""Prepare local resume candidates, then archive an explicit classification manifest."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import string
import tempfile
from time import perf_counter

import qq_imap_export as imap
from archive_store import Store, ArchiveError, digest_file, safe_name, collision_name


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_job(job):
    if not isinstance(job, dict) or set(job) - {'root', 'query', 'layout', 'include_companions', 'folder_template'}:
        raise ArchiveError('invalid_job_fields')
    if not isinstance(job.get('root'), str) or not Path(job['root']).is_absolute():
        raise ArchiveError('root_must_be_absolute')
    result = dict(job)
    result['query'] = imap.validate(job.get('query'))
    if result['query']['mode'] != 'attachments':
        raise ArchiveError('resume_workflow_requires_attachment_mode')
    result.setdefault('layout', 'auto')
    result.setdefault('include_companions', True)
    if result['layout'] not in ('auto', 'message', 'flat') or type(result['include_companions']) is not bool:
        raise ArchiveError('invalid_layout_or_include_companions')
    if 'folder_template' in result:
        template = result['folder_template']
        try:
            if not isinstance(template, str) or not template.strip():
                raise ValueError()
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is not None and (field not in {'date', 'subject', 'sender', 'uid', 'index'} or spec or conversion):
                    raise ValueError()
        except ValueError:
            raise ArchiveError('invalid_folder_template') from None
    if result['include_companions'] and 'filename' in result['query']:
        raise ArchiveError('companion_export_requires_unfiltered_attachment_inventory')
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temp = Path(output.name)
            output.write(encoded(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def prepare(job, work, session):
    job = validate_job(job)
    query = dict(job['query'], mode='both')
    # Candidate names remain original; final naming is applied exactly once.
    query.pop('name_template', None)
    query.pop('directory', None)
    work = Path(work)
    if not work.is_absolute():
        raise ArchiveError('work_must_be_absolute')
    run = work / fingerprint({'job': job, 'account': session.account.casefold()})
    stage = run / 'candidates'
    download = imap.export(query, str(stage), session)
    rows, context = [], {}
    store = Store(str(stage))
    try:
        account = 'qq-imap:' + session.account.casefold()
        for result in download['results']:
            if result['state'] not in ('saved', 'skipped'):
                continue
            row = store.row((account, result['message_id'], result['part_id']))
            mailbox, validity, uid = json.loads(row['message_id'])
            meta = store.db.execute('SELECT subject,sender,received FROM imap_headers_v1 WHERE account=? AND mailbox=? AND validity=? AND uid=?',
                                    (session.account.casefold(), mailbox, validity, uid)).fetchone()
            path = str(store.destination(store.root / row['relative_path']))
            if row['kind'] == 'email':
                context[row['message_id']] = path
                continue
            rows.append(dict(id=fingerprint([account, row['message_id'], row['part_id']]),
                message_id=row['message_id'], part_id=row['part_id'], subject=meta['subject'], sender=meta['sender'],
                date=datetime.fromisoformat(meta['received']).date().isoformat(), uid=uid,
                original_name=row['original_name'], source_path=path, sha256=row['sha256'], size=row['size']))
    finally:
        store.close()
    snapshot = dict(version=1, job=job, account=account, candidates=rows, context_eml=context,
                    download_complete=download['complete'])
    snapshot_id = fingerprint(snapshot)
    write_json(run / 'snapshot.json', snapshot)
    return dict(complete=False, phase='awaiting_classification', snapshot=str(run / 'snapshot.json'),
                snapshot_id=snapshot_id, candidates=rows, context_eml=context,
                download_complete=download['complete'], performance=download['performance'],
                download_counts=download['counts'], download_failures=[r for r in download['results'] if r['state'] == 'failed'])


def group_directory(store, account, row, prefix, template, index):
    store.db.execute('CREATE TABLE IF NOT EXISTS resume_groups (account TEXT, message TEXT, directory TEXT UNIQUE, PRIMARY KEY(account,message))')
    saved = store.db.execute('SELECT directory FROM resume_groups WHERE account=? AND message=?', (account, row['message_id'])).fetchone()
    if saved:
        path = store.destination(store.root / saved['directory'])
        path.mkdir(parents=True, exist_ok=True)
        return list(path.relative_to(store.root).parts)
    name = safe_name(template.format(date=row['date'], subject=row['subject'] or '无主题',
                                    sender=row['sender'], uid=row['uid'], index=index))
    parent = store.destination(store.root.joinpath(*(safe_name(p) for p in prefix)))
    parent.mkdir(parents=True, exist_ok=True)
    allocated = {r[0].casefold() for r in store.db.execute('SELECT directory FROM resume_groups')}
    number = 1
    while True:
        path = store.destination(parent / collision_name(name, number))
        relative = path.relative_to(store.root)
        occupied = {p.name.casefold() for p in parent.iterdir()}
        if str(relative).casefold() not in allocated and path.name.casefold() not in occupied:
            try:
                path.mkdir()
                break
            except FileExistsError:
                pass
        number += 1
    store.db.execute('INSERT INTO resume_groups VALUES (?,?,?)', (account, row['message_id'], str(relative)))
    store.db.commit()
    return list(relative.parts)


def finish(snapshot, decisions):
    started = perf_counter()
    job = validate_job(snapshot['job'])
    if not snapshot.get('download_complete'):
        raise ArchiveError('candidate_download_incomplete_rerun_prepare')
    if not isinstance(decisions, dict) or decisions.get('snapshot_id') != fingerprint(snapshot):
        raise ArchiveError('classification_snapshot_mismatch')
    by_id = {row['id']: row for row in snapshot['candidates']}
    labels = {}
    for decision in decisions.get('items', []):
        key = decision.get('id')
        if key not in by_id or key in labels:
            raise ArchiveError('unknown_or_duplicate_classification_id')
        if decision.get('sha256') != by_id[key]['sha256']:
            raise ArchiveError('classification_content_mismatch')
        if decision.get('label') not in ('resume', 'other', 'unknown'):
            raise ArchiveError('invalid_classification_label')
        if not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
            raise ArchiveError('classification_evidence_required')
        labels[key] = decision['label']
    positive = {row['message_id'] for key, row in by_id.items() if labels.get(key) == 'resume'}
    unresolved = [key for key in by_id if labels.get(key, 'unknown') == 'unknown' and
                  not (job['layout'] != 'auto' and job['include_companions'] and by_id[key]['message_id'] in positive)]
    if unresolved:
        return dict(complete=False, phase='awaiting_classification', unresolved=unresolved)
    # Verify all classified bytes, including exclusions, before publishing any file.
    for row in by_id.values():
        if digest_file(Path(row['source_path'])) != (row['size'], row['sha256']):
            raise ArchiveError('candidate_changed_reprepare_and_reclassify')
    selected = [row for row in snapshot['candidates'] if labels.get(row['id']) == 'resume' or
                (job['include_companions'] and row['message_id'] in positive)]
    with_companions = {row['message_id'] for row in selected if labels.get(row['id']) != 'resume'}
    message_order = {mid: i for i, mid in enumerate(dict.fromkeys(row['message_id'] for row in selected), 1)}
    results = []
    store = Store(job['root'])
    try:
        for index, row in enumerate(selected, 1):
            item = dict(message_id=row['message_id'], part_id=row['part_id'], kind='attachment',
                        subject=row['subject'], original_name=row['original_name'], source_path=row['source_path'])
            fallback = '附件_' + row['part_id'].split(':', 1)[-1]
            item['fallback_name'] = fallback
            directory = list(job['query'].get('directory', []))
            if job['layout'] == 'message' or (job['layout'] == 'auto' and row['message_id'] in with_companions):
                directory = group_directory(store, snapshot['account'], row, directory,
                    job.get('folder_template', '{subject}'), message_order[row['message_id']])
            item['directory'] = directory
            if 'name_template' in job['query']:
                filename = row['original_name'] or fallback
                item['save_name'] = job['query']['name_template'].format(date=row['date'], subject=row['subject'],
                    sender=row['sender'], index=index, uid=row['uid'], part=row['part_id'], original=filename,
                    stem=Path(filename).stem, ext=Path(filename).suffix)
            results.append(store.import_one(snapshot['account'], item))
        # Classification evidence stays local alongside the archive index.
        record = store.confined(store.root / '.mail-export' / ('classification-' + fingerprint(snapshot) + '.json'))
        write_json(record, decisions)
    finally:
        store.close()
    counts = {state: sum(r['state'] == state for r in results) for state in ('saved', 'skipped', 'failed')}
    return dict(complete=not counts['failed'], phase='finished', root=job['root'], results=results, counts=counts,
                resume_files=sum(v == 'resume' for v in labels.values()), selected_messages=len(positive),
                companion_files=len(selected) - sum(v == 'resume' for v in labels.values()),
                excluded_files=len(by_id) - len(selected), finish_seconds=round(perf_counter() - started, 4))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'finish'))
    parser.add_argument('--job')
    parser.add_argument('--work')
    parser.add_argument('--snapshot')
    parser.add_argument('--decisions')
    args = parser.parse_args()
    session = None
    try:
        read = lambda path: json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if args.phase == 'prepare':
            if not args.job or not args.work:
                raise ArchiveError('job_and_work_required')
            job = validate_job(read(args.job))
            account, secret = imap.credentials()
            session = imap.Session(account, secret, job['query']['mailbox'])
            session.connect()
            result = prepare(job, args.work, session)
        else:
            if not args.snapshot or not args.decisions:
                raise ArchiveError('snapshot_and_decisions_required')
            result = finish(read(args.snapshot), read(args.decisions))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('complete') or (args.phase == 'prepare' and result.get('download_complete')) else 1
    except (ArchiveError, imap.ExportError, OSError, ValueError, KeyError, TypeError, imap.imaplib.IMAP4.error) as exc:
        print(json.dumps(dict(complete=False, error=str(exc) if isinstance(exc, (ArchiveError, imap.ExportError)) else type(exc).__name__)))
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())

