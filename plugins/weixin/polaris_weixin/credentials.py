"""Local account storage: Windows DPAPI or owner-only POSIX JSON."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys

from .storage import atomic_write


def protect(data, *, decrypt=False):
    if sys.platform != "win32":
        raise OSError("DPAPI 仅适用于 Windows")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 无法加解密微信凭证")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialStore:
    MAX_SIZE = 262144
    FIELDS = ('token', 'botId', 'userId', 'baseUrl', 'cursor', 'seen', 'expired')

    def __init__(self, path):
        self.path = Path(path)
        self.corrupt = False

    @classmethod
    def clean(cls, account):
        if not isinstance(account, dict):
            raise ValueError("凭证格式不支持")
        result = {key: account[key] for key in cls.FIELDS if key in account}
        # Use the same account contract as the transport, without persisting any
        # transient QR or conversation context fields supplied by callers.
        from .client import Client
        Client.validate_account(result)
        seen = result.get('seen', [])
        if any(not isinstance(key, str) or not key or len(key) > 256 for key in seen):
            raise ValueError("去重记录无效")
        if 'seen' in result:
            result['seen'] = seen[-512:]
        return result

    def load(self):
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > self.MAX_SIZE:
                raise ValueError("凭证文件过大")
            data = self.path.read_bytes()
            if len(data) > self.MAX_SIZE:
                raise ValueError("凭证文件过大")
            if sys.platform == "win32":
                data = protect(data, decrypt=True)
            result = json.loads(data)
            if not isinstance(result, dict) or type(result.get("version")) is not int or result.pop("version") != 1:
                raise ValueError("凭证格式不支持")
            return self.clean(result)
        except (OSError, ValueError, TypeError, UnicodeError):
            self.corrupt = True
            raise

    def save(self, account):
        if self.corrupt:
            raise ValueError("凭证文件无法读取，请退出绑定后重试")
        # Validate an existing file even when this store has not loaded it yet.
        # Damaged or newer-format credentials must survive until explicit logout.
        if self.path.exists():
            self.load()
        data = json.dumps({"version": 1, **self.clean(account)}, ensure_ascii=False).encode('utf8')
        if len(data) > self.MAX_SIZE:
            raise ValueError("凭证文件过大")
        if sys.platform == "win32":
            data = protect(data)
        atomic_write(self.path, data)
        return True

    def clear(self):
        self.path.unlink(missing_ok=True)
        self.corrupt = False
