import argparse
import json
from pathlib import Path
import queue
import secrets
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk, simpledialog

from desktop import dpi_aware, root_window
from key_capture import KeyCapture, VK_F12
from link import (ADDRESS, PORT, MAX_FRAME,
                  Host, receive, receive_message, send_message, STATS_REPLY, STATS_KIND)

from quality import PRESETS, RATES, BITRATES, DEFAULT, dimensions_ok


def host_window(root):
    root.title("校园远控 · 被控端")
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill="both", expand=True)
    secret = tk.StringVar()
    status = tk.StringVar(value="共享已停止")
    enabled = tk.BooleanVar()
    updates = queue.SimpleQueue()
    host = None
    internet = None
    if updater := getattr(root, "updater", None):
        updater.mode = "host"
        updater.is_active = lambda: host is not None

    def toggle():
        nonlocal host, internet
        if host:
            if internet and internet.window.winfo_exists():
                internet.close()
            internet = None
            host.stop()
            host = None
            secret.set("")
            enabled.set(False)
            status.set("共享已停止")
            start.configure(text="开始共享")
        else:
            token = secrets.token_urlsafe(18)
            tag = object()
            try:
                host = Host(token, lambda message: updates.put((tag, message)))
            except OSError as error:
                status.set(str(error))
                return
            host.set_input_enabled(enabled.get())
            host.tag = tag
            secret.set(token)
            status.set("等待控制端连接")
            start.configure(text="停止共享")

    def change_input():
        if host:
            host.set_input_enabled(enabled.get())

    def refresh():
        while not updates.empty():
            tag, message = updates.get()
            if host and host.tag is tag:
                status.set(message)
        root.after(100, refresh)

    ttk.Label(panel, text="Windows 主屏幕共享", font=("Segoe UI", 16)).pack(anchor="w")
    ttk.Label(panel, text="支持本机、局域网和公网连接").pack(anchor="w", pady=(0, 12))
    ttk.Label(panel, textvariable=status).pack(anchor="w")
    ttk.Entry(panel, textvariable=secret, width=34, state="readonly").pack(fill="x", pady=7)

    def copy():
        if secret.get():
            root.clipboard_clear()
            root.clipboard_append(secret.get())

    ttk.Button(panel, text="复制连接密钥", command=copy).pack(anchor="w")
    ttk.Checkbutton(panel, text="允许控制键鼠", variable=enabled, command=change_input).pack(anchor="w", pady=8)
    start = ttk.Button(panel, text="开始共享", command=toggle)
    start.pack(anchor="w")

    def public_link():
        nonlocal internet
        if not host:
            status.set("请先点击开始共享")
            return
        if internet and internet.window.winfo_exists():
            internet.window.deiconify()
            internet.window.lift()
        else:
            from public_ui import InternetDialog

            internet = InternetDialog(root, host=host)

    ttk.Button(panel, text="设备连接", command=public_link).pack(anchor="w", pady=(5, 0))

    def close():
        if internet and internet.window.winfo_exists():
            internet.close()
        if host:
            host.stop()
            host.worker.join(4)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()


