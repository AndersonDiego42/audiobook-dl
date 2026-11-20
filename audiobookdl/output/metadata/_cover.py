from io import BytesIO

from PIL import Image, UnidentifiedImageError

from audiobookdl import Cover, logging


SUPPORTED_COVER_EXTENSIONS = {"jpg", "jpeg", "png"}


def _save_pillow_cover(img: Image.Image) -> tuple[bytes, str]:
    buf = BytesIO()
    if img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        img.save(buf, format="PNG")
        return buf.getvalue(), "png"
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "jpg"


def normalize_cover_image(
    cover: Cover,
    *,
    square: bool = False,
    max_size: int | None = None,
) -> tuple[bytes, str]:
    """Return cover bytes and a lowercase extension ('jpg', 'jpeg', or 'png').

    Converts unsupported formats (such as WebP), optionally letterboxes to `1:1`
    when `square=True`, and downscales when `max_size` is provided.
    """
    ext = cover.extension.lower().lstrip(".")
    needs_transform = square or (max_size is not None and max_size > 0)
    if ext in SUPPORTED_COVER_EXTENSIONS and not needs_transform:
        return cover.image, ext

    try:
        with Image.open(BytesIO(cover.image)) as raw_img:
            img: Image.Image = raw_img.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logging.debug(f"Cover image could not be decoded ({ext}); using original bytes: {exc}")
        return cover.image, ext
    transformed = False
    if square and img.width != img.height:
        side = max(img.width, img.height)
        mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
        bg: tuple[int, ...] = (0, 0, 0, 255) if mode == "RGBA" else (0, 0, 0)
        canvas = Image.new(mode, (side, side), bg)
        if img.mode != mode:
            img = img.convert(mode)
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        img = canvas
        transformed = True
    if max_size is not None and max_size > 0 and max(img.width, img.height) > max_size:
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        img.thumbnail((max_size, max_size), resampling)
        transformed = True
    if not transformed and ext in SUPPORTED_COVER_EXTENSIONS:
        return cover.image, ext
    return _save_pillow_cover(img)


def apply_cover_postprocessing(
    cover: Cover | None,
    *,
    square: bool = False,
    max_size: int | None = None,
) -> Cover | None:
    """Return `Cover` normalized according to `square` / `max_size`, or `None`."""
    if cover is None:
        return None
    if not square and not (max_size is not None and max_size > 0):
        return cover
    image_data, ext = normalize_cover_image(cover, square=square, max_size=max_size)
    return Cover(image_data, ext)
