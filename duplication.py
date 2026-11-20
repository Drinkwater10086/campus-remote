import re
import time

import av


class DesktopDuplication:
    def __init__(self, target_fps=60):
        import dxcam
        self.camera = None
        self.target_fps = target_fps
        self.cached_frame = None
        self.cached_ticks = None
        primary = re.search(r"Device\[(\d+)\] Output\[(\d+)\]:[^\n]*Primary:True",
                            dxcam.output_info())
        if not primary:
            raise OSError("DXGI 未找到主屏幕")
        device, output = map(int, primary.groups())
        try:
            self.camera = dxcam.create(device_idx=device, output_idx=output,
                                       output_color="BGRA", max_buffer_len=2)
            # The host paces output, and grab() reuses the latest buffered frame.
            self.camera.start(target_fps=self.target_fps, video_mode=False)
        except Exception:
            self.close()
            raise

    def frame(self, source_size):
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if not self.camera.is_capturing:
                break
            ticks = self.camera.latest_frame_ticks
            if self.cached_frame is not None:
                if ticks == self.cached_ticks and (self.cached_frame.width, self.cached_frame.height) == source_size:
                    return self.cached_frame
                self.cached_frame = None
            pixels = self.camera.grab()
            if pixels is not None and (pixels.shape[1], pixels.shape[0]) == source_size:
                self.cached_frame = av.VideoFrame.from_numpy_buffer(pixels, format="bgra")
                self.cached_ticks = ticks
                return self.cached_frame
            time.sleep(0.002)
        raise OSError("DXGI 无法获取当前主屏幕画面")

    def close(self):
        self.cached_frame = None
        if self.camera is not None:
            self.camera.release()
            self.camera = None