class Viewer:
    def __init__(self, root):
        self.root = root
        if updater := getattr(root, "updater", None):
            updater.mode = "viewer"
            updater.is_active = lambda: self.worker is not None
        self.port = PORT
        self.route = ""
        self.internet = None
        self.conn = None
        self.write_lock = threading.Lock()
        self.worker = None
        self.generation = 0
        self.events = queue.SimpleQueue()
        self.frames = queue.Queue(maxsize=1)
        self.bounds = None
        self.picture_focused = False
        self.focus_generation = 0
        self.keys = KeyCapture()
        self.control = tk.BooleanVar()
        self.status = tk.StringVar(value="未连接")
        root.title("校园远控 · 控制端")
        root.geometry("1000x680")
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="连接密钥").pack(side="left")
        self.token = ttk.Entry(bar, width=28, show="●")
        self.token.pack(side="left", padx=5)
        self.button = ttk.Button(bar, text="连接", command=self.toggle)
        self.button.pack(side="left")
        ttk.Button(bar, text="设备连接", command=self.public_link).pack(side="left", padx=(5, 0))
        self.selected_size = DEFAULT[:2]
        self.size_choice = ttk.Combobox(bar, state="readonly", width=13,
                                        values=(*PRESETS, "自定义…"))
        self.size_choice.set("1080p")
        self.size_choice.pack(side="left", padx=(8, 2))
        self.size_choice.bind("<<ComboboxSelected>>", self.choose_resolution)
        self.size_label = "1080p"
        self.custom_size = None
        self.options = []
        for name, values, initial in zip(("目标 FPS", "Mbps"), (RATES, BITRATES), DEFAULT[2:]):
            ttk.Label(bar, text=name).pack(side="left", padx=(7, 2))
            field = ttk.Combobox(bar, values=values, width=5, state="readonly")
            field.set(initial)
            field.pack(side="left")
            field.bind("<<ComboboxSelected>>", lambda _: self.send_settings())
            self.options.append(field)
        ttk.Checkbutton(bar, text="发送键鼠", variable=self.control, command=self.control_changed).pack(side="left", padx=8)
        text_bar = ttk.Frame(root, padding=(8, 0))
        text_bar.pack(fill="x")
        ttk.Label(text_bar, text="发送文字").pack(side="left")
        self.text_entry = ttk.Entry(text_bar, width=32)
        self.text_entry.pack(side="left", padx=5)
        self.text_entry.bind("<Return>", self.send_text)
        ttk.Button(text_bar, text="发送", command=self.send_text).pack(side="left")
        self.text_status = tk.StringVar()
        self.pending_text = None
        ttk.Label(text_bar, textvariable=self.text_status).pack(side="left", padx=6)
        ttk.Button(text_bar, text="网络统计", command=self.toggle_stats).pack(side="right")
        ttk.Label(root, textvariable=self.status, padding=6).pack(fill="x")
        self.canvas = tk.Canvas(root, background="#101318", highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self.viewport = (1000, 600)
        self.preview_visible = False
        self.canvas.bind("<Configure>", self.resize)
        self.canvas.bind("<Map>", self.preview_visibility)
        self.canvas.bind("<Unmap>", self.preview_visibility)
        self.canvas.bind("<Motion>", self.move)
        self.canvas.bind("<ButtonPress>", lambda e: self.button_event(e, True))
        self.canvas.bind("<ButtonRelease>", lambda e: self.button_event(e, False))
        self.canvas.bind("<MouseWheel>", self.wheel_event)
        self.canvas.bind("<Leave>", self.clear_picture_focus)
        self.canvas.bind("<FocusOut>", self.clear_picture_focus)
        self.last_move = 0
        self.photo = None
        self.view = self.canvas.create_image(0, 0, anchor="nw")
        self.stats = [0, 0]
        self.stats_since = time.monotonic()
        self.last_ping = 0
        self.last_native = None
        self.stats_window = None
        self.stats_label = None
        self.last_rtt = None
        self.last_fps = None
        self.last_mbps = None
        self.last_codecs = None
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def public_link(self):
        if self.internet:
            self.internet.window.deiconify()
            self.internet.window.lift()
        else:
            from public_ui import InternetDialog

            self.internet = InternetDialog(self.root, viewer=self)

    def resize(self, event):
        self.viewport = max(1, event.width), max(1, event.height)

    def preview_visibility(self, event):
        self.preview_visible = event.type == tk.EventType.Map
        if self.preview_visible:
            self.stats = [0, 0]
            self.stats_since = time.monotonic()
        elif self.photo is not None:
            self.canvas.itemconfigure(self.view, image="")
            self.photo = self.bounds = None

    def toggle(self):
        if self.worker:
            self.disconnect()
            return
        secret = self.token.get().strip()
        if not secret:
            self.status.set("请输入被控端的连接密钥")
            return
        self.generation += 1
        viewer_hwnd = None if self.route else root_window(self.root.winfo_id())
        self.worker = threading.Thread(
            target=self.read, args=(self.generation, secret, self.port, viewer_hwnd,
                                    self.route == "局域网直连"), daemon=True,
        )
        self.button.configure(text="断开")
        self.status.set("正在连接")
        self.worker.start()

    def read(self, generation, secret, port, viewer_hwnd, lan=False):
        import av
        from video import Decoder

        try:
            with socket.create_connection((ADDRESS, port), timeout=3) as conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                send_message(conn, {"op": "hello", "secret": secret, "video": "h264-v2",
                                    "codec_markers": True, "viewer_hwnd": viewer_hwnd, "frame_ack": lan})
                hello = receive_message(conn)
                if hello.get("video") != "h264-v2":
                    self.events.put((generation, "被控端版本不兼容", None))
                    return
                if hello.get("ok") is not True:
                    self.events.put((generation, "连接密钥无效", None))
                    return
                conn.settimeout(5)
                self.events.put((generation, "connected", conn))
                decoder = Decoder()
                pending_bytes = 0
                confirm_frames = lan and hello.get("frame_ack") is True
                processed = 0
                while generation == self.generation:
                    blob = receive(conn, MAX_FRAME)
                    if blob[0] == STATS_KIND:
                        if len(blob) == STATS_REPLY.size:
                            _, timestamp = STATS_REPLY.unpack(blob)
                            now = time.monotonic_ns()
                            if 0 <= now - timestamp < 15_000_000_000:
                                self.events.put((generation, "rtt", (now - timestamp) / 1e6))
                        continue
                    pending_bytes += len(blob) + 4
                    for frame in decoder.decode_frames(blob):
                        if self.preview_visible:
                            image = decoder.preview(frame, self.viewport)
                            native = frame.width, frame.height
                            try:
                                self.frames.get_nowait()
                            except queue.Empty:
                                pass
                            self.frames.put_nowait((generation, native, image, decoder.encoder_name,
                                                    decoder.decoder_name))
                        self.events.put((generation, "bytes", pending_bytes))
                        pending_bytes = 0
                    if confirm_frames:
                        processed += 1
                        with self.write_lock:
                            send_message(conn, {"op": "frame_ack", "count": processed})
        except (OSError, EOFError, ValueError, av.FFmpegError):
            self.events.put((generation, "连接已断开或被控端未启动", None))

    def send(self, message):
        if self.conn:
            try:
                with self.write_lock:
                    send_message(self.conn, message)
                return True
            except OSError:
                self.disconnect("连接已断开")
        return False

    def choose_resolution(self, _event=None):
        choice = self.size_choice.get()
        if choice in PRESETS:
            self.selected_size = PRESETS[choice]
        elif choice == "自定义…":
            dialog = CustomResolution(self.root, self.custom_size or self.selected_size)
            if dialog.result is None:
                self.size_choice.set(self.size_label)
                return
            self.custom_size = self.selected_size = dialog.result
            choice = f"{self.selected_size[0]}×{self.selected_size[1]}"
            self.size_choice.configure(values=(*PRESETS, choice, "自定义…"))
            self.size_choice.set(choice)
        elif self.custom_size and choice == f"{self.custom_size[0]}×{self.custom_size[1]}":
            self.selected_size = self.custom_size
        else:
            self.size_choice.set(self.size_label)
            return
        self.size_label = choice
        self.send_settings()

    def send_settings(self):
        width, height = self.selected_size
        fps, mbps = (int(field.get()) for field in self.options)
        self.send(dict(op="settings", width=width, height=height, fps=fps, mbps=mbps))

    def send_text(self, _event=None):
        if not self.conn or not self.control.get():
            self.text_status.set("请先连接并开启键鼠")
            return "break"
        text = self.text_entry.get()
        if not 0 < len(text) <= 128 or not text.isprintable():
            self.text_status.set("请输入 1–128 个可打印字符")
            return "break"
        self.cancel_text()
        generation = self.generation
        self.text_status.set("正在发送" if self.route else "2 秒后发送，请切换到目标窗口")

        def deliver():
            self.pending_text = None
            if generation != self.generation or not self.control.get():
                return
            if self.send({"op": "text", "text": text}):
                if self.text_entry.get() == text:
                    self.text_entry.delete(0, tk.END)
                self.text_status.set("")

        self.pending_text = self.root.after(0 if self.route else 2000, deliver)
        return "break"

    def cancel_text(self):
        if self.pending_text is not None:
            self.root.after_cancel(self.pending_text)
            self.pending_text = None
        self.text_status.set("")

    def disconnect(self, message="已断开"):
        self.clear_picture_focus(send_release=False)
        self.cancel_text()
        if self.route and self.internet and self.internet.session:
            self.internet.session.close()
        if self.route:
            self.port = PORT
            self.route = ""
        self.generation += 1
        if self.conn:
            try:
                with self.write_lock:
                    send_message(self.conn, {"op": "release"})
                    self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.conn = self.worker = self.bounds = self.photo = None
        self.last_rtt = self.last_native = self.last_fps = self.last_mbps = self.last_codecs = None
        self.update_stats_window()
        self.canvas.itemconfigure(self.view, image="")
        self.control.set(False)
        self.button.configure(text="连接")
        self.status.set(message)

    def close(self):
        if self.internet:
            self.internet.close()
        self.disconnect()
        self.keys.close()
        self.root.destroy()

    def control_changed(self):
        if not self.control.get():
            self.cancel_text()
            self.clear_picture_focus()

    def clear_picture_focus(self, _event=None, *, send_release=True):
        if not self.picture_focused:
            return
        self.picture_focused = False
        self.focus_generation += 1
        self.keys.target_window(None,self.focus_generation)
        if send_release:
            self.send({"op":"release"})

    def forward_keys(self):
        while not self.keys.events.empty():
            generation, vk, down, extended = self.keys.events.get()
            if not self.picture_focused or generation != self.focus_generation:
                continue
            if vk == VK_F12:
                self.control.set(False)
                self.control_changed()
            else:
                self.input("key",code=vk,down=down,extended=extended)

    def refresh(self):
        self.forward_keys()
        while not self.events.empty():
            generation, message, conn = self.events.get()
            if generation != self.generation:
                if message == "connected":
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                continue
            if message == "rtt":
                self.last_rtt = (conn, time.monotonic())
                self.update_stats_window()
                continue
            if message == "bytes":
                self.stats[1] += conn
                continue
            if message == "connected":
                self.conn = conn
                self.status.set("已连接 · 等待画面")
                self.last_native = None
                self.stats = [0, 0]
                self.stats_since = time.monotonic()
                self.send_settings()
            else:
                self.disconnect(message)
        try:
            generation, native, image, encoder_name, decoder_name = self.frames.get_nowait()
        except queue.Empty:
            pass
        else:
            if self.conn and generation == self.generation and self.preview_visible:
                self.draw(native, image, encoder_name, decoder_name)
        if self.conn and time.monotonic() - self.last_ping >= 1:
            self.last_ping = time.monotonic()
            self.send({'op': 'ping', 'probe': time.monotonic_ns()})
            self.update_stats_window()
        self.root.after(2 if self.conn else 20, self.refresh)

    def draw(self, native, image, encoder_name, decoder_name):
        from PIL import Image, ImageTk

        if self.last_native != native:
            self.last_native = native
            self.stats_since = time.monotonic()
            self.stats = [0, 0]
            self.status.set(f"{self.route or '本机直连'} · {native[0]}×{native[1]} · 正在统计帧率")
        width, height = self.viewport
        image.thumbnail((width, height), Image.Resampling.BILINEAR)
        x, y = (width - image.width) // 2, (height - image.height) // 2
        bounds = x, y, image.width, image.height
        if self.photo is None or (self.photo.width(), self.photo.height()) != image.size:
            self.photo = ImageTk.PhotoImage(image, master=self.root)
            self.canvas.itemconfigure(self.view, image=self.photo)
        else:
            self.photo.paste(image)
        if self.bounds != bounds:
            self.bounds = bounds
            self.canvas.coords(self.view, x, y)
        self.stats[0] += 1
        elapsed = time.monotonic() - self.stats_since
        if elapsed >= 1:
            self.last_fps = self.stats[0] / elapsed
            self.last_mbps = self.stats[1] * 8 / elapsed / 1e6
            self.last_codecs = f"{encoder_name} → {decoder_name}"
            self.update_stats_window()
            self.status.set(f"{self.route or '本机直连'} · {native[0]}×{native[1]} · 显示 {self.stats[0] / elapsed:.1f} FPS"
                            f" · {encoder_name} → {decoder_name} · 视频流 {self.stats[1] * 8 / elapsed / 1e6:.2f} Mbps · {self.input_status()}")
            self.stats = [0, 0]
            self.stats_since = time.monotonic()

    def toggle_stats(self):
        if self.stats_window is not None:
            self.close_stats_window()
            return
        window = tk.Toplevel(self.root)
        window.title("网络统计")
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.close_stats_window)
        self.stats_window = window
        self.stats_label = ttk.Label(window, padding=12, width=33, justify="left")
        self.stats_label.pack()
        self.update_stats_window()

    def close_stats_window(self):
        if self.stats_window is not None:
            self.stats_window.destroy()
            self.stats_window = self.stats_label = None

    def update_stats_window(self):
        if self.stats_label is None:
            return
        route = (self.route or "本机直连") if self.conn else "未连接"
        rtt = self.last_rtt
        latency = (f"{rtt[0]:.1f} ms" if rtt and time.monotonic()-rtt[1]<4
                   else "暂无回包" if self.conn else "—")
        bitrate = f"{self.last_mbps:.2f} Mbps" if self.last_mbps is not None else "—"
        fps = f"{self.last_fps:.1f} FPS" if self.last_fps is not None else "—"
        size = f"{self.last_native[0]}×{self.last_native[1]}" if self.last_native else "—"
        self.stats_label.configure(text="\n".join((f"连接    {route}", f"往返延迟    {latency}",
            f"接收码率    {bitrate}", f"显示帧率    {fps}", f"画面    {size}",
            f"编解码    {self.last_codecs or '—'}")))

    def input_status(self):
        if not self.control.get():
            return "键鼠未开启"
        if not self.picture_focused:
            return "点击画面接管键鼠"
        if self.keys.failure is not None:
            return "键盘接管失败 · 鼠标可用"
        if not self.keys.ready.is_set():
            return "正在接管键盘…"
        return "键鼠仅送远端 · F12 退出"

    def input(self, op, **fields):
        if self.control.get() and self.picture_focused:
            self.send(dict(op=op, **fields))

    def inside_picture(self, event):
        if not self.bounds:
            return False
        x,y,width,height=self.bounds
        return x <= event.x < x+width and y <= event.y < y+height

    def move(self, event, force=False):
        if not self.inside_picture(event):
            self.clear_picture_focus()
            return
        if not self.picture_focused:
            return
        now=time.monotonic()
        if not force and now-self.last_move<1/60:
            return
        self.last_move=now
        x,y,width,height=self.bounds
        self.input("move", x=(event.x-x)*10000//max(1,width-1),
                   y=(event.y-y)*10000//max(1,height-1))

    def wheel_event(self,event):
        if self.inside_picture(event):
            self.input("wheel",delta=event.delta)
        else:
            self.clear_picture_focus()

    def button_event(self,event,down):
        if not self.inside_picture(event):
            self.clear_picture_focus()
            return
        if not self.conn or not self.control.get() or event.num not in (1,2,3):
            return
        if down and not self.picture_focused:
            self.canvas.focus_set()
            self.picture_focused=True
            self.focus_generation+=1
            self.keys.target_window(root_window(self.root.winfo_id()),self.focus_generation)
        self.move(event,True)
        self.input("button",code=event.num,down=down)


class CustomResolution(simpledialog.Dialog):
    def __init__(self, parent, size):
        self.size = size
        super().__init__(parent, "自定义视频分辨率")

    def body(self, master):
        fields = []
        for row, (name, value) in enumerate(zip(("宽度", "高度"), self.size)):
            ttk.Label(master, text=name).grid(row=row, column=0, padx=7, pady=5)
            field = ttk.Entry(master, width=10)
            field.insert(0, str(value))
            field.grid(row=row, column=1, padx=7)
            fields.append(field)
        self.fields = fields
        self.error = ttk.Label(master, wraplength=280)
        self.error.grid(row=2, column=0, columnspan=2, padx=7)
        return fields[0]

    def validate(self):
        try:
            width, height = (int(field.get()) for field in self.fields)
        except ValueError:
            width = height = 0
        if not dimensions_ok(width, height, sending=True):
            self.error.configure(text="请输入偶数宽高，最小 320×180，单边最多 4096，总像素最多 4096×2160。")
            return False
        self.result = width, height
        return True


def choose_mode(root):
    root.title("校园远控")
    panel = ttk.Frame(root, padding=24)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="校园远控", font=("Segoe UI", 18)).pack(anchor="w")
    ttk.Label(panel, text="选择本次启动的角色").pack(anchor="w", pady=(4, 12))

    def select(mode):
        panel.destroy()
        root.resizable(True, True)
        root.geometry("")
        if mode == "host":
            host_window(root)
        else:
            Viewer(root)

    host_button = ttk.Button(panel, text="被控端 · 共享本机桌面", command=lambda: select("host"))
    host_button.pack(fill="x", pady=(0, 6))
    ttk.Button(panel, text="控制端 · 查看和操作", command=lambda: select("viewer")).pack(fill="x")
    host_button.focus_set()
    root.resizable(False, False)


