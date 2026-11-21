"""Download stable Campus Remote releases and replace the Windows executable after exit."""
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "0.2.0"
REPO = "Drinkwater10086/campus-remote"
RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "CampusRemote.exe"
MAX_ASSET = 128 * 1024 * 1024
TRUSTED_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com",
                 "objects.githubusercontent.com"}
VERSION_PATTERN = re.compile(r"v?(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})")


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.netloc.lower() not in TRUSTED_HOSTS:
            raise ValueError("更新链接跳转到了未知站点")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get(url):
    request = Request(url, headers={"User-Agent": f"CampusRemote/{VERSION}"})
    return build_opener(GitHubRedirect()).open(request, timeout=12)


@dataclass(frozen=True)
class Update:
    version: str
    url: str
    size: int
    digest: str


def release_update(data, current=VERSION):
    if not isinstance(data, dict):
        raise ValueError("更新服务器返回了无效数据")
    tag = data.get("tag_name")
    match = VERSION_PATTERN.fullmatch(tag) if isinstance(tag, str) else None
    if not match or data.get("draft") or data.get("prerelease"):
        return None
    if tuple(map(int, match.groups())) <= tuple(map(int, current.split("."))):
        return None
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("新版缺少发行附件")
    url = f"https://github.com/{REPO}/releases/download/{tag}/{ASSET}"
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != ASSET:
            continue
        digest, size = asset.get("digest"), asset.get("size")
        if (asset.get("state") == "uploaded" and asset.get("browser_download_url") == url
                and isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                and type(size) is int and 0 < size <= MAX_ASSET):
            return Update(tag, url, size, digest[7:])
    raise ValueError("新版缺少有效的 Windows 附件或 SHA-256")


def latest_update():
    with get(RELEASES) as response:
        data = response.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError("更新服务器返回的数据过大")
    return release_update(json.loads(data))


def download(update, target):
    stage = Path(tempfile.mkdtemp(prefix=".campus-update-", dir=target.parent))
    candidate = stage / ASSET
    try:
        digest = hashlib.sha256()
        total = 0
        with get(update.url) as response, candidate.open("xb") as output:
            while chunk := response.read(256 * 1024):
                if not total and not chunk.startswith(b"MZ"):
                    raise ValueError("下载内容不是 Windows 程序")
                total += len(chunk)
                if total > update.size:
                    raise ValueError("下载文件超过发行版声明的大小")
                digest.update(chunk)
                output.write(chunk)
        if total != update.size or digest.hexdigest() != update.digest:
            raise ValueError("下载校验失败，请重新检查更新")
        return candidate
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


# Values are passed as environment data so file paths never become PowerShell code.
APPLY_UPDATE = r"""
$ErrorActionPreference = 'Stop'
$target = $env:CAMPUS_UPDATE_TARGET
$stage = $env:CAMPUS_UPDATE_STAGE
$candidate = [IO.Path]::Combine($stage, 'CampusRemote.exe')
$backup = [IO.Path]::Combine($stage, 'previous.exe')
$ready = [IO.Path]::Combine($stage, 'ready')
$mode = $env:CAMPUS_UPDATE_MODE
$oldPid = [int]$env:CAMPUS_UPDATE_PID
$restarted = $false
$replaced = $false
function Replace-WithRetry($source, $destination, $previous) {
    $until = [DateTime]::UtcNow.AddSeconds(25)
    while ($true) {
        try { [IO.File]::Replace($source, $destination, $previous); return }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $until) { throw }
            [Threading.Thread]::Sleep(250)
        }
    }
}
function Launch($withMarker) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $target
    $start.Arguments = $mode
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
    $start.UseShellExecute = $false
    $start.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if ($withMarker) { $start.EnvironmentVariables['CAMPUS_UPDATE_READY'] = $ready }
    return [Diagnostics.Process]::Start($start)
}
try {
    $stream = [IO.File]::OpenRead($candidate)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
    if ($digest -ine $env:CAMPUS_UPDATE_DIGEST) {
        throw 'Update checksum changed'
    }
    $old = $null
    try { $old = [Diagnostics.Process]::GetProcessById($oldPid) } catch [ArgumentException] {}
    if ($old -and -not $old.WaitForExit(25000)) { throw 'The current application did not exit' }
    Replace-WithRetry $candidate $target $backup
    $replaced = $true
    $app = Launch $true
    $restarted = $true
    $until = [DateTime]::UtcNow.AddSeconds(35)
    while (-not [IO.File]::Exists($ready)) {
        if ($app.HasExited) { throw 'Updated executable exited before opening its window' }
        if ([DateTime]::UtcNow -ge $until) { exit 0 }
        [Threading.Thread]::Sleep(200)
    }
    [IO.File]::Delete($backup)
    [IO.File]::Delete($ready)
    [IO.Directory]::Delete($stage)
} catch {
    try {
        if ($replaced -and (-not $restarted -or $app.HasExited)) {
            Replace-WithRetry $backup $target ([IO.Path]::Combine($stage, 'failed.exe'))
            $null = Launch $false
        }
    } finally {
        $null = [Reflection.Assembly]::Load('System.Windows.Forms, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
        $null = [Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Campus Remote Update')
    }
    exit 1
}
"""


