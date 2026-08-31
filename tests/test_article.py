import pytest

from app.article import (
    ArticleValidationError,
    normalize_article_content,
    normalize_article_image_url,
    validate_article_content,
)


def test_article_content_accepts_wechat_images() -> None:
    result = validate_article_content(
        '<section><img src="https://mmbiz.qpic.cn/example/photo.jpg"></section>'
    )
    assert result["images"] == 1


@pytest.mark.parametrize(
    "content",
    [
        '<img src="https://example.com/photo.jpg">',
        '<img src="file:///tmp/photo.jpg">',
        '<section onclick="alert(1)">text</section>',
        "<script>alert(1)</script>",
    ],
)
def test_article_content_rejects_unsafe_markup(content: str) -> None:
    with pytest.raises(ArticleValidationError):
        validate_article_content(content)


def test_normalizes_wechat_article_image_alias_without_touching_other_urls() -> None:
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        '<img src="http://mmecoa.qpic.cn/mmecoa_jpg/example/0?from=appmsg">'
    )

    normalized = normalize_article_content(source)

    assert normalized == (
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        '<img src="https://mmbiz.qpic.cn/mmecoa_jpg/example/0?from=appmsg">'
    )
    assert validate_article_content(source)["images"] == 1


def test_normalize_article_image_url_preserves_path_and_query() -> None:
    assert normalize_article_image_url(
        "https://mmecoa.qpic.cn/sz_mmecoa_jpg/example/0?wx_fmt=jpeg"
    ) == "https://mmbiz.qpic.cn/sz_mmecoa_jpg/example/0?wx_fmt=jpeg"
