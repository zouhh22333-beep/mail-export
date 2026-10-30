#!/usr/bin/env python3
"""Download real QQ mail/attachment bytes via read-only IMAP; Python 3.10+."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.errors import InvalidBase64PaddingDefect, InvalidBase64CharactersDefect, InvalidBase64LengthDefect
from email.parser import BytesParser
from email.utils import parseaddr
import fnmatch
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import string
import sys
from time import perf_counter

from archive_store import Store, ArchiveError

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
PARSER = BytesParser(policy=policy.default)
BAD_BASE64 = (InvalidBase64PaddingDefect, InvalidBase64CharactersDefect, InvalidBase64LengthDefect)


class ExportError(Exception):
    pass


def credentials():
    account = os.environ.get('QQ_EMAIL_ACCOUNT', '').strip()
    secret = os.environ.get('QQ_EMAIL_AUTH_CODE', '').strip()
    if not account or not secret:
        raise ExportError('missing_QQ_EMAIL_ACCOUNT_or_QQ_EMAIL_AUTH_CODE')
    if not re.fullmatch(r'[^\s@]+@(qq|foxmail)\.com', account, re.I):
        raise ExportError('QQ_EMAIL_ACCOUNT_must_be_full_qq_or_foxmail_address')
    return account, secret


def validate(config):
    if not isinstance(config, dict):
        raise ExportError('config_must_be_object')
    allowed = {'mailbox', 'since', 'before', 'limit', 'all', 'uids', 'from', 'subject',
               'mode', 'filename', 'include_inline', 'name_template', 'directory', 'workers', 'month'}
    if set(config) - allowed:
        raise ExportError('unknown_config_fields:' + ','.join(sorted(set(config) - allowed)))
    result = dict(config)
    if 'month' in result:
        month = result.pop('month')
        if not isinstance(month, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month) or 'since' in result or 'before' in result:
            raise ExportError('month_requires_YYYY_MM_without_since_or_before')
        try:
            first = date.fromisoformat(month + '-01')
            following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        except ValueError:
            raise ExportError('invalid_month') from None
        result.update(since=first.isoformat(), before=following.isoformat())
    if type(result.get('workers', 2)) is not int or not 1 <= result.get('workers', 2) <= 4:
        raise ExportError('workers_must_be_integer_1_to_4')
    result.setdefault('workers', 2)
    for key in ('mailbox', 'from', 'subject', 'filename', 'name_template'):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ExportError(key + '_must_be_nonempty_string')
    result.setdefault('mailbox', 'INBOX')
    if any(ord(c) < 32 for c in result['mailbox']):
        raise ExportError('invalid_mailbox')
    for key in ('since', 'before'):
        if key in result:
            try:
                result[key] = date.fromisoformat(result[key]).isoformat()
            except (TypeError, ValueError):
                raise ExportError(key + '_must_be_YYYY-MM-DD') from None
    if 'since' in result and 'before' in result and result['since'] >= result['before']:
        raise ExportError('before_must_be_after_since')
    if 'limit' in result and (type(result['limit']) is not int or result['limit'] < 1):
        raise ExportError('limit_must_be_positive_integer')
    if 'uids' in result:
        uids = result['uids']
        if not isinstance(uids, list) or not uids or any(not re.fullmatch(r'[1-9][0-9]*', str(v)) for v in uids):
            raise ExportError('uids_must_be_nonempty_positive_IMAP_UID_list')
        result['uids'] = sorted(set(str(v) for v in uids), key=int)
    for key in ('all', 'include_inline'):
        if key in result and type(result[key]) is not bool:
            raise ExportError(key + '_must_be_boolean')
    if not any(k in result for k in ('since', 'before', 'limit', 'uids')) and not result.get('all'):
        raise ExportError('explicit_scope_required_use_dates_limit_uids_or_all_true')
    if result.get('mode', 'attachments') not in ('attachments', 'raw', 'both'):
        raise ExportError('mode_must_be_attachments_raw_or_both')
    result.setdefault('mode', 'attachments')
    if 'name_template' in result:
        try:
            for _, field, spec, conversion in string.Formatter().parse(result['name_template']):
                if field is not None and (field not in {'date', 'subject', 'sender', 'index', 'uid', 'part', 'original', 'stem', 'ext'} or spec or conversion):
                    raise ValueError()
        except ValueError:
            raise ExportError('invalid_name_template_fields_or_format') from None
    if 'directory' in result:
        parts = result['directory']
        if not isinstance(parts, list) or any(not isinstance(v, str) or not v.strip() or v in ('.', '..') or any(c in v for c in '/\\:') for v in parts):
            raise ExportError('directory_must_be_relative_components')
    return result


def imap_folder(value):
    # IMAP modified UTF-7, then quote even folders containing spaces/backslashes.
    out, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            out.append('&' + encoded + '-')
            pending.clear()
    for char in value:
        if 32 <= ord(char) <= 126:
            flush()
            out.append('&-' if char == '&' else char)
        else:
            pending.append(char)
    flush()
    return '"' + ''.join(out).replace('\\', '\\\\').replace('"', '\\"') + '"'


def ok(response, label):
    status, data = response
    if status != 'OK':
        raise ExportError(label + '_failed')
    return data


def literal(data):
    parts = [value[1] for value in data if isinstance(value, tuple) and isinstance(value[1], bytes)]
    if len(parts) != 1:
        raise ExportError('expected_one_complete_IMAP_literal')
    return parts[0]


def received_at(data):
    head = b' '.join(v[0] if isinstance(v, tuple) else v for v in data if isinstance(v, (tuple, bytes)))
    match = re.search(rb'INTERNALDATE "(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"', head)
    if not match:
        raise ExportError('missing_INTERNALDATE_cannot_order_latest')
    day, month, year, hour, minute, second, sign, zoneh, zonem = [v.decode() for v in match.groups()]
    offset = timedelta(hours=int(zoneh), minutes=int(zonem)) * (-1 if sign == '-' else 1)
    return datetime(int(year), MONTHS.index(month.title()) + 1, int(day), int(hour), int(minute), int(second), tzinfo=timezone(offset))


def selected_attachments(message, include_inline=False, position=''):
    """Stable MIME tree positions; do not descend into attached messages."""
    if not message.is_multipart():
        disposition = message.get_content_disposition()
        if disposition == 'attachment' or (message.get_filename() and (disposition != 'inline' or include_inline)):
            yield position or '1', message
        return
    for index, part in enumerate(message.iter_parts(), 1) if message.is_multipart() else []:
        key = f'{position}.{index}' if position else str(index)
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if disposition == 'attachment' or (filename and (disposition != 'inline' or include_inline)):
            yield key, part
        elif part.is_multipart() and part.get_content_type() != 'message/rfc822':
            yield from selected_attachments(part, include_inline, key)


class Session:
    def __init__(self, account, secret, mailbox):
        self.account, self.secret, self.mailbox = account, secret, mailbox
        self.client = None
        self.validity = None

    def fork(self):
        other = Session(self.account, self.secret, self.mailbox)
        other.validity = self.validity
        try:
            other.connect()
            return other
        except Exception:
            other.close()
            raise

    def connect(self):
        self.close()
        self.client = imaplib.IMAP4_SSL('imap.qq.com', 993, ssl_context=ssl.create_default_context(), timeout=60)
        try:
            ok(self.client.login(self.account, self.secret), 'authentication')
        except imaplib.IMAP4.error:
            raise ExportError('authentication_failed_check_IMAP_service_and_authorization_code') from None
        ok(self.client.select(imap_folder(self.mailbox), readonly=True), 'readonly_select')
        _, values = self.client.response('UIDVALIDITY')
        value = next((v.decode() for v in (values or []) if isinstance(v, bytes) and v.isdigit()), None)
        if not value:
            raise ExportError('missing_UIDVALIDITY')
        if self.validity is not None and self.validity != value:
            self.close()
            raise ExportError('UIDVALIDITY_changed_restart_selection')
        self.validity = value

    def fetch(self, uid, fields):
        for attempt in range(3):
            try:
                return ok(self.client.uid('FETCH', uid, fields), 'fetch')
            except (imaplib.IMAP4.abort, OSError):
                if attempt == 2:
                    raise ExportError('network_fetch_failed_retry_run') from None
                self.connect()
        raise ExportError('fetch_failed')

    def close(self):
        if self.client is not None:
            try:
                self.client.logout()  # No STORE, CLOSE or EXPUNGE.
            except (imaplib.IMAP4.error, OSError):
                pass
            self.client = None


def candidates(session, config, db=None, metrics=None):
    criteria = []
    for key, word in (('since', 'SINCE'), ('before', 'BEFORE')):
        if key in config:
            d = date.fromisoformat(config[key])
            criteria.extend([word, f'{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}'])
    data = ok(session.client.uid('SEARCH', None, *(criteria or ['ALL'])), 'search')
    found = data[0].decode().split() if data and isinstance(data[0], bytes) else []
    if any(not v.isdigit() for v in found):
        raise ExportError('invalid_search_UID_response')
    if 'uids' in config:
        missing = set(config['uids']) - set(found)
        if missing:
            raise ExportError('requested_UID_missing_or_outside_date_range:' + ','.join(sorted(missing, key=int)))
        found = config['uids']
    found = list(dict.fromkeys(found))
    scope = (session.account.casefold(), session.mailbox, session.validity)
    metadata = {}
    if db is not None:
        db.execute('CREATE TABLE IF NOT EXISTS imap_headers_v1 (account TEXT, mailbox TEXT, validity TEXT, uid TEXT, subject TEXT, sender TEXT, received TEXT, PRIMARY KEY(account,mailbox,validity,uid))')
        for offset in range(0, len(found), 400):
            batch = found[offset:offset + 400]
            query = 'SELECT uid,subject,sender,received FROM imap_headers_v1 WHERE account=? AND mailbox=? AND validity=? AND uid IN (' + ','.join('?' for _ in batch) + ')'
            for row in db.execute(query, (*scope, *batch)):
                try:
                    moment = datetime.fromisoformat(row['received'])
                    if moment.tzinfo is None:
                        continue
                    metadata[row['uid']] = dict(uid=row['uid'], subject=row['subject'], sender=row['sender'], received=moment)
                except (TypeError, ValueError):
                    pass  # Incomplete cache records are fetched again.
    if metrics is not None:
        metrics['header_cache_hits'] = len(metadata)
        metrics['headers_fetched'] = len(found) - len(metadata)
    pending = [uid for uid in found if uid not in metadata]
    for offset in range(0, len(pending), 100):
        batch = pending[offset:offset + 100]
        data = session.fetch(','.join(batch), '(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])')
        seen = set()
        fresh = []
        for response in data:
            if not isinstance(response, tuple) or len(response) != 2:
                continue
            match = re.search(rb'\bUID (\d+)\b', response[0])
            if not match or match.group(1).decode() not in batch:
                raise ExportError('unexpected_metadata_UID')
            uid = match.group(1).decode()
            if uid in seen:
                raise ExportError('duplicate_metadata_UID')
            seen.add(uid)
            header = PARSER.parsebytes(literal([response]))
            subject, sender = str(header.get('Subject', '')), str(header.get('From', ''))
            moment = received_at([response])
            metadata[uid] = dict(uid=uid, subject=subject, sender=sender, received=moment)
            fresh.append((*scope, uid, subject, sender, moment.isoformat()))
        if seen != set(batch):
            raise ExportError('metadata_UID_missing_retry_selection')
        if db is not None:
            db.executemany('INSERT OR REPLACE INTO imap_headers_v1 VALUES (?,?,?,?,?,?,?)', fresh)
            db.commit()
    result = []
    for uid in found:
        meta = metadata[uid]
        if config.get('subject', '').casefold() in meta['subject'].casefold() and config.get('from', '').casefold() in meta['sender'].casefold():
            result.append(dict(meta, sender=parseaddr(meta['sender'])[1] or meta['sender']))
    result.sort(key=lambda item: (item['received'], int(item['uid'])), reverse=True)
    return len(found), len(result), result[:config['limit']] if 'limit' in config else result


class Downloads:
    """Small bounded batches; each client has at most one in-flight command."""
    def __init__(self, session, workers):
        self.sessions = [session]
        self.workers = workers
        self.pool = None
        self.warning = None
        self.expanded = False

    def fetch_batch(self, messages):
        if not messages:
            return []
        if len(messages) > 1 and not self.expanded:
            self.expanded = True
            for _ in range(1, min(self.workers, len(messages))):
                try:
                    self.sessions.append(self.sessions[0].fork())
                except (ExportError, imaplib.IMAP4.error, OSError) as exc:
                    if str(exc) == 'UIDVALIDITY_changed_restart_selection':
                        raise
                    self.warning = 'extra_connection_unavailable_using_fewer_workers'
                    break
            if len(self.sessions) > 1:
                self.pool = ThreadPoolExecutor(max_workers=len(self.sessions))

        def fetch_one(session, meta):
            try:
                return literal(session.fetch(meta['uid'], '(UID BODY.PEEK[])'))
            except (ExportError, imaplib.IMAP4.error, OSError, ValueError) as exc:
                return exc

        result = []
        for offset in range(0, len(messages), len(self.sessions)):
            group = messages[offset:offset + len(self.sessions)]
            if self.pool is None:
                result.extend(fetch_one(self.sessions[0], meta) for meta in group)
            else:
                futures = [self.pool.submit(fetch_one, session, meta) for session, meta in zip(self.sessions, group)]
                result.extend(future.result() for future in futures)
        if any(isinstance(value, ExportError) and str(value) == 'UIDVALIDITY_changed_restart_selection' for value in result):
            raise ExportError('UIDVALIDITY_changed_restart_selection')
        return result

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
        for session in self.sessions[1:]:
            session.close()


def artifacts_from_raw(raw, meta, config):
    message = PARSER.parsebytes(raw)
    if config['mode'] in ('raw', 'both'):
        yield 'raw', 'email', None, (meta['subject'] or '无主题') + '.eml', raw, None
    if config['mode'] in ('attachments', 'both'):
        for position, part in selected_attachments(message, config.get('include_inline', False)):
            original = part.get_filename()
            fallback = '附件_' + position
            if config.get('filename') and not fnmatch.fnmatchcase((original or fallback).casefold(), config['filename'].casefold()):
                continue
            value = part.get_payload(decode=True)
            error = None
            if not isinstance(value, bytes) or any(isinstance(v, BAD_BASE64) for v in part.defects):
                error = 'unsupported_or_invalid_MIME_attachment'
            yield 'attachment:' + position, 'attachment', original, fallback, value, error


def export(config, root, session):
    started = perf_counter()
    store = Store(root)
    downloads = Downloads(session, config.get('workers', 2))
    metrics = {'body_fetches': 0, 'body_bytes': 0, 'download_seconds': 0.0, 'verify_seconds': 0.0, 'save_seconds': 0.0}
    try:
        store.db.execute('CREATE TABLE IF NOT EXISTS imap_scans (account TEXT, message TEXT, selection TEXT, parts TEXT, PRIMARY KEY(account,message,selection))')
        store.db.commit()
        account = 'qq-imap:' + session.account.casefold()
        tick = perf_counter()
        scanned, matched, messages = candidates(session, config, store.db, metrics)
        metrics['selection_seconds'] = perf_counter() - tick
        selection = json.dumps({k: config.get(k) for k in ('mode', 'filename', 'include_inline')}, sort_keys=True)
        report = {'mode': 'qq-imap', 'root': str(store.root), 'candidate_messages': scanned, 'matched_messages': matched,
                  'selected_messages': len(messages), 'messages': [], 'results': []}
        counter = 0
        width = config.get('workers', 2)
        for offset in range(0, len(messages), width):
            plans = []
            tick = perf_counter()
            for meta in messages[offset:offset + width]:
                message_id = json.dumps([session.mailbox, session.validity, meta['uid']], ensure_ascii=False, separators=(',', ':'))
                cached = store.db.execute('SELECT parts FROM imap_scans WHERE account=? AND message=? AND selection=?', (account, message_id, selection)).fetchone()
                known = json.loads(cached['parts']) if cached else None
                # One verification pass; no second check or saved-row UPDATE on a cache hit.
                checked = [store.import_one(account, part, check=True) for part in known] if known is not None else None
                ready = checked is not None and all(item['state'] == 'skipped' for item in checked)
                plans.append((meta, message_id, checked if ready else None))
            metrics['verify_seconds'] += perf_counter() - tick
            pending = [meta for meta, _, checked in plans if checked is None]
            tick = perf_counter()
            fetched = iter(downloads.fetch_batch(pending))
            metrics['download_seconds'] += perf_counter() - tick
            metrics['body_fetches'] += len(pending)
            tick = perf_counter()
            for meta, message_id, checked in plans:
                results = []
                try:
                    if checked is not None:
                        results = checked
                        counter += len(checked)
                        if config.get('name_template') or 'directory' in config:
                            for item in results:
                                item['warning'] = 'existing indexed path retained; rename is a separate operation'
                    else:
                        raw = next(fetched)
                        if isinstance(raw, Exception):
                            raise raw
                        metrics['body_bytes'] += len(raw)
                        known = []
                        for part_id, kind, original, fallback, value, error in artifacts_from_raw(raw, meta, config):
                            counter += 1
                            item = {'message_id': message_id, 'subject': meta['subject'], 'part_id': part_id, 'kind': kind,
                                    'original_name': original, 'fallback_name': fallback}
                            if 'directory' in config:
                                item['directory'] = config['directory']
                            if 'name_template' in config:
                                filename = original or fallback
                                item['save_name'] = config['name_template'].format(date=meta['received'].date().isoformat(), subject=meta['subject'], sender=meta['sender'], index=counter,
                                    uid=meta['uid'], part=part_id, original=filename, stem=Path(filename).stem, ext=Path(filename).suffix)
                            known.append(dict(item))
                            if error:
                                item['error'] = error
                            outcome = store.import_one(account, item, content=value if not error else None)
                            if error:
                                outcome['error'] = error
                            results.append(outcome)
                        if not any(item['state'] == 'failed' for item in results):
                            store.db.execute('INSERT OR REPLACE INTO imap_scans VALUES (?,?,?,?)', (account, message_id, selection, json.dumps(known, ensure_ascii=False)))
                            store.db.commit()
                except (ExportError, imaplib.IMAP4.error, OSError, ValueError) as exc:
                    if isinstance(exc, ExportError) and str(exc) == 'UIDVALIDITY_changed_restart_selection':
                        raise
                    results.append({'message_id': message_id, 'uid': meta['uid'], 'state': 'failed', 'error': str(exc) if isinstance(exc, ExportError) else 'mail_fetch_or_parse_failed'})
                report['results'].extend(results)
                failed = sum(item['state'] == 'failed' for item in results)
                report['messages'].append({'uid': meta['uid'], 'files': len(results), 'state': 'failed' if failed else ('no_selected_files' if not results else 'complete')})
            metrics['save_seconds'] += perf_counter() - tick
        report['counts'] = {state: sum(item['state'] == state for item in report['results']) for state in ('saved', 'skipped', 'failed')}
        report['complete'] = not report['counts']['failed']
        metrics['workers_used'] = len(downloads.sessions)
        metrics['total_seconds'] = perf_counter() - started
        report['performance'] = {key: round(value, 4) if isinstance(value, float) else value for key, value in metrics.items()}
        if downloads.warning:
            report['warnings'] = [downloads.warning]
        return report
    finally:
        downloads.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-config', action='store_true', help='Check local credential presence only; no network')
    parser.add_argument('--config', help='UTF-8 JSON task, never credentials')
    parser.add_argument('--root', help='Absolute local archive folder')
    args = parser.parse_args()
    session = None
    try:
        if args.check_config:
            account, _ = credentials()
            print(json.dumps({'configured': True, 'account_present': bool(account), 'auth_code_present': True, 'network_checked': False}))
            return 0
        if not args.config or not args.root:
            raise ExportError('config_and_root_required')
        config = validate(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
        if not Path(args.root).is_absolute():
            raise ExportError('root_must_be_absolute')
        account, secret = credentials()
        session = Session(account, secret, config['mailbox'])
        started = perf_counter()
        session.connect()
        connected = perf_counter()
        report = export(config, args.root, session)
        report['performance']['connect_seconds'] = round(connected - started, 4)
        report['performance']['total_seconds'] = round(perf_counter() - started, 4)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['complete'] else 1
    except (ExportError, ArchiveError, OSError, ValueError, imaplib.IMAP4.error) as exc:
        error = str(exc) if isinstance(exc, (ExportError, ArchiveError)) else 'imap_or_local_operation_failed:' + type(exc).__name__
        print(json.dumps({'complete': False, 'error': error}, ensure_ascii=False))
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())

