"""Shared resolution, frame rate and bitrate limits."""
PRESETS = {
    "360p": (640, 360), "480p": (854, 480), "720p": (1280, 720),
    "1080p": (1920, 1080), "2K · 1440p": (2560, 1440), "4K · 2160p": (3840, 2160),
}
RATES = (10, 15, 24, 30, 60, 120, 144, 165, 240)
BITRATES = (1, 2, 4, 6, 8, 12, 16, 25, 40, 60)
DEFAULT = (1920, 1080, 60, 8)
MAX_PIXELS = 4096 * 2160


def dimensions_ok(width, height, *, sending=False):
    return (type(width) is int and type(height) is int
            and (320 if sending else 2) <= width <= 4096
            and (180 if sending else 2) <= height <= 4096
            and width * height <= MAX_PIXELS
            and width % 2 == height % 2 == 0)


def settings(message):
    height, fps, mbps = (message.get(k) for k in ("height", "fps", "mbps"))
    width = message.get("width")
    if any(type(v) is not int for v in (height, fps, mbps)):
        raise ValueError("画质参数无效")
    if fps not in RATES or mbps not in BITRATES:
        raise ValueError("帧率或码率超出范围")
    if "width" not in message:
        if height not in (360, 480, 720, 1080):
            raise ValueError("旧版分辨率无效")
        return height, fps, mbps
    if not dimensions_ok(width, height, sending=True):
        raise ValueError("分辨率超出范围或宽高为奇数")
    return width, height, fps, mbps

