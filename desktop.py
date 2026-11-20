import ctypes as C
from ctypes import wintypes as W

user = C.WinDLL("user32", use_last_error=True)
ULONG_PTR = C.c_size_t


class Mouse(C.Structure):
    _fields_ = [("x", W.LONG), ("y", W.LONG), ("data", W.DWORD),
                ("flags", W.DWORD), ("time", W.DWORD), ("extra", ULONG_PTR)]


class Key(C.Structure):
    _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", ULONG_PTR)]


class Payload(C.Union):
    _fields_ = [("mouse", Mouse), ("key", Key)]


class Input(C.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("kind", W.DWORD), ("payload", Payload)]


user.SendInput.argtypes = W.UINT, C.POINTER(Input), C.c_int
user.SendInput.restype = W.UINT
user.SetCursorPos.argtypes = C.c_int, C.c_int
user.SetCursorPos.restype = W.BOOL
BUTTONS = {1: (2, 4), 2: (32, 64), 3: (8, 16)}
user.MapVirtualKeyW.argtypes = W.UINT, W.UINT
user.MapVirtualKeyW.restype = W.UINT
user.GetAncestor.argtypes = W.HWND, W.UINT
user.GetAncestor.restype = W.HWND
user.GetForegroundWindow.restype = W.HWND
user.WindowFromPoint.argtypes = (W.POINT,)
user.WindowFromPoint.restype = W.HWND
user.GetCursorPos.argtypes = (C.POINTER(W.POINT),)
user.GetCursorPos.restype = W.BOOL


def root_window(hwnd):
    return user.GetAncestor(hwnd, 2) if hwnd else None


def inject(event):
    if user.SendInput(1, C.byref(event), C.sizeof(event)) != 1:
        raise OSError("Windows 拒绝了输入")


NAVIGATION = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})


def keyboard(vk, down, extended=None):
    event = Input(kind=1)
    scan = user.MapVirtualKeyW(vk, 4)
    flags = 0 if down else 2
    if extended is None:
        extended = vk in NAVIGATION or scan >> 8 == 0xE0
    if scan and scan >> 8 != 0xE1:
        event.key = Key(0, scan & 0xff, flags | 8 | int(extended), 0, 0)
    else:
        event.key = Key(vk, 0, flags, 0, 0)
    inject(event)


def type_text(text):
    if not isinstance(text, str) or not 0 < len(text) <= 128 or not text.isprintable():
        return
    try:
        data = text.encode("utf-16-le")
    except UnicodeEncodeError:
        return
    events = (Input * len(data))()
    for offset in range(0, len(data), 2):
        code = int.from_bytes(data[offset:offset + 2], "little")
        for index, flags in ((offset, 4), (offset + 1, 6)):
            events[index].kind = 1
            events[index].key = Key(0, code, flags, 0, 0)
    if user.SendInput(len(events), events, C.sizeof(Input)) != len(events):
        raise OSError("Windows 拒绝了文字输入")


def mouse(flags, data=0):
    event = Input(kind=0)
    event.mouse = Mouse(0, 0, data & 0xffffffff, flags, 0, 0)
    inject(event)


def dpi_aware():
    try:
        user.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except AttributeError:
        user.SetProcessDPIAware()


class DesktopInput:
    def __init__(self):
        self.keys = {}
        self.buttons = set()
        self.protected_window = None

    def protected(self, hwnd):
        return bool(self.protected_window and root_window(hwnd) == self.protected_window)

    def protected_pointer(self):
        position = W.POINT()
        return (user.GetCursorPos(C.byref(position))
                and self.protected(user.WindowFromPoint(position)))

    def apply(self, message):
        kind = message.get("op")
        if kind == "release":
            self.release()
        elif kind == "text":
            if not self.protected(user.GetForegroundWindow()):
                type_text(message.get("text"))
        elif kind == "move":
            x, y = message.get("x"), message.get("y")
            if type(x) is not int or type(y) is not int or not (0 <= x <= 10000 and 0 <= y <= 10000):
                return
            point = W.POINT(x * (user.GetSystemMetrics(0) - 1) // 10000,
                            y * (user.GetSystemMetrics(1) - 1) // 10000)
            if self.protected(user.WindowFromPoint(point)):
                return
            if not user.SetCursorPos(point.x, point.y):
                raise OSError("无法移动鼠标")
        elif kind in ("key", "button"):
            code, down = message.get("code"), message.get("down")
            if type(code) is not int or type(down) is not bool:
                return
            if kind == "key" and 8 <= code <= 254 and (down or code in self.keys):
                if down and self.protected(user.GetForegroundWindow()):
                    return
                extended = message.get("extended")
                if type(extended) is not bool:
                    extended = None
                if down:
                    keyboard(code, True, extended)
                    self.keys[code] = extended
                else:
                    keyboard(code, False, self.keys[code])
                    del self.keys[code]
            elif kind == "button" and code in BUTTONS and (down or code in self.buttons):
                if down and self.protected_pointer():
                    return
                mouse(BUTTONS[code][not down])
                (self.buttons.add if down else self.buttons.discard)(code)
        elif kind == "wheel" and type(message.get("delta")) is int:
            if self.protected_pointer():
                return
            mouse(0x800, max(-1200, min(1200, message["delta"])))

    def release(self):
        for code in tuple(self.keys):
            try:
                keyboard(code, False, self.keys[code])
            except OSError:
                continue
            del self.keys[code]
        for code in tuple(self.buttons):
            try:
                mouse(BUTTONS[code][1])
            except OSError:
                continue
            self.buttons.discard(code)


