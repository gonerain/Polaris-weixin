"""Private temporary files and iLink CDN attachment decoding."""
import base64
import ctypes
import ctypes.util
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener
import uuid

MAX_FILE = 20 * 1024 * 1024
CDN = 'https://novac2c.cdn.weixin.qq.com/c2c/download'


def decrypt(data, key):
    """Use the host's OpenSSL EVP API; never put the media key in argv."""
    if len(key) != 16 or not data or len(data) % 16:
        raise ValueError('附件加密数据无效')
    library = ctypes.util.find_library('crypto')
    if not library:
        raise ValueError('附件解密需要本机 OpenSSL libcrypto')
    lib = ctypes.CDLL(library)
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    lib.EVP_aes_128_ecb.restype = ctypes.c_void_p
    lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_int]
    lib.EVP_DecryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise ValueError('无法初始化附件解密')
    output = ctypes.create_string_buffer(len(data) + 16)
    length, tail = ctypes.c_int(), ctypes.c_int()
    try:
        if (lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_128_ecb(), None, key, None) != 1
                or lib.EVP_DecryptUpdate(ctx, output, ctypes.byref(length), data, len(data)) != 1
                or lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(output, length.value), ctypes.byref(tail)) != 1):
            raise ValueError('附件解密失败，请重新发送原文件')
        return output.raw[:length.value + tail.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def media_url(media):
    url = media.get('full_url') or CDN + '?' + urlencode({'encrypted_query_param': media.get('encrypt_query_param', '')})
    p = urlsplit(url)
    if (p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
            or not (p.hostname or '').endswith('.cdn.weixin.qq.com') or p.fragment):
        raise ValueError('附件 CDN 地址不受信任')
    if not media.get('full_url') and not media.get('encrypt_query_param'):
        raise ValueError('附件缺少下载地址')
    return url


def download(media, cancel):
    from .client import NoRedirect
    request = Request(media_url(media), headers={'User-Agent': 'Polaris-Weixin/0.1'})
    finished, responses = threading.Event(), []
    deadline = time.monotonic()+30
    def watch():
        while not finished.wait(.1):
            if cancel.is_set() or time.monotonic()>=deadline:
                if responses: responses[0].close()
                return
    threading.Thread(target=watch, daemon=True, name='weixin-media-cancel').start()
    try:
        if cancel.is_set(): raise InterruptedError()
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            responses.append(response)
            data = response.read(MAX_FILE + 17)
        if cancel.is_set(): raise InterruptedError()
        if time.monotonic()>=deadline: raise TimeoutError('附件下载超时')
        if len(data) > MAX_FILE + 16: raise ValueError('附件超过 20 MiB')
        return data
    finally:
        finished.set()


class AttachmentStore:
    def __init__(self, root=None, fetch=download):
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / ('polaris-weixin-' + str(getattr(os, 'getuid', lambda: 'user')()))
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or self.root.is_symlink()
                or hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise ValueError('附件目录必须属于当前用户且不能是符号链接')
        self.root.chmod(0o700)
        self.fetch, self.records, self.lock = fetch, {}, threading.RLock()

    def register(self, identity, items, account):
        result = []
        with self.lock:
            for index, item in enumerate(items[:100]):
                if not isinstance(item, dict) or item.get('type') not in (2, 4): continue
                payload = item.get('image_item' if item['type'] == 2 else 'file_item')
                if not isinstance(payload, dict) or not isinstance(payload.get('media'), dict): continue
                key = hashlib.sha256(repr((account, identity, index)).encode()).hexdigest()
                self.records.setdefault(key, dict(item=payload, kind='image' if item['type'] == 2 else 'file', account=account))
                result.append(key)
        return result

    def save(self, attachment_id, account, cancel):
        with self.lock:
            record = self.records.get(attachment_id)
            if not record or record.get('account') != account: raise ValueError('附件不存在或账号已更换')
            if record.get('file') and Path(record['file']['path']).is_file(): return dict(record['file'])
            item, media = record['item'], record['item']['media']
        raw = self.fetch(media, cancel)
        key = item.get('aeskey') if record['kind'] == 'image' else None
        if key:
            key = bytes.fromhex(key)
        elif media.get('aes_key'):
            key = base64.b64decode(media['aes_key'], validate=True)
            if len(key) == 32: key = bytes.fromhex(key.decode('ascii'))
        if key:
            raw = decrypt(raw, key)
        elif record['kind'] != 'image':
            raise ValueError('文件附件缺少解密密钥')
        if item.get('len') is not None and str(len(raw)) != str(item['len']): raise ValueError('附件大小校验失败')
        if item.get('md5') and hashlib.md5(raw).hexdigest().lower() != item['md5'].lower(): raise ValueError('附件内容校验失败')
        if cancel.is_set(): raise InterruptedError()
        name = item.get('file_name') or ('image' + self.image_extension(raw) if record['kind'] == 'image' else 'attachment.bin')
        saved = self.write(name, base64.b64encode(raw).decode(), 'base64')
        with self.lock: record['file'] = saved
        return dict(saved)

    @staticmethod
    def image_extension(raw):
        if raw.startswith(b'\x89PNG\r\n'): return '.png'
        if raw.startswith(b'\xff\xd8'): return '.jpg'
        if raw.startswith(b'GIF8'): return '.gif'
        if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP': return '.webp'
        return '.img'

    def write(self, name, content, encoding='utf-8'):
        if (not isinstance(name, str) or not name or name in ('.', '..') or len(name) > 200
                or any(c in name for c in ('/', '\\', '\0', '\n', '\r'))): raise ValueError('文件名无效')
        if not isinstance(content, str) or len(content) > MAX_FILE * 2: raise ValueError('文件内容过大')
        if encoding not in ('utf-8', 'base64'): raise ValueError('仅支持 utf-8 或 base64')
        raw = content.encode('utf8') if encoding == 'utf-8' else base64.b64decode(content, validate=True)
        if len(raw) > MAX_FILE: raise ValueError('文件超过 20 MiB')
        directory = Path(tempfile.mkdtemp(prefix='file-', dir=self.root))
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as output: output.write(raw)
        result = dict(file_id=uuid.uuid4().hex, path=str(path.resolve()), name=name, size=len(raw), mime=mimetypes.guess_type(name)[0] or 'application/octet-stream')
        with self.lock: self.records[result['file_id']] = dict(file=result)
        return result

    def catalog(self, account):
        with self.lock:
            return [dict(attachment_id=key,kind=r['kind'],state='ready' if r.get('file') else 'pending',
                         **(r.get('file') or {})) for key,r in list(self.records.items())[-100:]
                    if r.get('account')==account and 'item' in r]

    def read(self, file_id, encoding='utf-8', offset=0, limit=65536):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1024 * 1024:
            raise ValueError('读取范围无效')
        if encoding not in ('utf-8','base64'): raise ValueError('仅支持 utf-8 或 base64')
        with self.lock: record = self.records.get(file_id, {}).get('file')
        if not record: raise ValueError('文件不存在或已过期')
        path = Path(record['path'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()): raise ValueError('文件路径已改变')
        with path.open('rb') as source:
            source.seek(offset)
            data = source.read(limit)
        return dict(file_id=file_id, content=data.decode('utf8') if encoding == 'utf-8' else base64.b64encode(data).decode(), encoding=encoding, offset=offset, next_offset=offset+len(data), eof=offset+len(data)>=path.stat().st_size)
