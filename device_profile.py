import ctypes as c
from contextlib import contextmanager
import msvcrt
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile


class Blob(c.Structure):
    _fields_ = [('size', w.DWORD), ('data', c.POINTER(c.c_ubyte))]


crypt = c.WinDLL('crypt32', use_last_error=True)
kernel = c.WinDLL('kernel32', use_last_error=True)
kernel.LocalFree.argtypes = (c.c_void_p,)
kernel.LocalFree.restype = c.c_void_p
for fn in (crypt.CryptProtectData, crypt.CryptUnprotectData):
    fn.argtypes = (c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p,
                   w.DWORD, c.POINTER(Blob))
    fn.restype = w.BOOL


def dpapi(data, protect):
    stored = (c.c_ubyte * len(data)).from_buffer_copy(data)
    source, dest = Blob(len(stored), stored), Blob()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not fn(c.byref(source), None, None, None, None, 1, c.byref(dest)):
        raise c.WinError(c.get_last_error())
    try:
        return c.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(dest.data)


class Profile:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ['LOCALAPPDATA']) / 'CampusRemote' / 'devices.dat'
        with self.locked():
            try:
                self.data = self.load()
            except FileNotFoundError:
                self.data = {'device': secrets.token_hex(6).upper(), 'password': secrets.token_urlsafe(18),
                             'name': socket.gethostname()[:64], 'server': '', 'turn': ['', '', ''],
                             'saved': {}, 'seen': {}}
                self.save()

    def load(self):
        return json.loads(dpapi(self.path.read_bytes(), False))

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as lock:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def save(self):
        payload = dpapi(json.dumps(self.data, ensure_ascii=False).encode(), True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(payload)
        try:
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def update(self, **values):
        with self.locked():
            self.data = self.load()
            self.data.update(values)
            self.save()

    def record(self, field, device, record):
        with self.locked():
            self.data = self.load()
            records = self.data[field]
            previous = records.pop(device, None)
            if field == "saved" and previous:
                record = {**previous, **record}
            records[device] = record
            if len(records) > 64:
                records.pop(next(iter(records)))
            self.save()

    def forget(self, device):
        with self.locked():
            self.data = self.load()
            self.data['saved'].pop(device, None)
            self.save()
