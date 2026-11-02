"""Account-scoped text transcripts; no transport secrets are accepted here."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ChatStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.corrupt = set()

    def path(self, account_id, user_id):
        key = hashlib.sha256(json.dumps([account_id, user_id]).encode()).hexdigest()
        return self.directory / (key + '.json')

    @staticmethod
    def clean(rows):
        if not isinstance(rows, list):
            raise ValueError('聊天记录格式无效')
        result = []
        for row in rows[-100:]:
            if (not isinstance(row, dict) or row.get('direction') not in ('in', 'out')
                    or any(not isinstance(row.get(k), str) for k in ('id', 'text', 'status', 'time'))
                    or len(row['text']) > 8000 or len(row['id']) > 256):
                raise ValueError('聊天记录格式无效')
            result.append({key: row[key] for key in ('id', 'text', 'direction', 'status', 'time')})
        return result

    def load(self, account_id, user_id):
        path = self.path(account_id, user_id)
        if not path.exists():
            return []
        if path.stat().st_size > 4000000:
            self.corrupt.add(path)
            raise ValueError('聊天记录过大')
        try:
            rows = self.clean(json.loads(path.read_text('utf8')))
        except (ValueError, UnicodeError):
            self.corrupt.add(path)
            raise
        for row in rows:
            if row['status'] == '发送中':
                row['status'] = '发送结果未知，请在微信核实'
        return rows

    def save(self, account_id, user_id, rows):
        if self.path(account_id, user_id) in self.corrupt:
            raise ValueError('聊天记录损坏，请清空后重试')
        atomic_write(self.path(account_id, user_id), json.dumps(self.clean(rows), ensure_ascii=False).encode('utf8'))

    def clear(self, account_id, user_id):
        path = self.path(account_id, user_id)
        path.unlink(missing_ok=True)
        self.corrupt.discard(path)
