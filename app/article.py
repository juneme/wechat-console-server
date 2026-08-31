from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse, urlsplit, urlunsplit

MAX_CONTENT_CHARACTERS = 20_000
MAX_CONTENT_BYTES = 1_000_000
CANONICAL_WECHAT_IMAGE_HOST = "mmbiz.qpic.cn"
WECHAT_IMAGE_HOST_ALIASES = {
    CANONICAL_WECHAT_IMAGE_HOST,
    "mmecoa.qpic.cn",
}

_IMAGE_TAG_PATTERN = re.compile(
    r"<img\b(?:[^>'\"]|'[^']*'|\"[^\"]*\")*>", re.IGNORECASE
)
_SOURCE_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<prefix>\bsrc\s*=\s*)"
    r"(?:(?P<quote>['\"])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)


class ArticleValidationError(ValueError):
    pass


def normalize_article_image_url(source: str) -> str:
    """Return the canonical HTTPS WeChat CDN URL for known upload hosts."""
    parsed = urlsplit(source.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in WECHAT_IMAGE_HOST_ALIASES:
        return source.strip()
    return urlunsplit(
        (
            "https",
            CANONICAL_WECHAT_IMAGE_HOST,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def normalize_article_content(content: str) -> str:
    """Canonicalize only image src attributes while preserving the HTML body."""

    def normalize_tag(tag_match: re.Match[str]) -> str:
        tag = tag_match.group(0)

        def normalize_source(source_match: re.Match[str]) -> str:
            quote = source_match.group("quote") or ""
            source = source_match.group("quoted") or source_match.group("bare") or ""
            normalized = normalize_article_image_url(source)
            return f"{source_match.group('prefix')}{quote}{normalized}{quote}"

        return _SOURCE_ATTRIBUTE_PATTERN.sub(normalize_source, tag, count=1)

    return _IMAGE_TAG_PATTERN.sub(normalize_tag, content)


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
        inspector.feed(normalize_article_content(content))
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