def apply_update(candidate, target, update, mode):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ, CAMPUS_UPDATE_TARGET=str(target), CAMPUS_UPDATE_STAGE=str(candidate.parent),
               CAMPUS_UPDATE_DIGEST=update.digest, CAMPUS_UPDATE_MODE=mode,
               CAMPUS_UPDATE_PID=str(os.getpid()))
    command = base64.b64encode(APPLY_UPDATE.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        env=env, cwd=target.parent, creationflags=subprocess.CREATE_NO_WINDOW,
    )


class AutoUpdater:
    def __init__(self, root):
        import tkinter as tk

        self.root = root
        self.menu = tk.Menu(root, tearoff=False)
        bar = tk.Menu(root)
        bar.add_cascade(label="更新", menu=self.menu)
        root.configure(menu=bar)
        self.menu.add_command(label=f"当前版本 {VERSION}", state="disabled")
        self.menu.add_command(label="检查更新", command=self.check)
        self.events = queue.SimpleQueue()
        self.busy = False
        self.closed = False
        self.applying = False
        self.is_active = lambda: False
        self.mode = ""
        self.ready = None
        self.target = Path(sys.executable).resolve()
        root.after(1000, lambda: self.check(manual=False))
        root.after(250, self.poll)
        root.bind("<Destroy>", self.on_destroy, add="+")

    def on_destroy(self, event):
        if event.widget == self.root:
            self.closed = True
            if self.ready and not self.applying:
                shutil.rmtree(self.ready[1].parent, ignore_errors=True)

    def check(self, manual=True):
        from tkinter import messagebox

        if not getattr(sys, "frozen", False):
            if manual:
                messagebox.showinfo("检查更新", "从源码运行时，请通过 Git 更新。", parent=self.root)
            return
        if self.ready:
            if manual:
                self.offer()
            return
        if self.busy:
            return
        self.busy = True
        self.menu.entryconfigure(1, label="检查并下载更新中…")
        threading.Thread(target=self.fetch, args=(manual,), daemon=True).start()

    def fetch(self, manual):
        try:
            update = latest_update()
            value = (update, download(update, self.target)) if update else None
            self.events.put((manual, value, None))
        except (OSError, ValueError) as error:
            self.events.put((manual, None, str(error)))

    def poll(self):
        from tkinter import messagebox

        try:
            manual, value, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.ready = value
            label = f"安装 {value[0].version}" if value else "检查更新"
            self.menu.entryconfigure(1, label=label)
            if value and (manual or not self.is_active()):
                self.offer()
            elif error and manual:
                messagebox.showerror("检查更新", error, parent=self.root)
            elif manual:
                messagebox.showinfo("检查更新", "已是最新版本。", parent=self.root)
        if not self.closed:
            self.root.after(250, self.poll)

    def offer(self):
        from tkinter import messagebox

        update, candidate = self.ready
        if self.is_active():
            messagebox.showinfo("版本更新", "新版已下载。请先停止共享或断开远控，再从「更新」菜单安装。",
                                parent=self.root)
            return
        if not messagebox.askyesno("版本更新", f"{update.version} 已通过校验。现在重启并安装？",
                                   parent=self.root):
            return
        try:
            apply_update(candidate, self.target, update, self.mode)
        except OSError as error:
            messagebox.showerror("版本更新", str(error), parent=self.root)
            return
        self.applying = True
        close = self.root.protocol("WM_DELETE_WINDOW")
        if close:
            self.root.tk.call(close)
        else:
            self.root.destroy()


def mark_ready():
    marker = os.environ.pop("CAMPUS_UPDATE_READY", "")
    if marker:
        path = Path(marker)
        target = Path(sys.executable).resolve()
        if (path.name == "ready" and path.parent.name.startswith(".campus-update-")
                and path.parent.parent == target.parent):
            path.touch(exist_ok=True)
