"""Compact agent observations, independent of presentation stream settings."""
import io

MAX_EDGE = 1280
MAX_BYTES = 256 * 1024


def dimensions(width, height):
    ratio = min(1, MAX_EDGE / width, MAX_EDGE / height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))


def encode(image):
    from PIL import Image
    size = dimensions(*image.size)
    image = image.convert('RGB')
    if image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    # Keep geometry stable even for complex/noisy screens: change compression,
    # never coordinates. The byte budget applies before base64 (~342 KiB after).
    for quality in (70, 60, 50, 40, 30, 20, 10, 5, 1):
        buf = io.BytesIO()
        image.save(buf, format='JPEG', quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_BYTES:
            return {'width': size[0], 'height': size[1], 'jpeg': data}
    raise ValueError('Screenshot exceeds the agent image budget')
