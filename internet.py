"""One-shot, authenticated ICE connection carrying a QUIC stream."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import queue
import secrets
import ssl
import struct
import threading
import time
from urllib.parse import parse_qs, urlsplit

from aioice import Candidate, Connection, TransportPolicy
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.buffer import Buffer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.packet import pull_quic_header
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ADDRESS = "127.0.0.1"
PEER = (ADDRESS, 9)  # Fixed address within this single-peer QUIC adapter.
SERVER_NAME = "campus-remote.invalid"
ALPN = "campus-remote-1"
CHUNK = 16384
WINDOW = 16
LENGTH = struct.Struct("!H")
LIFETIME = 300
CODE_LIMIT = 16000


def json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def pack(value):
    return base64.urlsafe_b64encode(json_bytes(value)).decode().rstrip("=")


def unpack(code, expected):
    code = code.strip()
    if not code or len(code) > CODE_LIMIT:
        raise ValueError("配对代码长度无效")
    try:
        value = json.loads(base64.b64decode(code + "=" * (-len(code) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or value.get("kind") != expected or value.get("v") != 1:
            raise ValueError("配对代码类型无效")
        expiry = value["expires"]
        if type(expiry) is not int or not time.time() - 10 <= expiry <= time.time() + LIFETIME + 30:
            raise ValueError("配对代码已过期或两台电脑时间不一致")
        if not isinstance(value.get("id"), str) or len(value["id"]) != 32:
            raise ValueError("配对代码标识无效")
        if not isinstance(value.get("ice"), dict):
            raise ValueError("配对地址无效")
        return value
    except (KeyError, TypeError, RecursionError) as error:
        raise ValueError("配对代码格式无效") from error


def turn_options(url, username, password, relay_only=False, stun=("stun.cloudflare.com", 3478)):
    options = {"stun_server": stun, "transport_policy": TransportPolicy.RELAY if relay_only else TransportPolicy.ALL}
    if not url.strip():
        if relay_only:
            raise ValueError("强制中继需要填写 TURN 地址和临时凭证")
        return options
    raw = url.strip()
    scheme, sep, rest = raw.partition(":")
    if not sep or scheme not in ("turn", "turns"):
        raise ValueError("TURN 地址需要以 turn: 或 turns: 开头")
    parsed = urlsplit(f"{scheme}://{rest.removeprefix('//')}")
    protocol = parse_qs(parsed.query).get("transport", ["tcp" if scheme == "turns" else "udp"])[0]
    if (not parsed.hostname or parsed.username is not None or parsed.path or parsed.fragment
            or protocol not in ("udp", "tcp") or (scheme == "turns" and protocol != "tcp")):
        raise ValueError("TURN 地址或传输参数无效")
    if not username or not password:
        raise ValueError("请填写 TURN 临时用户名和密码")
    options.update(turn_server=(parsed.hostname, parsed.port or (5349 if scheme == "turns" else 3478)),
                   turn_username=username, turn_password=password, turn_ssl=scheme == "turns",
                   turn_transport=protocol)
    return options


def certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_NAME)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(SERVER_NAME)]), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


def ice_description(ice):
    return {"user": ice.local_username, "password": ice.local_password,
            "candidates": [c.to_sdp() for c in ice.local_candidates][:32]}


async def set_remote(ice, data):
    name, password = data.get("user"), data.get("password")
    candidates = data.get("candidates")
    if (not isinstance(name, str) or not 4 <= len(name) <= 256
            or not isinstance(password, str) or not 22 <= len(password) <= 256
            or not isinstance(candidates, list) or not 1 <= len(candidates) <= 32):
        raise ValueError("对方的 ICE 信息无效")
    ice.remote_username, ice.remote_password = name, password
    for item in candidates:
        if not isinstance(item, str) or len(item) > 512:
            raise ValueError("对方的 ICE 地址无效")
        candidate = Candidate.from_sdp(item)
        address = ipaddress.ip_address(candidate.host)
        if (candidate.component != 1 or candidate.transport.lower() != "udp"
                or candidate.type not in ("host", "srflx", "relay")
                or not 1 <= candidate.port <= 65535 or address.is_multicast
                or address.is_unspecified or address.is_loopback):
            continue
        await ice.add_remote_candidate(candidate)
    await ice.add_remote_candidate(None)


def proof(key, answer):
    return hmac.new(key, json_bytes(answer), hashlib.sha256).hexdigest()


class IceDatagrams:
    def __init__(self):
        self.outgoing = asyncio.Queue(maxsize=192)

    def sendto(self, data, _address):
        try:
            self.outgoing.put_nowait(data)
        except asyncio.QueueFull:
            pass  # QUIC recovers datagrams dropped under congestion.


async def bridge(tcp_reader, tcp_writer, remote_reader, remote_writer):
    credits = asyncio.BoundedSemaphore(WINDOW)
    inflight = 0

    async def outbound():
        nonlocal inflight
        while True:
            data = await tcp_reader.read(CHUNK)
            if not data:
                return
            await asyncio.wait_for(credits.acquire(), 12)
            inflight += 1
            remote_writer.write(LENGTH.pack(len(data)) + data)

    async def inbound():
        nonlocal inflight
        while True:
            length, = LENGTH.unpack(await remote_reader.readexactly(LENGTH.size))
            if not length:
                if not inflight:
                    raise ValueError("公网数据窗口无效")
                inflight -= 1
                credits.release()
                continue
            if length > CHUNK:
                raise ValueError("公网数据长度无效")
            tcp_writer.write(await remote_reader.readexactly(length))
            await asyncio.wait_for(tcp_writer.drain(), 8)
            remote_writer.write(b"\0\0")

    tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        remote_writer.close()
        tcp_writer.close()


async def tunnel(ice, config, client_key, host_port, publish):
    connection = config.is_client
    adapter = IceDatagrams()
    local_ready = asyncio.get_running_loop().create_future()
    active = False
    tasks = set()

    async def handle(remote_reader, remote_writer, tcp=None):
        nonlocal active
        if active:
            remote_writer.close()
            if tcp:
                tcp[1].close()
            return
        active = True
        local_ready.set_result(None)
        try:
            if tcp is None:
                supplied = await asyncio.wait_for(remote_reader.readexactly(len(client_key)), 6)
                if not hmac.compare_digest(supplied, client_key):
                    raise ValueError("公网控制端认证失败")
                tcp = await asyncio.open_connection(ADDRESS, host_port)
                remote_writer.write(b"\1")
            else:
                remote_writer.write(client_key)
                if await asyncio.wait_for(remote_reader.readexactly(1), 6) != b"\1":
                    raise ValueError("公网认证失败")
            publish("connected", None)
            await bridge(*tcp, remote_reader, remote_writer)
        finally:
            if tcp:
                tcp[1].close()
            remote_writer.close()

    def attach(reader, writer, tcp=None):
        task = asyncio.create_task(handle(reader, writer, tcp))
        tasks.add(task)

    first = None
    if connection:
        quic = QuicConnection(configuration=config)
    else:
        first = await asyncio.wait_for(ice.recv(), 15)
        header = pull_quic_header(Buffer(data=first), host_cid_length=config.connection_id_length)
        quic = QuicConnection(configuration=config, original_destination_connection_id=header.destination_cid)
    wire = QuicConnectionProtocol(quic, stream_handler=attach)
    wire.connection_made(adapter)

    async def send_ice():
        while True:
            await ice.send(await adapter.outgoing.get())

    async def receive_ice():
        while True:
            wire.datagram_received(await ice.recv(), PEER)

    pumps = [asyncio.create_task(send_ice()), asyncio.create_task(receive_ice())]
    listener = None
    try:
        if connection:
            wire.connect(PEER)
        else:
            wire.datagram_received(first, PEER)
        if connection:
            await asyncio.wait_for(wire.wait_connected(), 15)

            async def accept_local(reader, writer):
                try:
                    streams = await wire.create_stream()
                    attach(*streams, (reader, writer))
                except Exception:
                    writer.close()

            listener = await asyncio.start_server(accept_local, ADDRESS, 0)
            publish("port", listener.sockets[0].getsockname()[1])
        await asyncio.wait_for(local_ready, 18)
        await asyncio.wait([*pumps, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in [*pumps, *tasks]:
            if task.done():
                task.result()
    finally:
        if listener:
            listener.close()
            await listener.wait_closed()
        wire.close()
        for task in [*pumps, *tasks]:
            task.cancel()
        await asyncio.gather(*pumps, *tasks, return_exceptions=True)


class InternetSession:
    def __init__(self, options, host_port=None, token=None, offer=None):
        self.options = options
        self.host_port = host_port
        self.token = token
        self.offer = offer
        self.events = queue.SimpleQueue()
        self.answer = None
        self.stopping = threading.Event()
        self.loop = None
        self.task = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def publish(self, kind, value):
        self.events.put((kind, value))

    def submit(self, code):
        self.answer = code

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass

    def run(self):
        try:
            asyncio.run(self.main())
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.publish("error", str(error) or type(error).__name__)
        finally:
            self.publish("closed", None)

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        if self.stopping.is_set():
            return
        host = self.host_port is not None
        ice = Connection(ice_controlling=not host, **self.options)
        config = QuicConfiguration(is_client=not host, alpn_protocols=[ALPN], idle_timeout=15,
                                   congestion_control_algorithm="cubic")
        try:
            self.publish("status", "正在收集 P2P／TURN 地址…")
            await asyncio.wait_for(ice.gather_candidates(), 12)
            local = ice_description(ice)
            if not local["candidates"]:
                raise ValueError("没有可用地址，请检查网络或 TURN 凭证")
            if host:
                config.certificate, config.private_key = certificate()
                pem = config.certificate.public_bytes(serialization.Encoding.PEM).decode()
                key = secrets.token_bytes(32)
                invitation = {"v": 1, "kind": "offer", "expires": int(time.time()) + LIFETIME,
                              "id": secrets.token_hex(16), "key": key.hex(), "secret": self.token,
                              "certificate": pem, "ice": local}
                self.publish("offer", pack(invitation))
                while self.answer is None:
                    if time.time() > invitation["expires"]:
                        raise ValueError("邀请已过期，请重新生成")
                    await asyncio.sleep(.10)
                reply = unpack(self.answer, "answer")
                tag = reply.pop("proof", None)
                if (reply["id"] != invitation["id"] or not isinstance(tag, str)
                        or not hmac.compare_digest(tag, proof(key, reply))):
                    raise ValueError("应答不匹配，请核对邀请和应答")
                remote = reply["ice"]
            else:
                invitation = unpack(self.offer, "offer")
                key = bytes.fromhex(invitation["key"])
                if len(key) != 32 or not isinstance(invitation.get("secret"), str):
                    raise ValueError("邀请密钥无效")
                config.verify_mode = ssl.CERT_REQUIRED
                config.server_name = SERVER_NAME
                config.cadata = invitation["certificate"].encode()
                self.token = invitation["secret"]
                reply = {"v": 1, "kind": "answer", "id": invitation["id"],
                         "expires": invitation["expires"], "ice": local}
                reply["proof"] = proof(key, reply)
                self.publish("answer", pack(reply))
                remote = invitation["ice"]
            await set_remote(ice, remote)
            self.publish("status", "等待双方配对并检测路径…")
            try:
                await asyncio.wait_for(ice.connect(), min(20, max(1, invitation["expires"] - time.time())))
            except (ConnectionError, TimeoutError) as error:
                has_relay = any(c.type == "relay" for c in ice.local_candidates)
                if not self.options.get("turn_server"):
                    hint = "未设置 TURN。请在两端填入托管中继的地址和临时凭证，再连接。"
                elif not has_relay:
                    hint = "本端没有拿到 TURN 中继地址。请检查凭证是否过期，或尝试服务商的 TLS 443 地址。"
                else:
                    hint = "已获得 TURN 地址，但双方路径仍不可达。请检查另一端的 TURN 设置和网络。"
                raise ConnectionError("连接路径协商失败。" + hint) from error
            # aioice 0.13 has no public API for the nominated candidate pair.
            pair = ice._nominated.get(1)
            relayed = pair is not None and "relay" in (pair.local_candidate.type, pair.remote_candidate.type)
            self.publish("route", "TURN 中继" if relayed else "P2P 直连")
            await tunnel(ice, config, key, self.host_port, self.publish)
        finally:
            await ice.close()



