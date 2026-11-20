"""Direct, offline LAN transport with password-authenticated certificate pinning."""
import asyncio
import hashlib
import hmac
import ipaddress
import queue
import secrets
import socket
import ssl
import struct
import tempfile
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from internet import certificate, SERVER_NAME
from rendezvous import validate

LAN_PORT = 49322
LIMIT = 4096
HEADER = struct.Struct("!H")
TIMEOUT = 6


def parse_address(value):
    host, sep, port = value.strip().partition(":")
    addr = ipaddress.IPv4Address(host)
    if addr.is_unspecified or addr.is_multicast:
        raise ValueError("请输入被控端的局域网 IPv4 地址")
    port = int(port) if sep else LAN_PORT
    if not 1 <= port <= 65535:
        raise ValueError("端口无效")
    return str(addr), port


def ipv4_addresses():
    from aioice.ice import get_host_addresses
    values = []
    for host in get_host_addresses(True, False):
        ip = ipaddress.IPv4Address(host)
        if not ip.is_loopback:
            values.append(host)
    return values


def key_for(device, password):
    device, password = validate(device,password)
    return device.encode(), hashlib.sha256(("lan-v1:"+device+"."+password).encode()).digest()


def mac(key, *fields):
    return hmac.new(key, b"".join(fields), hashlib.sha256).digest()


async def read(reader):
    async with asyncio.timeout(TIMEOUT):
        size, = HEADER.unpack(await reader.readexactly(HEADER.size))
        if not 0 < size <= LIMIT:
            raise ValueError("局域网握手报文无效")
        return await reader.readexactly(size)


async def send(writer, payload):
    writer.write(HEADER.pack(len(payload))+payload)
    async with asyncio.timeout(TIMEOUT):
        await writer.drain()


async def close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(),1)
    except (OSError, TimeoutError):
        pass


async def bridge(left, right):
    async def pump(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await asyncio.wait_for(writer.drain(),10)
    tasks = [asyncio.create_task(pump(left[0],right[1])),
             asyncio.create_task(pump(right[0],left[1]))]
    try:
        await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await asyncio.gather(close_writer(left[1]),close_writer(right[1]))


class Background:
    def start(self):
        self.events = queue.SimpleQueue()
        self.stop = threading.Event()
        self.loop = None
        self.task = None
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def emit(self, kind, value):
        self.events.put((kind,value))

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass

    def run(self):
        try:
            asyncio.run(self.dispatch())
        except asyncio.CancelledError:
            pass
        except (OSError,ValueError,asyncio.IncompleteReadError) as error:
            self.emit("error", str(error) or "局域网连接已断开")
        finally:
            self.emit("closed",None)

    async def dispatch(self):
        self.loop=asyncio.get_running_loop()
        self.task=asyncio.current_task()
        if not self.stop.is_set():
            await self.main()


class LanHost(Background):
    def __init__(self, host, device, password, port=LAN_PORT, identity=None):
        self.host = host
        self.device, self.key = key_for(device,password)
        self.port = port
        self.identity = identity or device
        self.start()

    async def main(self):
        cert,key=certificate()
        pem=cert.public_bytes(serialization.Encoding.PEM)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_3
        with tempfile.TemporaryDirectory(prefix="campus-lan-") as temp:
            certfile,keyfile=Path(temp)/"cert.pem",Path(temp)/"key.pem"
            certfile.write_bytes(pem)
            keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
            context.load_cert_chain(certfile,keyfile)
        tasks=set()
        active=False

        async def serve(reader, writer):
            nonlocal active
            authenticated=False
            target=None
            try:
                req=await read(reader)
                if len(req)!=44 or req[:12]!=self.device:
                    raise ValueError("设备号不匹配")
                client_nonce=req[12:]
                await send(writer,mac(self.key,b"certificate",client_nonce,pem)+pem)
                async with asyncio.timeout(TIMEOUT):
                    await writer.start_tls(context,ssl_handshake_timeout=TIMEOUT)
                challenge=secrets.token_bytes(32)
                await send(writer,challenge)
                proof=await read(reader)
                suffix=proof[32:]
                if (len(proof)<44 or not hmac.compare_digest(
                        proof[:32],mac(self.key,b"client",client_nonce,challenge,suffix))):
                    raise ValueError("密码不匹配")
                if active:
                    await send(writer,b"BUSY")
                    return
                client_device=suffix[:12].decode()
                client_name=suffix[12:].decode()
                active=authenticated=True
                target=await asyncio.open_connection("127.0.0.1",self.host.port)
                await send(writer,b"OK"+self.host.secret.encode()+b"\0"+self.identity.encode())
                self.emit("remember",{"device":client_device,"name":client_name})
                self.emit("connected",None)
                await bridge((reader,writer),target)
            except (OSError,ValueError,asyncio.IncompleteReadError):
                pass
            finally:
                if target:
                    await close_writer(target[1])
                await close_writer(writer)
                if authenticated:
                    active=False
                    self.emit("status","局域网在线，等待连接")

        def accept(reader,writer):
            if len(tasks)>=8:
                writer.close()
                return
            t=asyncio.create_task(serve(reader,writer))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        listener=await asyncio.start_server(accept,"0.0.0.0",self.port)
        self.emit("listening",listener.sockets[0].getsockname()[1])
        try:
            async with listener:
                await listener.serve_forever()
        finally:
            for t in list(tasks):
                t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


class LanClient(Background):
    def __init__(self, address, device, password, identity):
        self.target=parse_address(address)
        self.device,self.key=key_for(device,password)
        self.identity=identity
        self.token=None
        self.start()

    async def main(self):
        self.emit("status","正在连接局域网地址…")
        async with asyncio.timeout(TIMEOUT):
            reader,writer=await asyncio.open_connection(*self.target)
        proxy=None
        link=None
        try:
            nonce=secrets.token_bytes(32)
            await send(writer,self.device+nonce)
            response=await read(reader)
            pem=response[32:]
            if not hmac.compare_digest(response[:32],mac(self.key,b"certificate",nonce,pem)):
                raise ValueError("设备号或密码不匹配")
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version=ssl.TLSVersion.TLSv1_3
            context.load_verify_locations(cadata=pem.decode())
            async with asyncio.timeout(TIMEOUT):
                await writer.start_tls(context,server_hostname=SERVER_NAME,ssl_handshake_timeout=TIMEOUT)
            challenge=await read(reader)
            if len(challenge)!=32:
                raise ValueError("局域网认证质询无效")
            identity=(self.identity["device"]+self.identity["name"]).encode()
            await send(writer,mac(self.key,b"client",nonce,challenge,identity)+identity)
            answer=await read(reader)
            if answer==b"BUSY":
                raise ValueError("被控端已有局域网会话")
            if not answer.startswith(b"OK"):
                raise ValueError("局域网认证失败")
            token, sep, name = answer[2:].partition(b"\0")
            if not sep or not token:
                raise ValueError("局域网认证应答无效")
            self.token=token.decode()
            accepted=asyncio.get_running_loop().create_future()
            def accept(local_reader, local_writer):
                if accepted.done():
                    local_writer.close()
                    return
                accepted.set_result((local_reader,local_writer))
            proxy=await asyncio.start_server(accept,"127.0.0.1",0)
            self.emit("route","局域网直连")
            self.emit("port",proxy.sockets[0].getsockname()[1])
            link=await asyncio.wait_for(accepted,12)
            self.emit("remember",{"device":self.device.decode(),"name":name.decode()})
            self.emit("connected",None)
            await bridge(link,(reader,writer))
        finally:
            if proxy:
                proxy.close()
                await proxy.wait_closed()
            await close_writer(writer)

