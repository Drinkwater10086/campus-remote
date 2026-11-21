"""Create a free, image-backed Render signaling service without a Git repository."""
import json
from pathlib import Path
import queue
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from rendezvous import OPENER

API = "https://api.render.com/v1"
IMAGE = "docker.io/library/node:22-alpine"
ASSETS = Path(__file__).resolve().parent / "render_signal"


def request(path,key,payload=None):
    body = None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    req=Request(API+path,data=body,method="GET" if body is None else "POST",
                headers={"Authorization":"Bearer "+key,"Content-Type":"application/json",
                         "Accept":"application/json"})
    try:
        with OPENER.open(req,timeout=15) as response:
            return json.loads(response.read(600000))
    except HTTPError as error:
        if error.code in (401,403):
            raise OSError(f"Render 拒绝访问（HTTP {error.code}）。请检查 API Key 和服务创建权限。") from error
        if error.code==400:
            raise OSError("Render 未接受免费服务的创建配置。请检查账号状态和免费服务额度。") from error
        raise OSError(f"Render API 返回 HTTP {error.code}。") from error


def config(owner,name):
    return {
        "type":"web_service","ownerId":owner,"name":name,
        "autoDeployTrigger":"off",
        "image":{"ownerId":owner,"imagePath":IMAGE},
        "secretFiles":[{"name":f,"content":(ASSETS/f).read_text(encoding="utf-8-sig")}
                       for f in ("server.mjs","worker.mjs")],
        "serviceDetails":{
            "runtime":"image","plan":"free","region":"singapore",
            "healthCheckPath":"/health","numInstances":1,
            "envSpecificDetails":{"dockerCommand":"node /etc/secrets/server.mjs"}
        }
    }


def endpoint(service):
    url = service.get("serviceDetails",{}).get("url")
    if not isinstance(url,str):
        return None
    try:
        p=urlsplit(url)
        if (p.scheme=="https" and p.netloc==p.hostname and p.hostname
                and p.hostname.endswith(".onrender.com") and not(p.query or p.fragment)
                and p.path in ("","/")):
            return url.rstrip("/")
    except ValueError:
        pass
    return None


class RenderSetup:
    def __init__(self,device,key):
        if not re.fullmatch("[0-9A-F]{12}",device):
            raise ValueError("本机设备号格式无效")
        self.key=key.strip()
        if not self.key or len(self.key)>256 or not self.key.isascii() or any(ch.isspace() for ch in self.key):
            raise ValueError("请输入 Render API Key")
        self.name="linkdesk-remote-"+device.lower()
        self.events=queue.SimpleQueue()
        self.choice=queue.Queue(1)
        self.stopping=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()

    def choose(self,owner):
        try:
            self.choice.put_nowait(owner)
        except queue.Full:
            pass

    def report(self,kind,value):
        self.events.put((kind,value))

    def owner(self):
        rows=request("/owners?limit=100",self.key)
        owners={}
        for row in rows:
            owner=row.get("owner") or {}
            oid=owner.get("id")
            if isinstance(oid,str) and re.fullmatch("[A-Za-z0-9-]{3,100}",oid):
                owners[oid]=owner.get("name") or oid
        if not owners:
            raise OSError("这个 API Key 没有可用的 Render 工作区。")
        if len(owners)==1:
            return next(iter(owners))
        self.report("accounts",list(owners.items())[:50])
        while not self.stopping.is_set():
            try:
                chosen=self.choice.get(timeout=.3)
                if chosen in owners:
                    return chosen
            except queue.Empty:
                pass
        raise InterruptedError

    def run(self):
        url=None
        try:
            self.report("status","正在检查 Render 工作区…")
            owner=self.owner()
            query=urlencode({"ownerId":owner,"name":self.name,"limit":20})
            existing=request("/services?"+query,self.key)
            service=next((row["service"] for row in existing if row.get("service",{}).get("name")==self.name),None)
            if self.stopping.is_set():
                raise InterruptedError
            if service is None:
                self.report("status","正在创建免费 Render 信令服务…")
                response=request("/services",self.key,config(owner,self.name))
                service=response.get("service") or response
            else:
                self.report("status","已找到同名服务，检查现有地址…")
            sid=service.get("id")
            if not isinstance(sid,str) or not re.fullmatch("srv-[A-Za-z0-9-]+",sid):
                raise OSError("Render 未返回有效的服务 ID，请在控制台查看部署。")
            url=endpoint(service)
            self.report("status","正在等待部署并验证信令，首次启动可能需要几分钟…")
            deadline=time.monotonic()+185
            while not self.stopping.is_set() and time.monotonic()<deadline:
                if not url:
                    url=endpoint(request("/services/"+sid,self.key))
                if url:
                    try:
                        with OPENER.open(url+"/health",timeout=8) as response:
                            if response.read(80)==b"campus-remote-signal-v1":
                                self.report("ready",url)
                                return
                    except (OSError,ValueError):
                        pass
                self.stopping.wait(4)
            if not self.stopping.is_set():
                raise OSError("Render 服务已创建，但健康检查尚未通过。请查看 Render 的 Deploys 页面，再点部署重试验证。"
                              + (f" 待验证地址：{url}" if url else ""))
        except InterruptedError:
            pass
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
            if not self.stopping.is_set():
                self.report("error",str(error))
        finally:
            self.key=""
            self.report("closed",None)

