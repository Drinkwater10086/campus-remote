from fractions import Fraction
from statistics import median
from time import perf_counter

import av
from PIL import Image
from av.codec.context import Flags
from av.codec.hwaccel import HWAccel
from av.video.frame import PictureType
from av.video.reformatter import Interpolation, VideoReformatter
from quality import dimensions_ok

ENCODERS = {
    "h264_nvenc": ("NVENC", "yuv420p", 2),
    "h264_qsv": ("Intel QSV", "nv12", 3),
    "h264_amf": ("AMD AMF", "nv12", 8),
    "libx264": ("CPU", "yuv420p", 1),
}
ENCODER_NAMES = {marker: name for name, _, marker in ENCODERS.values()}
KEYFRAME = 4
DECODERS = (
    ("NVDEC", "h264", "cuda", {}),
    ("Intel QSV", "h264_qsv", "qsv", {"child_device_type": "d3d11va"}),
    ("D3D11VA", "h264", "d3d11va", {}),
    ("AMD D3D11VA", "h264", "d3d11va", {"vendor_id": "0x1002"}),
    ("Intel D3D11VA", "h264", "d3d11va", {"vendor_id": "0x8086"}),
    ("DXVA2", "h264", "dxva2", {}),
    ("CPU", "h264", None, {}),
)
CODEC_ERRORS = (av.FFmpegError, ValueError, NotImplementedError)


