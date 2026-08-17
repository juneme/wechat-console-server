from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

PERMANENT_FORMATS = {"BMP", "PNG", "JPEG", "GIF"}
MAX_IMAGE_PIXELS = 40_000_000


class ImageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int
    content_type: str


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    filename: str
    content_type: str
    width: int
    height: int


def inspect_image(data: bytes) -> ImageInfo:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImageValidationError("图片像素数量过大，已拒绝处理") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("文件不是可识别的图片，或图片已经损坏") from exc

    if image_format not in PERMANENT_FORMATS:
        raise ImageValidationError("仅支持 BMP、PNG、JPEG/JPG、GIF 图片")
    if width < 1 or height < 1:
        raise ImageValidationError("图片尺寸无效")
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageValidationError(
            f"图片像素数量超过上限 {MAX_IMAGE_PIXELS:,}，请先缩小图片"
        )
    content_type = {
        "BMP": "image/bmp",
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "GIF": "image/gif",
    }[image_format]
    return ImageInfo(image_format, width, height, content_type)


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _save_jpeg(image: Image.Image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
        subsampling="4:2:0",
    )
    return output.getvalue()


def _save_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _open_normalized(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            normalized = ImageOps.exif_transpose(opened)
            if normalized.mode in {"RGBA", "LA"} or (
                normalized.mode == "P" and "transparency" in normalized.info
            ):
                return normalized.convert("RGBA")
            return normalized.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("无法处理该图片") from exc


def _compress_as_jpeg(
    image: Image.Image, safe_name: str, *, max_bytes: int
) -> PreparedImage:
    image = _flatten_to_rgb(image)
    for _ in range(9):
        low, high = 38, 92
        best: bytes | None = None
        while low <= high:
            quality = (low + high) // 2
            candidate = _save_jpeg(image, quality)
            if len(candidate) < max_bytes:
                best = candidate
                low = quality + 1
            else:
                high = quality - 1
        if best is not None:
            return PreparedImage(
                data=best,
                filename=f"{safe_name}.jpg",
                content_type="image/jpeg",
                width=image.width,
                height=image.height,
            )
        next_size = (max(int(image.width * 0.82), 1), max(int(image.height * 0.82), 1))
        image = image.resize(next_size, Image.Resampling.LANCZOS)
    raise ImageValidationError("图片压缩后仍超过设定的大小限制")


def prepare_article_image(
    data: bytes,
    filename: str,
    *,
    max_bytes: int = 1_000_000,
    max_dimension: int = 2000,
) -> PreparedImage:
    info = inspect_image(data)
    safe_name = Path(filename).stem or "image"
    image = _open_normalized(data)
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    if info.format == "PNG":
        candidate = _save_png(image)
        if len(candidate) < max_bytes:
            return PreparedImage(
                data=candidate,
                filename=f"{safe_name}.png",
                content_type="image/png",
                width=image.width,
                height=image.height,
            )
    return _compress_as_jpeg(image, safe_name, max_bytes=max_bytes)


def _prepare_gif(
    data: bytes,
    safe_name: str,
    *,
    max_bytes: int,
    max_dimension: int,
) -> PreparedImage:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            loop = int(opened.info.get("loop", 0))
            default_duration = int(opened.info.get("duration", 100))
            frames: list[Image.Image] = []
            durations: list[int] = []
            for frame in ImageSequence.Iterator(opened):
                rgba = frame.convert("RGBA")
                rgba.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                frames.append(rgba)
                durations.append(int(frame.info.get("duration", default_duration)))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("无法处理该 GIF 图片") from exc

    if not frames:
        raise ImageValidationError("GIF 图片不包含有效画面")
    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        optimize=True,
        loop=loop,
        duration=durations,
        disposal=2,
    )
    prepared = output.getvalue()
    if len(prepared) >= max_bytes:
        raise ImageValidationError(
            "GIF 压缩后仍超过临时托管限制，请先压缩后重试，以免动画丢失"
        )
    return PreparedImage(
        data=prepared,
        filename=f"{safe_name}.gif",
        content_type="image/gif",
        width=frames[0].width,
        height=frames[0].height,
    )


def prepare_temporary_image(
    data: bytes,
    filename: str,
    *,
    max_bytes: int = 1_000_000,
    max_dimension: int = 2000,
) -> PreparedImage:
    info = inspect_image(data)
    safe_name = Path(filename).stem or "image"
    if info.format == "GIF":
        return _prepare_gif(
            data,
            safe_name,
            max_bytes=max_bytes,
            max_dimension=max_dimension,
        )
    return prepare_article_image(
        data,
        filename,
        max_bytes=max_bytes,
        max_dimension=max_dimension,
    )