def diagnose(root, report, public=False, lan=False):
    """Exercise the same capture and playback path as the two app modes."""
    if lan:
        from lan import LanHost, LanClient
    if public:
        from internet import InternetSession, turn_options

    samples = []
    capture_status = []
    token = secrets.token_urlsafe(18)
    receiver = Viewer(root)
    root.geometry("1920x1140")
    receiver.token.insert(0, token)
    with HostForDiagnosis(token, capture_status.append) as host:
        host_peer = viewer_peer = None
        if lan:
            device = "A123456789BC"
            password = secrets.token_urlsafe(18)
            host_peer = LanHost(host, device, password, port=0)
        elif public:
            options = turn_options("", "", "", stun=None)
            host_peer = InternetSession(options, host_port=host.port, token=token)
        else:
            receiver.port = host.port
            receiver.toggle()
        deadline = time.monotonic() + 23
        last = None

        def sample():
            nonlocal last, viewer_peer
            if host_peer:
                while not host_peer.events.empty():
                    kind, value = host_peer.events.get()
                    if kind == "listening" and lan:
                        viewer_peer = LanClient(f"127.0.0.1:{value}",device,password,
                                               {"device":"B123456789AC","name":"诊断控制端"})
                    elif kind == "offer":
                        viewer_peer = InternetSession(options, offer=value)
                    elif kind in ("error", "route"):
                        capture_status.append(value)
                if viewer_peer:
                    while not viewer_peer.events.empty():
                        kind, value = viewer_peer.events.get()
                        if kind == "answer":
                            host_peer.submit(value)
                        elif kind == "route":
                            receiver.route = value
                        elif kind == "port":
                            receiver.port = value
                            receiver.toggle()
                        elif kind == "error":
                            capture_status.append(value)
            stamp = receiver.stats_since
            message = receiver.status.get()
            if stamp != last and receiver.last_native == (1920, 1080) and " FPS" in message:
                samples.append(message)
            last = stamp
            if len(samples) < 10 and time.monotonic() < deadline:
                root.after(50, sample)
            else:
                root.quit()

        root.protocol("WM_DELETE_WINDOW", root.quit)
        root.after(50, sample)
        root.mainloop()
        receiver.disconnect()
        for peer in (host_peer, viewer_peer):
            if peer:
                peer.close()
        for peer in (host_peer, viewer_peer):
            if peer:
                peer.thread.join(2)
    root.destroy()
    result = {"completed": len(samples) >= 10, "capture": capture_status, "video_samples": samples}
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["completed"] else 1


class HostForDiagnosis(Host):
    def __init__(self, secret, status):
        super().__init__(secret, status, port=0)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()
        self.worker.join(4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("host", "viewer"))
    parser.add_argument('--diagnose', type=Path, metavar='REPORT.json')
    parser.add_argument('--diagnose-public', type=Path, metavar='REPORT.json')
    parser.add_argument('--diagnose-lan', type=Path, metavar='REPORT.json')
    args = parser.parse_args()
    dpi_aware()
    root = tk.Tk()
    if args.diagnose_lan:
        raise SystemExit(diagnose(root, args.diagnose_lan, lan=True))
    if args.diagnose_public:
        raise SystemExit(diagnose(root, args.diagnose_public, public=True))
    if args.diagnose:
        raise SystemExit(diagnose(root, args.diagnose))
    from updater import AutoUpdater, mark_ready

    root.updater = AutoUpdater(root)
    if args.mode == "host":
        host_window(root)
    elif args.mode == "viewer":
        Viewer(root)
    else:
        choose_mode(root)
    root.after_idle(mark_ready)
    root.mainloop()






