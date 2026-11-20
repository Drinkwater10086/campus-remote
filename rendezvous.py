import base64
import hashlib
import hmac
import json
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from internet import InternetSession

TTL = 115
MAX_DATA = 30000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def validate(device, password):
    device = device.strip().replace(" ", "").replace("-", "").upper()
    if not re.fullmatch("[0-9A-F]{12}", device):
        raise ValueError("设备号应为 12 位十六进制字符")
    if not re.fullmatch("[A-Za-z0-9_-]{24}", password):
        raise ValueError("请输入被控端生成的 24 位固定密码")
    return device, password


def signal_url(url, allow_loopback=False):
    url = url.strip().rstrip("/")
    parsed = urlsplit(url)
    valid_scheme = parsed.scheme == "https" or (
        allow_loopback and parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
    if not valid_scheme or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("请输入 HTTPS 信令服务地址")
    return url


class Board:
    def __init__(self, origin, device, password, allow_loopback=False):
        self.origin = signal_url(origin, allow_loopback)
        device, password = validate(device, password)
        root = hashlib.sha256((device + "." + password).encode()).digest()
        self.room = hmac.new(root, b"campus-room-v1", hashlib.sha256).hexdigest()
        self.aead = AESGCM(hmac.new(root, b"campus-payload-v1", hashlib.sha256).digest())

    def exchange(self, slot, payload=None):
        method = "GET"
        body = None
        aad = ("campus-v1:" + self.room + ":" + slot).encode()
        if payload is not None:
            method = "PUT"
            nonce = secrets.token_bytes(12)
            document = dict(payload, at=int(time.time()))
            body = base64.urlsafe_b64encode(nonce + self.aead.encrypt(
                nonce, json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode(), aad))
            if len(body) > MAX_DATA:
                raise ValueError("配对信息过长")
        url = f"{self.origin}/v1/{self.room}/{slot}?t={secrets.token_hex(4)}"
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"Content-Type": "application/octet-stream"})
        try:
            with OPENER.open(request, timeout=5) as response:
                if payload is not None:
                    return None
                data = response.read(MAX_DATA + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404 and payload is None:
                return None
            raise OSError(f"信令服务返回 HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise OSError("无法连接信令服务，请检查网址和网络") from error
        if len(data) > MAX_DATA:
            return None
        try:
            raw = base64.b64decode(data, altchars=b"-_", validate=True)
            value = json.loads(self.aead.decrypt(raw[:12], raw[12:], aad))
            if isinstance(value, dict) and type(value.get("at")) is int and -20 <= time.time()-value["at"] <= TTL:
                return value
        except (ValueError, InvalidTag, RecursionError):
            pass
        return None


class AutoPair:
    def __init__(self, board, options, identity, host=None):
        self.board = board
        self.options = options
        self.identity = identity
        self.host = host
        self.events = queue.SimpleQueue()
        self.stop = threading.Event()
        self.peer = None
        self.token = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.peer:
            self.peer.close()

    def emit(self, kind, value):
        self.events.put((kind, value))

    def run(self):
        try:
            if self.host:
                self.listen()
            else:
                nonce = secrets.token_hex(12)
                self.board.exchange("request", {"id":nonce, **self.identity})
                self.emit("status", "正在等待被控端。若长时间无响应，检查设备号、密码和被控端是否在线。")
                offer = self.wait_slot("offer-"+nonce, 35)
                if offer and not self.stop.is_set():
                    self.connect(nonce, offer)
                elif not self.stop.is_set():
                    self.emit("error", "被控端未响应：核对设备号、密码、信令网址和共享状态。")
        except (OSError, ValueError, KeyError, TimeoutError) as error:
            if not self.stop.is_set():
                self.emit("error",str(error))
        finally:
            if self.peer:
                self.peer.close()
            self.emit("closed", None)

    def wait_slot(self, slot, seconds):
        end = time.monotonic()+seconds
        while not self.stop.is_set() and time.monotonic()<end:
            value = self.board.exchange(slot)
            if value:
                return value
            self.stop.wait(.8)
        return None

    def listen(self):
        seen = set()
        opened = time.time() - 4
        self.emit("status", "正在连接信令服务…")
        waiting = False
        while not self.stop.is_set():
            try:
                request = self.board.exchange("request")
                if not waiting:
                    self.emit("status", "在线，等待设备号连接")
                    waiting = True
                if request and request["at"] >= opened:
                    nonce = request.get("id")
                    if isinstance(nonce,str) and re.fullmatch("[0-9a-f]{24}", nonce) and nonce not in seen:
                        if len(seen) >= 256:
                            seen.clear()
                        seen.add(nonce)
                        self.connect(nonce, request)
                        waiting = False
            except (OSError, TimeoutError) as error:
                self.emit("status", f"{error}。正在重试…")
                waiting = False
            self.stop.wait(1.5)

    def connect(self, nonce, remote):
        try:
            role = bool(self.host)
            if role:
                self.peer = InternetSession(self.options, host_port=self.host.port, token=self.host.secret)
            else:
                if not isinstance(remote.get("code"), str):
                    raise ValueError("信令应答无效")
                self.peer = InternetSession(self.options, offer=remote["code"])
            awaiting = False
            sent_answer = False
            next_poll = 0.0
            while not self.stop.is_set():
                try:
                    kind, value = self.peer.events.get(timeout=.15)
                except queue.Empty:
                    kind = None
                if kind in ("offer", "answer"):
                    self.board.exchange(kind+"-"+nonce, {"code":value, **self.identity})
                    if kind == "offer":
                        awaiting = True
                    else:
                        sent_answer = True
                elif kind == "connected":
                    self.emit("remember", remote)
                    self.emit(kind, value)
                elif kind == "port":
                    if not sent_answer:
                        raise ValueError("尚未交换应答")
                    self.token = self.peer.token
                    self.emit(kind, value)
                elif kind in ("status", "route", "error"):
                    self.emit(kind, value)
                elif kind == "closed":
                    return
                if awaiting and time.monotonic() >= next_poll:
                    reply = self.board.exchange("answer-"+nonce)
                    if reply and isinstance(reply.get("code"),str):
                        self.peer.submit(reply["code"])
                        awaiting = False
                    next_poll = time.monotonic()+.65
        finally:
            if self.peer:
                self.peer.close()
                self.peer = None

