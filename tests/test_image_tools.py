import io

from PIL import Image, PngImagePlugin

from app.image_tools import prepare_article_image, prepare_temporary_image


def test_article_image_strips_png_metadata() -> None:
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "sensitive-marker")
    Image.new("RGBA", (64, 32), (255, 0, 0, 128)).save(
        output, "PNG", pnginfo=metadata
    )

    prepared = prepare_article_image(output.getvalue(), "photo.png")

    assert prepared.content_type == "image/png"
    assert b"sensitive-marker" not in prepared.data


def test_temporary_gif_keeps_animation() -> None:
    frames = [Image.new("RGB", (24, 24), color) for color in ("red", "blue")]
    output = io.BytesIO()
    frames[0].save(
        output,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[80, 120],
        loop=0,
    )

    prepared = prepare_temporary_image(output.getvalue(), "animated.gif")

    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.n_frames == 2
    assert prepared.content_type == "image/gif"
