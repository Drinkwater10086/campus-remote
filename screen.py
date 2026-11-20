"""Capture the primary Windows display into a reusable, scaled DIB."""
import ctypes as C
from ctypes import wintypes as W

import av
from desktop import user

gdi = C.WinDLL("gdi32", use_last_error=True)


def api(library, name, result, *args):
    function = getattr(library, name)
    function.argtypes = args
    function.restype = result
    return function


class BitmapHeader(C.Structure):
    _fields_ = [
        ("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
        ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
        ("image_size", W.DWORD), ("x_density", W.LONG), ("y_density", W.LONG),
        ("colors", W.DWORD), ("important", W.DWORD),
    ]


get_dc = api(user, "GetDC", W.HDC, W.HWND)
release_dc = api(user, "ReleaseDC", C.c_int, W.HWND, W.HDC)
make_dc = api(gdi, "CreateCompatibleDC", W.HDC, W.HDC)
delete_dc = api(gdi, "DeleteDC", W.BOOL, W.HDC)
delete_object = api(gdi, "DeleteObject", W.BOOL, W.HGDIOBJ)
select = api(gdi, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
make_dib = api(gdi, "CreateDIBSection", W.HBITMAP, W.HDC, C.POINTER(BitmapHeader),
               W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD)
stretch = api(gdi, "StretchBlt", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int,
              W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.DWORD)
set_mode = api(gdi, "SetStretchBltMode", C.c_int, W.HDC, C.c_int)
flush = api(gdi, "GdiFlush", W.BOOL)


def primary_size():
    return user.GetSystemMetrics(0), user.GetSystemMetrics(1)


class ScreenCapture:
    def __init__(self, size):
        self.width, self.height = size
        self.screen = self.memory = self.bitmap = self.original = None
        try:
            self.screen = get_dc(None)
            if not self.screen:
                raise OSError("无法打开 Windows 主屏幕")
            self.memory = make_dc(self.screen)
            if not self.memory:
                raise OSError("无法创建采集缓冲区")
            pixels = C.c_void_p()
            header = BitmapHeader(C.sizeof(BitmapHeader), self.width, -self.height, 1, 32)
            self.bitmap = make_dib(self.screen, C.byref(header), 0, C.byref(pixels), None, 0)
            if not self.bitmap or not pixels.value:
                raise OSError("无法分配画面缓冲区")
            self.original = select(self.memory, self.bitmap)
            if not self.original or self.original == C.c_void_p(-1).value:
                self.original = None
                raise OSError("无法选择画面缓冲区")
            set_mode(self.memory, 3)  # COLORONCOLOR
            self.pixels = (C.c_ubyte * (self.width * self.height * 4)).from_address(pixels.value)
        except Exception:
            self.close()
            raise

    def frame(self, source_size):
        if not stretch(self.memory, 0, 0, self.width, self.height,
                       self.screen, 0, 0, *source_size, 0x40CC0020):
            raise OSError("屏幕采集失败")
        if not flush():
            raise OSError("屏幕采集缓冲区同步失败")
        frame = av.VideoFrame(self.width, self.height, "bgra")
        plane = frame.planes[0]
        if plane.line_size == self.width * 4:
            plane.update(memoryview(self.pixels))
        else:
            source = memoryview(self.pixels).cast("B")
            row = self.width * 4
            padded = bytearray(plane.buffer_size)
            for index in range(self.height):
                start = index * plane.line_size
                padded[start:start + row] = source[index * row:(index + 1) * row]
            plane.update(padded)
        return frame

    def close(self):
        if self.original:
            select(self.memory, self.original)
        if self.bitmap:
            delete_object(self.bitmap)
        if self.memory:
            delete_dc(self.memory)
        if self.screen:
            release_dc(None, self.screen)
        self.screen = self.memory = self.bitmap = self.original = None

