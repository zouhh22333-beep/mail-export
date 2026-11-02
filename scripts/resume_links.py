"""Discover email links and bind host-browser downloads to their source emails.

This module never fetches URLs or imports browser/connector credentials.
"""
import copy
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from archive_store import ArchiveError, Store, digest_file
from imap_export import PARSER


def web_url(value):
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            return None
        _ = parts.port
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ''))
    except ValueError:
        return None


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.flush()
            self.current = dict(attrs).get('href')

    def handle_data(self, text):
        if self.current:
            self.text.append(text)

    def handle_endtag(self, tag):
        if tag == 'a':
            self.flush()

    def flush(self):
        if self.current:
            self.links.append((self.current, ' '.join(self.text).strip()[:300]))
        self.current, self.text = None, []


def discover(context, metadata):
    result = []
    for message_id, path in context.items():
        message = PARSER.parsebytes(Path(path).read_bytes())
        found = {}
        for part in message.walk():
            if part.get_content_disposition() == 'attachment' or part.get_content_type() not in ('text/plain', 'text/html'):
                continue
            try:
                text = part.get_content()
            except (LookupError, UnicodeError):
                raise ArchiveError('cannot_decode_mail_links') from None
            if not isinstance(text, str):
                continue
            if part.get_content_type() == 'text/html':
                parser = Anchors()
                parser.feed(text)
                parser.flush()
                links = parser.links
            else:
                links = [(m.group(0).rstrip('.,;)>'), '') for m in re.finditer(r'https?://[^\s<>"\u3000]+', text)]
            for raw, label in links:
                url = web_url(raw)
                if url:
                    found.setdefault(url, label)
                    if label and not found[url]:
                        found[url] = label
        for url, label in found.items():
            result.append(dict(id='L' + str(len(result) + 1), url=url, text=label,
                               message_id=message_id, **metadata[message_id]))
    return result


def attach(snapshot, manifest, snapshot_id):
    """Host resolves every link; unresolved/blocked links must prevent completion."""
    if not isinstance(manifest, dict) or set(manifest) != {'snapshot_id', 'items'} or manifest['snapshot_id'] != snapshot_id:
        raise ArchiveError('link_manifest_snapshot_mismatch')
    if not isinstance(manifest['items'], list):
        raise ArchiveError('link_items_must_be_list')
    result = copy.deepcopy(snapshot)
    links = {row['id']: row for row in result.get('links', [])}
    seen, plans = set(), []
    for item in manifest['items']:
        if not isinstance(item, dict) or set(item) - {'id', 'state', 'files', 'reason'}:
            raise ArchiveError('invalid_link_resolution')
        key, state = item.get('id'), item.get('state')
        if key not in links or key in seen or state not in ('downloaded', 'not_resume', 'blocked'):
            raise ArchiveError('unknown_duplicate_or_invalid_link_resolution')
        seen.add(key)
        if not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ArchiveError('link_resolution_evidence_required')
        files = item.get('files', [])
        if not isinstance(files, list) or (state == 'downloaded') != bool(files):
            raise ArchiveError('downloaded_link_requires_files_only')
        link = links[key]
        prefix = 'link:' + hashlib.sha256(link['url'].encode()).hexdigest() + ':'
        result['candidates'] = [old for old in result['candidates']
                                if old['message_id'] != link['message_id'] or not old['part_id'].startswith(prefix)]
        for index, file in enumerate(files, 1):
            if not isinstance(file, dict) or set(file) != {'source_path', 'original_name', 'kind'}:
                raise ArchiveError('invalid_link_file')
            if file['kind'] not in ('original_download', 'page_pdf') or not isinstance(file['original_name'], str) or not file['original_name'].strip():
                raise ArchiveError('invalid_link_file_kind_or_name')
            path = Path(file['source_path'])
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise ArchiveError('link_download_must_be_local_regular_file')
            with path.open('rb') as stream:
                head = stream.read(512).lstrip().lower()
            if head.startswith((b'<!doctype html', b'<html')):
                raise ArchiveError('HTML_page_is_not_a_downloaded_resume_file')
            if file['kind'] == 'page_pdf' and (not head.startswith(b'%pdf-') or Path(file['original_name']).suffix.lower() != '.pdf'):
                raise ArchiveError('page_export_must_be_real_PDF')
            size, sha = digest_file(path)
            if not size:
                raise ArchiveError('empty_link_download')
            part_id = prefix + str(index)
            plans.append((link, file, part_id, size, sha))
        link['resolution'] = {'state': state, 'reason': item['reason']}
    # Stage actual files using the same atomic store and identity semantics.
    if plans:
        stage = Path(result['link_stage'])
        store = Store(str(stage))
        try:
            for link, file, part_id, size, sha in plans:
                saved = store.import_one(result['account'], dict(message_id=link['message_id'], part_id=part_id,
                    kind='attachment', subject=link['subject'], source_path=file['source_path'],
                    original_name=file['original_name'], directory=[]))
                if saved['state'] not in ('saved', 'skipped') or digest_file(Path(saved['path'])) != (size, sha):
                    raise ArchiveError('link_file_staging_failed_or_changed')
                key = hashlib.sha256(json.dumps([result['account'], link['message_id'], part_id],
                    ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                row = dict(id=key, message_id=link['message_id'], part_id=part_id,
                    subject=link['subject'], sender=link['sender'], date=link['date'], uid=link['uid'],
                    original_name=file['original_name'], source_path=saved['path'], size=size, sha256=sha,
                    origin=file['kind'])
                result['candidates'] = [old for old in result['candidates'] if old['id'] != key] + [row]
        finally:
            store.close()
    return result