class Encoder:
    def __init__(self, size, settings, prefer_nvidia=True, *, codec_markers=True):
        if len(settings) == 4:
            self.width, self.height, fps, mbps = settings
            if not dimensions_ok(self.width, self.height, sending=True):
                raise ValueError("编码分辨率无效")
        else:
            width, height = size
            max_height, fps, mbps = settings
            scale = min(1, max_height / height, (max_height * 16 / 9) / width)
            self.width = max(2, int(width * scale) // 2 * 2)
            self.height = max(2, int(height * scale) // 2 * 2)
        self.fps = fps
        self.mbps = mbps
        self.reformatter = VideoReformatter()
        self.index = 0
        self.first_packet = True
        self.pending_frames = 0
        self.codec_markers = codec_markers
        self.backends = tuple(ENCODERS) if prefer_nvidia else ("libx264",)
        self.candidates = iter(self.backends)
        self.select_codec()

    @property
    def nvidia(self):
        return self.codec.name == "h264_nvenc"

    def select_codec(self):
        self.codec = None
        for backend in self.candidates:
            try:
                self.codec = self.open_codec(backend)
            except CODEC_ERRORS:
                if backend == "libx264":
                    raise
            else:
                self.first_packet = True
                self.pending_frames = 0
                self.codec_start = self.index
                return

    def open_codec(self, backend):
        codec = av.CodecContext.create(backend, "w")
        codec.width = self.width
        codec.height = self.height
        codec.pix_fmt = ENCODERS[backend][1]
        codec.time_base = Fraction(1, self.fps)
        codec.framerate = Fraction(self.fps)
        codec.gop_size = self.fps * 2
        codec.max_b_frames = 0
        rate = self.mbps * 1_000_000
        codec.bit_rate = rate
        if backend == "h264_nvenc":
            codec.options = {
                "preset": "p1", "tune": "ull", "rc": "vbr", "cq": "23",
                "rc-lookahead": "0", "zerolatency": "1", "delay": "0",
                "maxrate": str(rate), "bufsize": str(rate // 2),
            }
        elif backend == "h264_qsv":
            codec.options = {
                "preset": "veryfast", "async_depth": "1", "repeat_pps": "1",
                "forced_idr": "1", "maxrate": str(rate), "bufsize": str(rate // 2),
            }
        elif backend == "h264_amf":
            codec.flags |= Flags.low_delay
            codec.options = {
                "usage": "ultralowlatency", "quality": "speed", "rc": "cbr",
                "async_depth": "1", "preanalysis": "0", "forced_idr": "1",
                "header_spacing": str(codec.gop_size),
            }
        else:
            codec.bit_rate = 0
            codec.options = {
                "preset": "ultrafast" if self.fps >= 60 else "veryfast",
                "tune": "zerolatency",
                "crf": "23",
                "x264-params": (
                    f"vbv-maxrate={self.mbps * 1000}:vbv-bufsize={self.mbps * 500}:"
                    "repeat-headers=1:annexb=1"
                ),
            }
        codec.open()
        return codec

    def prepare_frame(self, image, codec):
        return self.reformatter.reformat(
            image, width=self.width, height=self.height, format=codec.pix_fmt,
            interpolation=Interpolation.FAST_BILINEAR,
        )

    def rank_hardware(self, image):
        if self.codec.name == "libx264":
            return
        working = [self.codec]
        for backend in self.backends[self.backends.index(self.codec.name) + 1:-1]:
            try:
                working.append(self.open_codec(backend))
            except CODEC_ERRORS:
                continue
        if len(working) == 1:
            return
        scores = []
        for codec in working:
            durations = []
            first_output = None
            try:
                for index in range(4):
                    start = perf_counter()
                    frame = self.prepare_frame(image, codec)
                    frame.pts = index
                    frame.pict_type = PictureType.I if index == 0 else PictureType.NONE
                    packets = codec.encode(frame)
                    durations.append(perf_counter() - start)
                    if packets and first_output is None:
                        if not packets[0].is_keyframe:
                            break
                        first_output = index
            except CODEC_ERRORS:
                continue
            if first_output is not None:
                # A buffered frame costs one full frame interval before it reaches the viewer.
                scores.append((median(durations) + first_output / self.fps, codec.name))
        ordered = tuple(name for _, name in sorted(scores))
        # Probe packets are private. Release these sessions before opening the live encoder.
        del codec
        working.clear()
        self.candidates = iter((*ordered, "libx264"))
        self.select_codec()

    def encode(self, image):
        for flags, packet in self.encode_packets(image):
            yield b"".join((bytes([flags]), packet))

    def encode_packets(self, image):
        if not isinstance(image, av.VideoFrame):
            if image.size != (self.width, self.height):
                image = image.resize((self.width, self.height), Image.Resampling.BILINEAR)
            image = av.VideoFrame.from_image(image)
        if self.index == 0:
            self.rank_hardware(image)
        pts = self.index
        self.index += 1
        while True:
            frame = self.prepare_frame(image, self.codec)
            frame.pts = pts
            if self.codec.name in ("h264_qsv", "h264_amf"):
                frame.pict_type = (PictureType.I if (pts - self.codec_start) % self.codec.gop_size == 0
                                   else PictureType.NONE)
            try:
                packets = self.codec.encode(frame)
                self.pending_frames = 0 if packets else self.pending_frames + 1
                if self.pending_frames >= 4 or (self.first_packet and packets and not packets[0].is_keyframe):
                    raise ValueError("编码器无法及时输出可解码画面")
                break
            except CODEC_ERRORS:
                if self.codec.name == "libx264":
                    raise
                self.select_codec()
                self.codec_start = pts
        marker = ENCODERS[self.codec.name][2]
        if not self.codec_markers and marker not in (1, 2):
            marker = 1
        for packet in packets:
            flags = marker if self.first_packet else 0
            yield flags | (KEYFRAME if packet.is_keyframe else 0), packet
            self.first_packet = False


class Decoder:
    def __init__(self, prefer_nvidia=True):
        self.codec = None
        self.backends = DECODERS if prefer_nvidia else DECODERS[-1:]
        self.candidates = self.backends
        self.backend = 0
        self.encoder_name = ""
        self.reformatter = VideoReformatter()
        self.wait_for_keyframe = False
        self.pending_packets = 0

    @property
    def decoder_name(self):
        return self.candidates[self.backend][0] if self.codec and self.codec.is_hwaccel else "CPU"

    def open_codec(self, candidate):
        _, name, device, options = candidate
        hardware = HWAccel(device, options=options, allow_software_fallback=False) if device else None
        codec = av.CodecContext.create(name, "r", hwaccel=hardware)
        codec.thread_count = 1
        if device == "qsv":
            codec.options = {"async_depth": "1"}
        return codec

    def rank_hardware(self, packet):
        remaining = list(self.candidates)
        ranked = []
        best = None
        # Compare the primary APIs. Vendor-specific DirectX devices and CPU are fallbacks.
        for candidate in self.candidates[:3]:
            if not candidate[2]:
                break
            remaining.pop(0)
            try:
                codec = self.open_codec(candidate)
                codec.open()
                start = perf_counter()
                frames = codec.decode(packet)
            except CODEC_ERRORS:
                continue
            elapsed = perf_counter() - start if frames else float("inf")
            ranked.append((elapsed, candidate))
            if best is None or elapsed < best[0] - 0.001:
                best = elapsed, candidate, codec, frames
        if best is None:
            self.candidates = tuple(remaining)
            return None
        _, selected, self.codec, frames = best
        remaining[:0] = [candidate for _, candidate in sorted(ranked) if candidate != selected]
        self.candidates = (selected, *remaining)
        return frames

    def decode(self, data, viewport=None):
        for frame in self.decode_frames(data):
            if viewport is None:
                yield frame.to_image()
            else:
                yield (frame.width, frame.height), self.preview(frame, viewport)

    def preview(self, frame, viewport):
        scale = min(1, viewport[0] / frame.width, viewport[1] / frame.height)
        rgb = self.reformatter.reformat(
            frame, width=max(1, int(frame.width * scale)),
            height=max(1, int(frame.height * scale)), format="rgb24",
            interpolation=Interpolation.FAST_BILINEAR,
        )
        plane = rgb.planes[0]
        # Pillow copies directly from the padded plane, avoiding PyAV's ndarray copy.
        return Image.frombytes("RGB", (rgb.width, rgb.height), memoryview(plane),
                               "raw", "RGB", plane.line_size, 1)

    def decode_frames(self, data):
        if len(data) < 2:
            raise ValueError('视频流格式无效')
        marker = data[0] & ~KEYFRAME
        keyframe = bool(data[0] & KEYFRAME)
        if marker and marker not in ENCODER_NAMES:
            raise ValueError("视频流格式无效")
        if marker:
            self.encoder_name = ENCODER_NAMES[marker]
            self.codec = None
            self.candidates = self.backends
            self.backend = 0
            self.pending_packets = 0
            self.wait_for_keyframe = True
        if self.wait_for_keyframe and not keyframe:
            return
        if not self.codec and not keyframe:
            raise ValueError("视频流缺少起始帧")
        self.wait_for_keyframe = False
        packet = av.Packet(memoryview(data)[1:])
        frames = self.rank_hardware(packet) if marker else None
        while True:
            try:
                if frames is None:
                    if self.codec is None:
                        self.codec = self.open_codec(self.candidates[self.backend])
                    frames = self.codec.decode(packet)
                self.pending_packets = 0 if frames else self.pending_packets + 1
                if self.pending_packets >= 8 and self.codec.is_hwaccel:
                    raise ValueError("硬件解码器未输出画面")
                break
            except CODEC_ERRORS:
                if self.backend + 1 == len(self.candidates):
                    raise
                self.backend += 1
                self.codec = None
                self.pending_packets = 0
                frames = None
                if not keyframe:
                    self.wait_for_keyframe = True
                    return
        for frame in frames:
            if not dimensions_ok(frame.width, frame.height):
                raise ValueError("画面尺寸无效")
            yield frame


