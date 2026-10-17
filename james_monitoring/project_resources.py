"""Project-owned reference material. URLs are references, never fetched or credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .fileio import atomic_write, path_lock

KINDS = ('brief', 'document', 'dataset', 'design', 'artifact', 'tool', 'link')
TEXT = {'.txt', '.md', '.csv', '.tsv', '.json', '.yaml', '.yml'}
FILES = TEXT | {'.pdf', '.docx', '.xlsx', '.pptx', '.png', '.jpg', '.jpeg', '.webp'}
MAX_FILE = 2_000_000


class ProjectResources:
    def __init__(self, root: Path, project: str):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', project):
            raise ValueError('Invalid project id.')
        self.directory = root / 'projects' / project / 'resources'
        self._safe(self.directory, root)

    @staticmethod
    def _safe(path: Path, root: Path) -> Path:
        if path.is_symlink() or path.resolve() != root.resolve().joinpath(path.relative_to(root)):
            raise ValueError('Project resource paths must not contain symlinks.')
        return path

    def path(self, rid: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', rid):
            raise ValueError('Invalid resource id.')
        return self._safe(self.directory / f'{rid}.json', self.directory)

    def all(self) -> list[dict]:
        return [self.get(p.stem) for p in sorted(self.directory.glob('*.json'))]

    def get(self, rid: str) -> dict:
        p = self.path(rid)
        if p.stat().st_size > 3_000_000:
            raise ValueError('Resource record exceeds the supported size.')
        d = json.loads(p.read_text())
        if not isinstance(d, dict) or d.get('id') != rid:
            raise ValueError('Invalid resource record.')
        return d

    @staticmethod
    def view(d: dict) -> dict:
        return {k: v for k, v in d.items() if k != 'data'}

    def save(self, b: dict, by: str) -> dict:
        with path_lock(self.directory):
            rid = str(b.get('resource_id') or uuid.uuid4().hex)
            old = self.get(rid) if b.get('resource_id') else {}
            if old and b.get('revision') != old['revision']:
                raise ValueError('Resource changed. Reload before saving.')
            if not old and len(self.all()) >= 40:
                raise ValueError('A project supports up to 40 resources; reuse an existing entry.')
            title = str(b.get('title', old.get('title', ''))).strip()
            kind = str(b.get('kind', old.get('kind', 'document')))
            notes = str(b.get('notes', old.get('notes', ''))).strip()
            url = str(b.get('url', old.get('url', ''))).strip()
            if not title or len(title) > 160 or kind not in KINDS or len(notes) > 20000:
                raise ValueError('Use a title up to 160 characters, a supported type and notes up to 20,000 characters.')
            if url:
                u = urlsplit(url)
                if len(url) > 2000 or u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password \
                        or re.search(r'[\x00-\x20\x7f]', url):
                    raise ValueError('Use a plain HTTP(S) link without embedded credentials.')
                if any(re.search(r'token|secret|password|signature|api.?key|authorization|^code$', k, re.I)
                       for k, _ in parse_qsl(u.query) + parse_qsl(u.fragment)):
                    raise ValueError('Do not attach credential-bearing or signed links. Use Settings → Secrets.')
            filename, data = old.get('filename', ''), old.get('data', '')
            if b.get('remove_file') is True:
                filename, data = '', ''
            if 'data' in b:
                filename, data = str(b.get('filename', '')), b['data']
                if not isinstance(data, str) or len(data) > 2_700_000:
                    raise ValueError('Files must be at most 2 MB.')
                if not re.fullmatch(r'[\w .()-]{1,160}', filename) or filename.startswith('.') \
                        or Path(filename).suffix.lower() not in FILES:
                    raise ValueError('Use a supported text, document, spreadsheet, slide or image filename.')
                try:
                    content = base64.b64decode(data, validate=True)
                except (ValueError, TypeError):
                    raise ValueError('Invalid file encoding.') from None
                if not content or len(content) > MAX_FILE:
                    raise ValueError('Files must be nonempty and at most 2 MB.')
            record = dict(id=rid, title=title, kind=kind, notes=notes, url=url, filename=filename, data=data,
                          archived=bool(b.get('archived', old.get('archived', False))),
                          include=bool(b.get('include', old.get('include', True))),
                          revision=old.get('revision', 0) + 1, updated_by=by,
                          updated_at=datetime.now(timezone.utc).isoformat())
            atomic_write(self.path(rid), json.dumps(record, ensure_ascii=False, indent=2), mode=0o600)
            return self.view(record)

    def context(self, limit: int = 20000) -> str:
        chunks = []
        for r in self.all():
            if r['archived'] or not r['include']:
                continue
            text = ''
            if r['filename'] and Path(r['filename']).suffix.lower() in TEXT:
                raw = base64.b64decode(r['data'])
                text = raw[:20000].decode('utf-8', errors='replace')
                if len(raw) > 20000:
                    text += '\n[File excerpt truncated]'
            elif r['filename']:
                text = '[Attachment stored; its binary contents have not been extracted or read.]'
            chunks.append(json.dumps({'resource': r['title'], 'kind': r['kind'], 'notes': r['notes'],
                                      'url_reference_only': r['url'], 'filename': r['filename'], 'excerpt': text},
                                     ensure_ascii=False))
        result = '\n'.join(chunks)
        return result[:limit] + ('\n[Resource context truncated; narrow the included resources.]' if len(result) > limit else '')

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.all(), sort_keys=True).encode()).hexdigest()
