"""Deploy the bundled Cloudflare signaling Worker after local browser authorization."""
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

from rendezvous import OPENER, signal_url, validate
from internet import pack

WRANGLER = "wrangler@4.119.0"
API = "https://api.cloudflare.com/client/v4"
ASSETS = Path(__file__).resolve().parent / "signal"
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def connection_code(url, device, password):
    device, password = validate(device, password)
    return "CR1-" + pack({"url": signal_url(url), "device":device, "password":password})


def read_connection_code(code):
    import base64
    code = code.strip()
    if not code.startswith("CR1-") or len(code) > 2400:
        raise ValueError("剪贴板里没有 CampusRemote 公网连接码")
    code = code[4:]
    try:
        data = json.loads(base64.b64decode(code + "=" * (-len(code) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict):
            raise ValueError
        device, password = validate(data["device"], data["password"])
        return signal_url(data["url"]), device, password
    except (AttributeError, KeyError, TypeError, RecursionError, ValueError) as error:
        raise ValueError("连接码格式无效") from error


class ApiError(OSError):
    def __init__(self, status, codes):
        self.status, self.codes = status, codes
        suffix = "，Cloudflare " + ", ".join(map(str, codes)) if codes else ""
        super().__init__(f"Cloudflare API 请求失败（HTTP {status}{suffix}）")


def cloudflare(path, token, data=None):
    request = urllib.request.Request(API + path, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"},
                                     method="GET" if data is None else "PUT")
    try:
        with OPENER.open(request, timeout=12) as response:
            result = json.loads(response.read(600000))
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            result = json.loads(error.read(600000))
        except (OSError, ValueError):
            result = {}
    if not isinstance(result, dict) or result.get("success") is not True or status >= 400:
        errors = result.get("errors") if isinstance(result, dict) else None
        codes = [x["code"] for x in (errors or []) if isinstance(x, dict) and type(x.get("code")) is int]
        raise ApiError(status,codes)
    return result.get("result")


def npx_command():
    node = shutil.which("node.exe")
    if node:
        launcher = Path(node).with_name("node_modules") / "npm" / "bin" / "npx-cli.js"
        if launcher.is_file():
            return [node, str(launcher), "--yes", WRANGLER]
    raise OSError("自动部署需要 Node.js LTS（含 npm）。请安装后重启软件。")


class SignalSetup:
    def __init__(self, device):
        if not re.fullmatch("[A-F0-9]{12}", device):
            raise ValueError("本机设备号格式无效")
        self.name = "campus-remote-" + device.lower()
        self.events = queue.SimpleQueue()
        self.choice = queue.Queue(1)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()

    def choose(self, account):
        try:
            self.choice.put_nowait(account)
        except queue.Full:
            pass

    def report(self, kind, value):
        self.events.put((kind,value))

    def cli(self, workspace, args, timeout, account=None):
        if self.stopping.is_set():
            raise InterruptedError
        env = os.environ.copy()
        env.pop("FORCE_COLOR", None)
        env.update(WRANGLER_SEND_METRICS="false", NO_COLOR="1",
                   npm_config_update_notifier="false")
        if account:
            env["CLOUDFLARE_ACCOUNT_ID"] = account
            env["CI"] = "true"
        with subprocess.Popen([*npx_command(), *args], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW) as command:
            remaining = timeout
            while remaining > 0 and not self.stopping.is_set():
                try:
                    output, diagnostics = command.communicate(timeout=min(2,remaining))
                    return command.returncode, output if args[:2] == ["auth","token"] else output+"\n"+diagnostics
                except subprocess.TimeoutExpired:
                    remaining -= 2
            if command.poll() is None:
                killer = Path(os.environ.get("SystemRoot",r"C:\Windows")) / "System32" / "taskkill.exe"
                try:
                    subprocess.run([str(killer),"/PID",str(command.pid),"/T","/F"], timeout=5,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if command.poll() is None:
                    command.kill()
            if self.stopping.is_set():
                raise InterruptedError
            raise TimeoutError("Cloudflare 命令等待超时，请检查网络后重试")

    def token(self, work):
        code, output = self.cli(work, ["auth","token","--json"], 100)
        if code:
            self.report("status","请在浏览器登录 Cloudflare 并确认授权…")
            code, _ = self.cli(work, ["login","--device"], 325)
            if code:
                raise OSError("Cloudflare 授权未完成，请重试并检查浏览器提示")
            code, output = self.cli(work, ["auth","token","--json"], 30)
        if code:
            raise OSError("Cloudflare 授权信息无法读取")
        try:
            data = json.loads(output)
        except ValueError as error:
            raise OSError("Wrangler 未返回可用的授权信息") from error
        if not isinstance(data,dict) or data.get("type") not in ("oauth","api_token") or not isinstance(data.get("token"),str) or not data["token"]:
            raise OSError("请使用 Cloudflare 浏览器授权，或提供已配置的 API Token")
        return data["token"]

    def account(self, token):
        response = cloudflare("/accounts?per_page=50",token)
        accounts = {}
        for row in response or []:
            if isinstance(row,dict) and re.fullmatch(r"[a-fA-F0-9]{32}",str(row.get("id",""))):
                accounts[row["id"]] = str(row.get("name") or row["id"])
        preferred = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if preferred in accounts:
            return preferred
        if len(accounts) == 1:
            return next(iter(accounts))
        if not accounts:
            raise OSError("这个 Cloudflare 登录没有可用的账号")
        self.report("accounts",list(accounts.items()))
        while not self.stopping.is_set():
            try:
                chosen=self.choice.get(timeout=.3)
                if chosen in accounts:
                    return chosen
            except queue.Empty:
                pass
        raise InterruptedError

    def subdomain(self, account, token):
        path = f"/accounts/{account}/workers/subdomain"
        try:
            result = cloudflare(path, token)
        except ApiError as error:
            if 10007 not in error.codes:
                raise
            result = {}
        name = result.get("subdomain") if isinstance(result,dict) else None
        if not name:
            self.report("status","正在创建账号的 workers.dev 子域名…")
            name = "campus-" + secrets.token_hex(6)
            if self.stopping.is_set():
                raise InterruptedError
            result = cloudflare(path,token,{"subdomain":name})
            if isinstance(result,dict) and result.get("subdomain"):
                name = result["subdomain"]
        if not isinstance(name,str) or not SUBDOMAIN.fullmatch(name):
            raise OSError("Cloudflare 返回的子域名无效")
        return name

    def run(self):
        try:
            self.report("status","正在准备 Cloudflare 部署工具…")
            with tempfile.TemporaryDirectory(prefix="campus-signal-") as directory:
                work=Path(directory)
                for name in ("worker.mjs","wrangler.toml"):
                    shutil.copy2(ASSETS/name,work/name)
                token=self.token(work)
                account=self.account(token)
                if self.stopping.is_set():
                    raise InterruptedError
                subdomain=self.subdomain(account,token)
                endpoint=f"https://{self.name}.{subdomain}.workers.dev"
                self.report("status","正在部署信令服务…")
                code, output=self.cli(work,["deploy","--name",self.name],150,account=account)
                if code:
                    details=" ".join(x.strip() for x in output.splitlines() if "[ERROR]" in x)[:240]
                    raise OSError("Cloudflare 部署失败" + ("：" + details if details else "，请检查账号权限"))
                self.report("status","正在验证部署地址…")
                for _ in range(12):
                    if self.stopping.is_set():
                        raise InterruptedError
                    try:
                        with OPENER.open(endpoint+"/health",timeout=5) as response:
                            if response.read(80)==b"campus-remote-signal-v1":
                                self.report("ready",endpoint)
                                return
                    except (OSError, ValueError):
                        pass
                    self.stopping.wait(2)
                raise OSError("服务已发布，但本机暂时打不开验证地址：" + endpoint)
        except InterruptedError:
            pass
        except (OSError,ValueError,TypeError) as error:
            if not self.stopping.is_set():
                self.report("error",str(error))
        finally:
            self.report("closed",None)

