"""Forward physical key events only while the remote picture owns focus."""
import ctypes as C
from ctypes import wintypes as W
import queue
import threading

from desktop import user

LRESULT = C.c_ssize_t
WH_KEYBOARD_LL = 13
WM_QUIT = 0x12
VK_F12 = 0x7B
KEY_DOWN = (0x100, 0x104)
KEY_UP = (0x101, 0x105)
LLKHF_INJECTED = 0x10


class KeyboardInfo(C.Structure):
    _fields_ = [("vk", W.DWORD), ("scan", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", C.c_size_t)]


CALLBACK = C.WINFUNCTYPE(LRESULT, C.c_int, W.WPARAM, W.LPARAM)
user.SetWindowsHookExW.argtypes = (C.c_int,CALLBACK,W.HMODULE,W.DWORD)
user.SetWindowsHookExW.restype = W.HANDLE
user.UnhookWindowsHookEx.argtypes = (W.HANDLE,)
user.UnhookWindowsHookEx.restype = W.BOOL
user.CallNextHookEx.argtypes = (W.HANDLE,C.c_int,W.WPARAM,W.LPARAM)
user.CallNextHookEx.restype = LRESULT
user.GetMessageW.argtypes = (C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT)
user.GetMessageW.restype = C.c_int
user.PeekMessageW.argtypes = (C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT,W.UINT)
user.PeekMessageW.restype = W.BOOL
user.PostThreadMessageW.argtypes = (W.DWORD,W.UINT,W.WPARAM,W.LPARAM)
user.PostThreadMessageW.restype = W.BOOL
user.GetAsyncKeyState.argtypes = (C.c_int,)
user.GetAsyncKeyState.restype = C.c_short
kernel = C.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentThreadId.restype = W.DWORD
kernel.GetModuleHandleW.argtypes = (W.LPCWSTR,)
kernel.GetModuleHandleW.restype = W.HMODULE


class KeyCapture:
    def __init__(self):
        self.target = None
        self.events = queue.SimpleQueue()
        self.held = {}
        self.local = set()
        self.failure = None
        self.thread_id = None
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def target_window(self, hwnd, generation):
        self.target = (hwnd, generation) if hwnd else None
        if hwnd and self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def close(self):
        self.target = None
        self.closed.set()
        if self.thread_id is not None:
            user.PostThreadMessageW(self.thread_id,WM_QUIT,0,0)

    def filter_key(self, foreground, vk, message, flags):
        target = self.target
        if flags & LLKHF_INJECTED or not 8 <= vk <= 254:
            return False
        if message in KEY_UP:
            if vk in self.held:
                generation = self.held.pop(vk)
                if target == (foreground,generation) and vk != VK_F12:
                    self.events.put((generation,vk,False,bool(flags & 1)))
                return True
            self.local.discard(vk)
            return False
        if message not in KEY_DOWN:
            return False
        generation = self.held.get(vk)
        if generation is not None:
            if target == (foreground,generation) and vk != VK_F12:
                self.events.put((generation,vk,True,bool(flags & 1)))
            return True
        if vk in self.local or not target or target[0] != foreground:
            self.local.add(vk)
            return False
        generation = target[1]
        self.held[vk] = generation
        self.events.put((generation,vk,True,bool(flags & 1)))
        return True

    def run(self):
        @CALLBACK
        def callback(code,message,pointer):
            if code == 0:
                info = C.cast(pointer, C.POINTER(KeyboardInfo)).contents
                if self.filter_key(user.GetForegroundWindow(),info.vk,message,info.flags):
                    return 1
            return user.CallNextHookEx(None,code,message,pointer)

        msg=W.MSG()
        user.PeekMessageW(C.byref(msg),None,0,0,0)
        self.thread_id=kernel.GetCurrentThreadId()
        if self.closed.is_set():
            return
        self.local = {vk for vk in range(8,255) if user.GetAsyncKeyState(vk)&0x8000}
        handle=user.SetWindowsHookExW(WH_KEYBOARD_LL,callback,kernel.GetModuleHandleW(None),0)
        if not handle:
            self.failure=C.get_last_error()
        self.ready.set()
        if not handle:
            return
        try:
            while user.GetMessageW(C.byref(msg),None,0,0)>0:
                pass
        finally:
            user.UnhookWindowsHookEx(handle)


