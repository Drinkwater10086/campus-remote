import hmac
import json
import socket
import struct
import threading
import time

from desktop import DesktopInput, root_window
from quality import DEFAULT, settings

ADDRESS = "127.0.0.1"
PORT = 49321
MAX_FRAME = 4_000_000
HEADER = struct.Struct("!I")
VIDEO_HEADER = struct.Struct("!IB")
STATS_REPLY = struct.Struct("!BQ")
STATS_KIND = 0x80


def read_exact(conn, count):
    data = bytearray(count)
    pending = memoryview(data)
    while pending:
        received = conn.recv_into(pending)
        if not received:
            raise EOFError
        pending = pending[received:]
    return data


def receive(conn, limit):
    size, = HEADER.unpack(read_exact(conn, HEADER.size))
    if not 0 < size <= limit:
        raise ValueError("报文长度无效")
    return read_exact(conn, size)


def send(conn, payload):
    conn.sendall(HEADER.pack(len(payload)) + payload)


def send_message(conn, message):
    send(conn, json.dumps(message, separators=(",", ":")).encode())


def receive_message(conn):
    try:
        message = json.loads(receive(conn, 2048))
    except RecursionError as error:
        raise ValueError("报文嵌套过深") from error
    if not isinstance(message, dict):
        raise ValueError("报文格式无效")
    return message


def capture():
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=False)
    return image if image.mode == 'RGB' else image.convert('RGB')


class Host:
    def __init__(self, secret, status, capture_frame=None, input_factory=DesktopInput, port=PORT):
        self.secret = secret
        self.status = status
        self.capture_frame = capture_frame
        self.input_factory = input_factory
        self.stop_event = threading.Event()
        self.input_enabled = False
        self.input_lock = threading.Lock()
        self.input_state = None
        self.lock = threading.Lock()
        self.listener = socket.socket()
        try:
            self.listener.bind((ADDRESS, port))
            self.listener.listen(1)
        except OSError:
            self.listener.close()
            raise
        self.listener.settimeout(0.25)
        self.port = self.listener.getsockname()[1]
        self.active = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def set_input_enabled(self, enabled):
        with self.input_lock:
            self.input_enabled = enabled and not self.stop_event.is_set()
            if not self.input_enabled and self.input_state:
                self.input_state.release()

    def stop(self):
        self.stop_event.set()
        self.set_input_enabled(False)
        self.listener.close()
        with self.lock:
            if self.active:
                try:
                    self.active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def run(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                import av

                with self.lock:
                    if self.stop_event.is_set():
                        return
                    self.active = conn
                try:
                    self.session(conn)
                except (EOFError, OSError, ValueError, av.FFmpegError):
                    pass
                finally:
                    with self.lock:
                        self.active = None
                    if not self.stop_event.is_set():
                        self.status("等待控制端连接")

    def session(self, conn):
        from duplication import DesktopDuplication
        from screen import ScreenCapture, primary_size
        from video import Encoder

        conn.settimeout(3)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        hello = receive_message(conn)
        supplied = hello.get("secret")
        allowed = (hello.get("op") == "hello" and hello.get("video") == "h264-v2" and isinstance(supplied, str)
                   and hmac.compare_digest(supplied.encode(), self.secret.encode()))
        send_message(conn, {"ok": allowed, "video": "h264-v2"})
        if not allowed:
            return
        self.status("已连接 · 本机直连")
        closed = threading.Event()
        encoder_settings = DEFAULT
        pending_probe = None
        probe_lock = threading.Lock()
        input_state = self.input_factory()
        viewer_hwnd = hello.get("viewer_hwnd")
        if isinstance(input_state, DesktopInput) and type(viewer_hwnd) is int and 0 < viewer_hwnd <= 0xffffffff:
            input_state.protected_window = root_window(viewer_hwnd)
        with self.input_lock:
            self.input_state = input_state

        def read_controls():
            nonlocal encoder_settings, pending_probe
            try:
                while not closed.is_set() and not self.stop_event.is_set():
                    message = receive_message(conn)
                    if message.get("op") == "settings":
                        try:
                            encoder_settings = settings(message)
                        except ValueError:
                            continue
                    elif message.get("op") == "ping":
                        nonce = message.get("probe")
                        if type(nonce) is int and 0 < nonce < 2**64:
                            with probe_lock:
                                pending_probe = nonce
                    else:
                        with self.input_lock:
                            if self.input_enabled and not self.stop_event.is_set():
                                try:
                                    input_state.apply(message)
                                except OSError:
                                    self.status("已连接 · Windows 拒绝了部分输入")
            except (EOFError, OSError, ValueError):
                pass
            finally:
                closed.set()
                with self.input_lock:
                    input_state.release()
                    self.input_state = None

        reader = threading.Thread(target=read_controls, daemon=True)
        reader.start()
        encoder = None
        format_key = None
        screen = None
        duplicate = None
        dxgi_failed = False
        next_frame = time.perf_counter()
        paced_fps = None
        try:
            while not closed.is_set() and not self.stop_event.is_set():
                active_settings = encoder_settings
                fps = active_settings[-2]
                interval = 1 / fps
                now = time.perf_counter()
                if fps != paced_fps:
                    paced_fps = fps
                    next_frame = now
                if now < next_frame:
                    time.sleep(next_frame - now)
                    if closed.is_set() or self.stop_event.is_set():
                        break
                image = self.capture_frame() if self.capture_frame else None
                source_size = image.size if image is not None else primary_size()
                new_format = (source_size, active_settings)
                if new_format != format_key:
                    encoder = Encoder(source_size, active_settings, codec_markers=hello.get("codec_markers") is True)
                    format_key = new_format
                if image is None:
                    if fps >= 60 and not dxgi_failed:
                        try:
                            if duplicate is not None and duplicate.target_fps != fps:
                                duplicate.close()
                                duplicate = None
                            if duplicate is None:
                                duplicate = DesktopDuplication(fps)
                                self.status("已连接 · DXGI 屏幕采集")
                            image = duplicate.frame(source_size)
                        except Exception:
                            if duplicate:
                                duplicate.close()
                                duplicate = None
                            dxgi_failed = True
                            self.status("DXGI 不可用 · 已改用兼容采集")
                    elif duplicate:
                        duplicate.close()
                        duplicate = None
                    if image is None:
                        target = encoder.width, encoder.height
                        if screen and (screen.width, screen.height) != target:
                            screen.close()
                            screen = None
                        if screen is None:
                            screen = ScreenCapture(target)
                        image = screen.frame(source_size)
                # Emit diagnostics on the video writer thread to preserve frame boundaries.
                with probe_lock:
                    nonce, pending_probe = pending_probe, None
                if nonce is not None:
                    send(conn, STATS_REPLY.pack(STATS_KIND, nonce))
                for flags, packet in encoder.encode_packets(image):
                    size = packet.size + 1
                    if size > MAX_FRAME:
                        raise ValueError("编码报文过大")
                    conn.sendall(b"".join((VIDEO_HEADER.pack(size, flags), packet)))
                next_frame += interval
                now = time.perf_counter()
                # Skip missed slots without letting a stalled frame trigger a burst.
                if now > next_frame + interval:
                    missed = int((now - next_frame) / interval)
                    next_frame += missed * interval
        finally:
            if duplicate:
                duplicate.close()
            if screen:
                screen.close()
            closed.set()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            reader.join()


