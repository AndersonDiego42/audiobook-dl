from io import BytesIO
from typing import Tuple

from PIL import Image

from audiobookdl import Cover


SUPPORTED_COVER_EXTENSIONS = {"jpg", "jpeg", "png"}


def normalize_cover_image(cover: Cover) -> Tuple[bytes, str]:
    """Return cover bytes and a lowercase extension ('jpg', 'jpeg', or 'png').

    Converts other formats (such as WebP) in memory using Pillow so ID3 and MP4
    cover tags always receive a standard supported image format.
    """
    ext = cover.extension.lower().lstrip(".")
    if ext in SUPPORTED_COVER_EXTENSIONS:
        return cover.image, ext
    with Image.open(BytesIO(cover.image)) as img:
        buf = BytesIO()
        if img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            img.save(buf, format="PNG")
            return buf.getvalue(), "png"
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG")
        return buf.getvalue(), "jpg"
