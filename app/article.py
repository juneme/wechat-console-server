from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_CONTENT_CHARACTERS = 20_000
MAX_CONTENT_BYTES = 1_000_000


class ArticleValidationError(ValueError):
    pass


class _ArticleInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self.image_urls: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "iframe", "object", "embed"}:
            self.errors.append(f"正文不允许使用 <{lowered}> 标签")
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.errors.append(f"正文不允许使用事件属性 {name}")
            if lowered == "img" and name.lower() == "src" and value:
                self.image_urls.append(value.strip())


def validate_article_content(content: str) -> dict[str, int]:
    character_count = len(content)
    byte_count = len(content.encode("utf-8"))
    if character_count >= MAX_CONTENT_CHARACTERS:
        raise ArticleValidationError(
            f"正文必须少于 {MAX_CONTENT_CHARACTERS} 字符，当前 {character_count} 字符"
        )
    if byte_count >= MAX_CONTENT_BYTES:
        raise ArticleValidationError(
            f"正文超过 {MAX_CONTENT_BYTES} 字节限制，当前 {byte_count} 字节"
        )

    inspector = _ArticleInspector()
    try:
        inspector.feed(content)
        inspector.close()
    except Exception as exc:
        raise ArticleValidationError("正文 HTML 无法解析") from exc
    if inspector.errors:
        raise ArticleValidationError("；".join(dict.fromkeys(inspector.errors)))

    for source in inspector.image_urls:
        parsed = urlparse(source)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ArticleValidationError(f"正文包含非 HTTPS 图片地址：{source[:120]}")
        hostname = (parsed.hostname or "").lower()
        if not (hostname == "mmbiz.qpic.cn" or hostname.endswith(".mmbiz.qpic.cn")):
            raise ArticleValidationError(
                f"正文图片必须使用微信正文图片接口返回的地址：{source[:120]}"
            )

    return {
        "characters": character_count,
        "bytes": byte_count,
        "images": len(inspector.image_urls),
    }
