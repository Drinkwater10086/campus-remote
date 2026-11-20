import re
import tkinter as tk
from tkinter import simpledialog, ttk

from device_profile import Profile
from internet import turn_options
from lan import LanHost, LanClient, LAN_PORT, ipv4_addresses
from rendezvous import AutoPair, Board
from setup_signal import SignalSetup, connection_code, read_connection_code
from setup_render import RenderSetup


class InternetDialog:
    def __init__(self, root, host=None, viewer=None):
        self.host, self.viewer = host, viewer
        self.session = None
        self.deployment = None
        self.route = ""
        self.window = tk.Toplevel(root)
        self.window.title("设备连接 · 被控端" if host else "设备连接 · 控制端")
        self.window.transient(root)
        self.window.protocol("WM_DELETE_WINDOW", self.hide_or_close)
        panel = ttk.Frame(self.window, padding=14)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)
        self.status = tk.StringVar(self.window)
        self.known_status = tk.StringVar(self.window)
        try:
            self.profile = Profile()
        except (OSError, ValueError) as error:
            ttk.Label(panel, text=f"无法打开 Windows 设备档案：{error}", wraplength=460).pack()
            return
        data = self.profile.data
        self.panel = panel
        self.hint = tk.StringVar(self.window)
        ttk.Label(panel, textvariable=self.hint, wraplength=530).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.mode = tk.StringVar(self.window, value=data.get("mode", "局域网"))
        mode = ttk.Combobox(panel, textvariable=self.mode, values=("局域网", "公网"), state="readonly", width=11)
        mode.grid(row=1, column=0, sticky="w")
        mode.bind("<<ComboboxSelected>>", self.show_mode)
        self.mode_widget = mode
        self.server = self.field(panel, 12, "信令地址", data["server"])
        if host:
            self.deploy_button = ttk.Button(panel, text="部署 Cloudflare", command=self.deploy)
            self.deploy_button.grid(row=12, column=2, padx=(5, 0))
            self.render_button = ttk.Button(panel, text="部署 Render", command=self.deploy_render)
            self.render_button.grid(row=14, column=2, padx=(5, 0))
            ttk.Button(panel, text="复制公网连接码", command=self.copy_public).grid(
                row=14, column=1, sticky="w", pady=(5, 0))
        else:
            ttk.Button(panel, text="从剪贴板导入连接码", command=self.import_public).grid(
                row=14, column=1, sticky="w", pady=(5, 0))
        ips = ", ".join(ipv4_addresses()) if host else data.get("lan", "")
        self.lan_address = self.field(panel, 13, "本机 IPv4" if host else "对方 IPv4",
                                      ips, readonly=bool(host))
        if host:
            self.device = self.field(panel, 3, "本机设备号", data["device"], readonly=True)
            self.password = self.field(panel, 4, "固定密码", data["password"], readonly=True, show="●")
            ttk.Button(panel, text="复制", command=lambda: self.copy(self.device.get())).grid(row=3, column=2)
            ttk.Button(panel, text="复制", command=lambda: self.copy(self.password.get())).grid(row=4, column=2)
        else:
            ttk.Label(panel, text="已保存设备").grid(row=2, column=0, sticky="w")
            self.known = ttk.Combobox(panel, state="readonly", width=43)
            self.known.grid(row=2, column=1, sticky="ew")
            self.known.bind("<<ComboboxSelected>>", self.select_saved)
            ttk.Button(panel, text="忘记", command=self.forget).grid(row=2, column=2)
            self.device = self.field(panel, 3, "对方设备号", "")
            self.password = self.field(panel, 4, "固定密码", "", show="●")
        self.turn = self.field(panel, 5, "TURN 地址", data["turn"][0])
        self.turn_user = self.field(panel, 6, "TURN 用户名", data["turn"][1])
        self.turn_pass = self.field(panel, 7, "TURN 密码", data["turn"][2], show="●")
        self.force = tk.BooleanVar(self.window)
        ttk.Checkbutton(panel, text="仅使用 TURN（测试中继）", variable=self.force).grid(row=8, column=1, sticky="w")
        self.start = ttk.Button(panel, text="上线等待连接" if host else "连接设备", command=self.begin)
        self.start.grid(row=9, column=1, sticky="w")
        self.stop_button = ttk.Button(panel, text="停止监听" if host else "取消连接", command=self.stop, state="disabled")
        self.stop_button.grid(row=9, column=2)
        ttk.Label(panel, textvariable=self.status, wraplength=520).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=8)
        if host:
            ttk.Label(panel, textvariable=self.known_status, wraplength=520).grid(
                row=11, column=0, columnspan=3, sticky="w")
        self.refresh_devices()
        self.show_mode()
        self.window.after(70, self.poll)

    def show_mode(self, _event=None):
        lan = self.mode.get()=="局域网"
        for row in (5,6,7,8,12,13,14):
            widgets = [child for child in self.panel.winfo_children()
                       if child.grid_info().get("row")==row]
            if widgets:
                if not hasattr(self, "_mode_rows"):
                    self._mode_rows = {}
                self._mode_rows[row] = widgets
            for widget in getattr(self, "_mode_rows", {}).get(row, []):
                if ((row==13) == lan):
                    widget.grid()
                else:
                    widget.grid_remove()
        if lan:
            self.hint.set("无需外网。两台电脑需在可互通的内网，通过被控端 IPv4 连接。")
        else:
            self.hint.set("首次可自动部署信令，随后复制连接码给控制端。" if self.host
                          else "首次导入被控端的连接码，之后可在设备列表连接。")

    def field(self, panel, row, label, value, readonly=False, show=""):
        text = tk.StringVar(self.window, value=value)
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(panel, width=45, textvariable=text, show=show,
                  state="readonly" if readonly else "normal").grid(row=row, column=1, sticky="ew")
        return text

    def copy(self, value):
        self.window.clipboard_clear()
        self.window.clipboard_append(value)
        self.status.set("已复制。固定密码只发给你允许连接的人。")

    def deploy(self):
        if self.session or self.deployment:
            return
        try:
            self.deployment = SignalSetup(self.profile.data["device"])
        except ValueError as error:
            self.status.set(str(error))
            return
        self.deployment_started()

    def deploy_render(self):
        if self.session or self.deployment:
            return
        key = simpledialog.askstring("部署 Render · 免费信令服务",
            "请输入 Render API Key。软件只用它创建和检查服务，不会保存。\n"
            "在 Render 账号设置的 API Keys 页面创建。", show="●", parent=self.window)
        if not key:
            return
        try:
            self.deployment = RenderSetup(self.profile.data["device"], key)
        except ValueError as error:
            self.status.set(str(error))
            return
        self.deployment_started()

    def deployment_started(self):
        self.start.configure(state="disabled")
        self.mode_widget.configure(state="disabled")
        self.deploy_button.configure(state="disabled")
        self.render_button.configure(state="disabled")
        self.stop_button.configure(state="normal", text="取消等待")

    def copy_public(self):
        try:
            code = connection_code(self.server.get(),self.device.get(),self.password.get())
        except ValueError:
            self.status.set("请先自动部署，或填写可用的 HTTPS 信令地址。")
            return
        self.copy(code)
        self.status.set("连接码已复制，其中包含固定密码，请私下发送给控制端。")

    def import_public(self):
        if self.session:
            return
        try:
            server, device, password = read_connection_code(self.window.clipboard_get())
        except (ValueError,tk.TclError) as error:
            self.status.set(str(error) if isinstance(error,ValueError) else "剪贴板没有可导入的连接码")
            return
        self.server.set(server)
        self.device.set(device)
        self.password.set(password)
        self.mode.set("公网")
        self.show_mode()
        self.begin()

    def poll_deployment(self):
        while self.deployment and not self.deployment.events.empty():
            kind, value = self.deployment.events.get()
            if kind in ("status","error"):
                self.status.set(value)
            elif kind == "accounts":
                rows = "\n".join(f"{i+1}. {name}" for i, (_,name) in enumerate(value[:50]))
                index = simpledialog.askinteger("选择部署工作区",
                    f"请输入部署目标的序号：\n{rows}",
                    parent=self.window, minvalue=1, maxvalue=len(value))
                if index is None:
                    self.deployment.close()
                    self.status.set("已取消部署")
                else:
                    self.deployment.choose(value[index-1][0])
            elif kind == "ready":
                try:
                    self.profile.update(server=value, mode="公网")
                except (ValueError,OSError) as error:
                    self.status.set(f"已部署但未保存：{error}")
                    self.server.set(value)
                    continue
                self.server.set(value)
                self.status.set("信令部署完成，正在上线…")
                self.begin()
            elif kind == "closed":
                self.deployment = None
                self.deploy_button.configure(state="normal")
                self.render_button.configure(state="normal")
                self.stop_button.configure(text="停止监听")
                if not self.session:
                    self.start.configure(state="normal")
                    self.mode_widget.configure(state="readonly")
                    self.stop_button.configure(state="disabled")

    def refresh_devices(self):
        if self.host:
            known = [f'{name} ({device})' for device, name in self.profile.data["seen"].items()]
            self.known_status.set("最近连接的控制端：" + "、".join(known[-5:]) if known else "尚无连接记录")
        else:
            saved = self.profile.data["saved"]
            self.known["values"] = [f'{item["name"]} · {device}' for device, item in saved.items()]

    def select_saved(self, _event=None):
        device = self.known.get().rsplit(" · ", 1)[-1]
        item = self.profile.data["saved"].get(device)
        if item:
            self.device.set(device)
            self.password.set(item["password"])
            self.server.set(item.get("server",""))
            self.lan_address.set(item.get("lan",""))
            self.mode.set(item.get("mode","公网"))
            self.show_mode()

    def forget(self):
        device = self.known.get().rsplit(" · ", 1)[-1]
        if device in self.profile.data["saved"]:
            try:
                self.profile.forget(device)
            except OSError as error:
                self.status.set(f"无法保存设备档案：{error}")
                return
            self.known.set("")
            self.device.set("")
            self.password.set("")
            self.refresh_devices()

    def begin(self):
        if self.session:
            return
        try:
            mode = self.mode.get()
            data = self.profile.data
            device = self.device.get().strip().replace(" ", "").replace("-", "").upper()
            password = self.password.get()
            identity = {"device": data["device"], "name": data["name"]}
            server = self.server.get().strip()
            address = self.lan_address.get().strip()
            self.selection = (device, password, server, address, mode)
            if mode == "局域网":
                if self.host:
                    self.profile.update(mode=mode)
                    self.session = LanHost(self.host, device, password, identity=data["name"])
                else:
                    from lan import parse_address, key_for
                    parse_address(address)
                    key_for(device,password)
                    self.profile.update(mode=mode, lan=address)
                    self.session = LanClient(address, device, password, identity)
                self.route = "局域网直连"
            else:
                board = Board(server, device, password)
                turn = [self.turn.get(), self.turn_user.get(), self.turn_pass.get()]
                options = turn_options(*turn, relay_only=self.force.get())
                self.profile.update(server=board.origin, turn=turn, mode=mode)
                self.session = AutoPair(board, options, identity, host=self.host)
                self.route = ""
        except (ValueError, OSError) as error:
            self.status.set(str(error) or "公网配置无效")
            return
        self.mode_widget.configure(state="disabled")
        self.start.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def remember(self, remote):
        device, name = remote.get("device"), remote.get("name")
        if not isinstance(device, str) or not re.fullmatch("[0-9A-F]{12}", device):
            return
        name = name[:64] if isinstance(name, str) and name.isprintable() else "Windows 设备"
        try:
            if self.host:
                self.profile.record("seen", device, name)
            else:
                selected, password, server, address, mode = self.selection
                if selected == device:
                    record = dict(self.profile.data["saved"].get(device, {}))
                    record.update(name=name, password=password, mode=mode)
                    if mode=="局域网":
                        record.update(lan=address)
                    else:
                        record.update(server=server)
                    self.profile.record("saved", device, record)
            self.refresh_devices()
        except (ValueError, OSError) as error:
            self.status.set(f"已连接，但无法保存设备：{error}")

    def poll(self):
        self.poll_deployment()
        while self.session and not self.session.events.empty():
            kind, value = self.session.events.get()
            if kind in ("status", "error"):
                self.status.set(value)
            elif kind == "remember":
                self.remember(value)
            elif kind == "listening":
                self.status.set(f"局域网在线 · TCP {value} · 等待连接")
            elif kind == "route":
                self.route = value
                self.status.set(f"{value} · 正在建立加密通道")
            elif kind == "connected":
                suffix = " · 设备已记住" if self.host else ""
                self.status.set(f"{self.route} · 已连接{suffix}")
            elif kind == "port" and self.viewer:
                if self.viewer.worker:
                    self.viewer.disconnect()
                self.viewer.port = value
                self.viewer.route = self.route
                self.viewer.token.delete(0, tk.END)
                self.viewer.token.insert(0, self.session.token)
                self.viewer.toggle()
                self.window.withdraw()
            elif kind == "closed":
                self.session = None
                self.mode_widget.configure(state="readonly")
                self.start.configure(state="normal")
                self.stop_button.configure(state="disabled")
                if self.viewer:
                    if self.viewer.route:
                        self.viewer.disconnect("公网连接已断开")
                    if self.window.state() == "withdrawn":
                        self.window.deiconify()
        self.window.after(70, self.poll)

    def stop(self):
        if self.deployment:
            self.deployment.close()
            self.status.set("已取消等待。已提交的部署可在对应平台控制台查看。")
        if self.session:
            self.session.close()
            self.status.set("正在停止连接…")

    def hide_or_close(self):
        if self.host and self.session:
            self.window.withdraw()
        else:
            self.close()

    def close(self):
        if self.deployment:
            self.deployment.close()
        if self.session:
            self.session.close()
        if self.viewer:
            if self.viewer.route:
                self.viewer.disconnect()
            self.viewer.internet = None
        self.window.destroy()

