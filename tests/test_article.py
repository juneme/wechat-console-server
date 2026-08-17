import pytest

from app.article import ArticleValidationError, validate_article_content


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
