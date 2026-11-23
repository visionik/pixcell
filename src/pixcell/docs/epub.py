"""Built-in EPUB renderer using stdlib ``zipfile`` and ``xml.etree`` for :mod:`pixcell.docs`.

Parses ``META-INF/container.xml`` and the OPF package document, reads spine chapters in
order, strips ``<script>`` elements and inline event handlers, and inlines relative images
and stylesheets so the returned :class:`~pixcell.docs.HTML` is completely self-contained.
"""

from __future__ import annotations

import base64
import html
import html.entities
import mimetypes
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from pixcell.docs.errors import DocsError, DocumentTooLarge
from pixcell.docs.registry import sniff_mime
from pixcell.docs.types import DEFAULT_MAX_BYTES, DEFAULT_MAX_PAGES, HTML

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_DC_NS = "http://purl.org/dc/elements/1.1/"

_READ_CHUNK_SIZE = 65_536
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\b[^>]*>", re.IGNORECASE)
_NAMED_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]+);")
_CSS_URL_RE = re.compile(
    r"""url\(\s*(?P<quote>['"]?)(?P<url>[^)'"]+?)(?P=quote)\s*\)""",
    re.IGNORECASE,
)
_XML_BUILTIN_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _validate_member_name(name: str, path: Path) -> None:
    if not name:
        raise DocsError(f"Empty archive member name in EPUB {path}")
    if name.startswith(("/", "\\")) or _DRIVE_PREFIX_RE.match(name):
        raise DocsError(f"Absolute archive member path in EPUB {path}: {name!r}")
    parts = re.split(r"[/\\]", name)
    if any(part == ".." for part in parts):
        raise DocsError(f"Path traversal archive member in EPUB {path}: {name!r}")


def _is_external_or_fragment(url: str) -> bool:
    stripped = url.strip()
    if not stripped or stripped.startswith(("#", "//")):
        return True
    return bool(_SCHEME_RE.match(stripped))


def _is_javascript_uri(url: str) -> bool:
    cleaned = re.sub(r"[\x00-\x20]+", "", url).lower()
    return cleaned.startswith(("javascript:", "vbscript:"))


def _resolve_archive_path(
    base_dir: str,
    href: str,
    path: Path,
    *,
    strict: bool = False,
) -> str | None:
    raw = href.strip()
    if not raw or _is_external_or_fragment(raw):
        return None
    # Strip fragment and query parameters before resolving within the archive.
    without_frag = raw.split("#", 1)[0].split("?", 1)[0]
    unquoted = urllib.parse.unquote(without_frag).strip()
    if not unquoted:
        return None
    if unquoted.startswith(("/", "\\")) or _DRIVE_PREFIX_RE.match(unquoted):
        if strict:
            raise DocsError(f"Absolute archive reference in EPUB {path}: {href!r}")
        return None
    # Normalize backslashes to POSIX separators inside ZIP archives.
    posix_href = unquoted.replace("\\", "/")
    joined = posixpath.join(base_dir, posix_href) if base_dir else posix_href
    normalized = posixpath.normpath(joined)
    if normalized in ("", "."):
        return None
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        if strict:
            raise DocsError(f"Path traversal reference in EPUB {path}: {href!r}")
        return None
    return normalized


class _ArchiveReader:
    """Bounded, path-validated reader over an EPUB ZIP archive."""

    def __init__(
        self,
        archive: zipfile.ZipFile,
        path: Path,
        *,
        max_bytes: int | None,
    ) -> None:
        self._archive = archive
        self.path = path
        self.max_bytes = max_bytes
        self._bytes_read = 0
        self._cache: dict[str, bytes] = {}
        self.members: set[str] = set()

        total_declared = 0
        for info in archive.infolist():
            _validate_member_name(info.filename, path)
            self.members.add(info.filename)
            total_declared += max(0, info.file_size)
            if max_bytes is not None and total_declared > max_bytes:
                raise DocumentTooLarge(path, size=total_declared, max_bytes=max_bytes)

    def read_bytes(self, name: str) -> bytes:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        chunks: list[bytes] = []
        with self._archive.open(name, "r") as stream:
            while True:
                chunk = stream.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                self._bytes_read += len(chunk)
                if self.max_bytes is not None and self._bytes_read > self.max_bytes:
                    raise DocumentTooLarge(
                        self.path,
                        size=self._bytes_read,
                        max_bytes=self.max_bytes,
                    )
                chunks.append(chunk)
        data = b"".join(chunks)
        self._cache[name] = data
        return data

    def read_text(self, name: str) -> str:
        return self.read_bytes(name).decode("utf-8-sig", errors="replace")


def _find_opf_path(reader: _ArchiveReader) -> str:
    container_name = "META-INF/container.xml"
    if container_name not in reader.members:
        raise DocsError(f"EPUB missing {container_name}: {reader.path}")
    try:
        root = ET.fromstring(reader.read_text(container_name))
    except ET.ParseError as exc:
        raise DocsError(f"Invalid {container_name} in EPUB {reader.path}: {exc}") from exc

    fallback_path: str | None = None
    for elem in root.iter():
        if _local_name(elem.tag) != "rootfile":
            continue
        full_path = elem.attrib.get("full-path", "").strip()
        if not full_path:
            continue
        media_type = elem.attrib.get("media-type", "").strip().lower()
        if media_type == "application/oebps-package+xml":
            resolved = _resolve_archive_path("", full_path, reader.path, strict=True)
            if resolved is None or resolved not in reader.members:
                raise DocsError(f"EPUB OPF rootfile {full_path!r} not found in {reader.path}")
            return resolved
        if fallback_path is None:
            fallback_path = full_path

    if fallback_path is not None:
        resolved = _resolve_archive_path("", fallback_path, reader.path, strict=True)
        if resolved is not None and resolved in reader.members:
            return resolved

    raise DocsError(f"EPUB container.xml has no valid OPF rootfile: {reader.path}")


def _parse_opf(
    reader: _ArchiveReader,
    opf_path: str,
) -> tuple[str, dict[str, str], list[str]]:
    """Parse OPF metadata title, path->media_type map, and ordered spine chapter paths."""
    try:
        root = ET.fromstring(reader.read_text(opf_path))
    except ET.ParseError as exc:
        raise DocsError(f"Invalid OPF package {opf_path!r} in EPUB {reader.path}: {exc}") from exc

    opf_dir = posixpath.dirname(opf_path)

    # Extract dc:title from <metadata>.
    title = ""
    fallback_title = ""
    for elem in root:
        if _local_name(elem.tag) == "metadata":
            for child in elem.iter():
                if _local_name(child.tag) == "title":
                    text = "".join(child.itertext()).strip()
                    if not text:
                        continue
                    if child.tag == f"{{{_DC_NS}}}title":
                        title = text
                        break
                    if not fallback_title:
                        fallback_title = text
            if not title:
                title = fallback_title
            break

    # Parse <manifest> items.
    id_to_href: dict[str, str] = {}
    path_to_mime: dict[str, str] = {}
    for elem in root:
        if _local_name(elem.tag) != "manifest":
            continue
        for item in elem:
            if _local_name(item.tag) != "item":
                continue
            item_id = item.attrib.get("id", "").strip()
            href = item.attrib.get("href", "").strip()
            media_type = item.attrib.get("media-type", "").strip().lower()
            if not href:
                continue
            resolved = _resolve_archive_path(opf_dir, href, reader.path, strict=True)
            if resolved is None:
                continue
            if item_id:
                id_to_href[item_id] = resolved
            if media_type:
                path_to_mime[resolved] = media_type

    # Parse <spine> in reading order.
    linear_chapters: list[str] = []
    all_chapters: list[str] = []
    for elem in root:
        if _local_name(elem.tag) != "spine":
            continue
        for itemref in elem:
            if _local_name(itemref.tag) != "itemref":
                continue
            idref = itemref.attrib.get("idref", "").strip()
            chapter_path = id_to_href.get(idref)
            if not chapter_path or chapter_path not in reader.members:
                continue
            all_chapters.append(chapter_path)
            if itemref.attrib.get("linear", "yes").strip().lower() != "no":
                linear_chapters.append(chapter_path)

    chapters = linear_chapters if linear_chapters else all_chapters
    return title, path_to_mime, chapters


def _guess_mime(member_path: str, data: bytes, path_to_mime: dict[str, str]) -> str:
    if member_path in path_to_mime:
        return path_to_mime[member_path]
    sniffed = sniff_mime(data[:512])
    if sniffed:
        return sniffed
    guessed, _ = mimetypes.guess_type(member_path)
    return guessed or "application/octet-stream"


def _to_data_uri(
    reader: _ArchiveReader,
    member_path: str,
    path_to_mime: dict[str, str],
    uri_cache: dict[str, str],
) -> str:
    cached = uri_cache.get(member_path)
    if cached is not None:
        return cached
    data = reader.read_bytes(member_path)
    mime = _guess_mime(member_path, data, path_to_mime)
    encoded = base64.b64encode(data).decode("ascii")
    uri = f"data:{mime};base64,{encoded}"
    uri_cache[member_path] = uri
    return uri


def _rewrite_css_urls(
    css_text: str,
    base_dir: str,
    reader: _ArchiveReader,
    path_to_mime: dict[str, str],
    uri_cache: dict[str, str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        resolved = _resolve_archive_path(base_dir, raw_url, reader.path)
        if resolved is None or resolved not in reader.members:
            return match.group(0)
        uri = _to_data_uri(reader, resolved, path_to_mime, uri_cache)
        return f'url("{uri}")'

    return _CSS_URL_RE.sub(replace, css_text)


def _rewrite_srcset(
    srcset: str,
    base_dir: str,
    reader: _ArchiveReader,
    path_to_mime: dict[str, str],
    uri_cache: dict[str, str],
) -> str:
    parts: list[str] = []
    for candidate in srcset.split(","):
        tokens = candidate.strip().split()
        if not tokens:
            continue
        resolved = _resolve_archive_path(base_dir, tokens[0], reader.path)
        if resolved is not None and resolved in reader.members:
            tokens[0] = _to_data_uri(reader, resolved, path_to_mime, uri_cache)
        parts.append(" ".join(tokens))
    return ", ".join(parts)


def _replace_html_entities(markup: str) -> str:
    cleaned = _DOCTYPE_RE.sub("", markup)

    def sub_entity(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_BUILTIN_ENTITIES:
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        if codepoint is not None:
            return f"&#{codepoint};"
        return match.group(0)

    return _NAMED_ENTITY_RE.sub(sub_entity, cleaned)


def _remove_child_preserving_tail(parent: ET.Element, index: int) -> None:
    child = parent[index]
    tail = child.tail
    if tail:
        if index > 0:
            prev = parent[index - 1]
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(child)


def _sanitize_and_rewrite_element(
    elem: ET.Element,
    *,
    chapter_dir: str,
    reader: _ArchiveReader,
    path_to_mime: dict[str, str],
    uri_cache: dict[str, str],
    css_blocks: list[str],
    seen_css_paths: set[str],
    in_svg: bool = False,
) -> None:
    # Normalize XML namespaces so ET.tostring(..., method="html") emits clean HTML5 tags.
    tag = elem.tag
    if tag.startswith(f"{{{_XHTML_NS}}}"):
        elem.tag = tag[len(_XHTML_NS) + 2 :]
    elif tag.startswith(f"{{{_SVG_NS}}}"):
        local = tag[len(_SVG_NS) + 2 :]
        elem.tag = local
        if local.lower() == "svg" and not in_svg:
            elem.attrib.setdefault("xmlns", _SVG_NS)
        in_svg = True
    elif "}" in tag:
        elem.tag = tag.rsplit("}", 1)[1]

    local_tag = elem.tag.lower()

    # Clean attributes: strip inline event handlers, javascript: URLs, and normalize xlink:href.
    new_attrib: dict[str, str] = {}
    for attr_name, attr_val in list(elem.attrib.items()):
        if attr_name == f"{{{_XLINK_NS}}}href":
            clean_name = "href"
        elif "}" in attr_name:
            clean_name = attr_name.rsplit("}", 1)[1]
        else:
            clean_name = attr_name

        lower_name = clean_name.lower()
        if lower_name.startswith("on"):
            continue
        if lower_name in ("href", "src", "data", "action", "formaction") and _is_javascript_uri(
            attr_val
        ):
            continue
        new_attrib[clean_name] = attr_val

    # Rewrite relative asset references into self-contained data URIs.
    if local_tag == "img":
        src = new_attrib.get("src")
        if src:
            resolved = _resolve_archive_path(chapter_dir, src, reader.path)
            if resolved is not None and resolved in reader.members:
                new_attrib["src"] = _to_data_uri(reader, resolved, path_to_mime, uri_cache)
        srcset = new_attrib.get("srcset")
        if srcset:
            new_attrib["srcset"] = _rewrite_srcset(
                srcset, chapter_dir, reader, path_to_mime, uri_cache
            )
    elif local_tag == "image":
        href = new_attrib.get("href")
        if href:
            resolved = _resolve_archive_path(chapter_dir, href, reader.path)
            if resolved is not None and resolved in reader.members:
                new_attrib["href"] = _to_data_uri(reader, resolved, path_to_mime, uri_cache)
    elif local_tag == "source":
        src = new_attrib.get("src")
        if src:
            resolved = _resolve_archive_path(chapter_dir, src, reader.path)
            if resolved is not None and resolved in reader.members:
                new_attrib["src"] = _to_data_uri(reader, resolved, path_to_mime, uri_cache)
        srcset = new_attrib.get("srcset")
        if srcset:
            new_attrib["srcset"] = _rewrite_srcset(
                srcset, chapter_dir, reader, path_to_mime, uri_cache
            )
    elif local_tag == "object":
        data_attr = new_attrib.get("data")
        if data_attr:
            resolved = _resolve_archive_path(chapter_dir, data_attr, reader.path)
            if resolved is not None and resolved in reader.members:
                new_attrib["data"] = _to_data_uri(reader, resolved, path_to_mime, uri_cache)

    inline_style = new_attrib.get("style")
    if inline_style and "url(" in inline_style.lower():
        new_attrib["style"] = _rewrite_css_urls(
            inline_style, chapter_dir, reader, path_to_mime, uri_cache
        )

    elem.attrib.clear()
    elem.attrib.update(new_attrib)

    # Walk children in reverse index order or careful index loop so removals preserve tails.
    idx = 0
    while idx < len(elem):
        child = elem[idx]
        child_local = _local_name(child.tag)
        if child_local == "script":
            _remove_child_preserving_tail(elem, idx)
            continue
        if child_local == "link":
            rel = child.attrib.get("rel", "").lower().split()
            href = child.attrib.get("href", "").strip()
            if "stylesheet" in rel and href:
                resolved = _resolve_archive_path(chapter_dir, href, reader.path)
                if (
                    resolved is not None
                    and resolved in reader.members
                    and resolved not in seen_css_paths
                ):
                    seen_css_paths.add(resolved)
                    css_raw = reader.read_text(resolved)
                    css_dir = posixpath.dirname(resolved)
                    css_rewritten = _rewrite_css_urls(
                        css_raw, css_dir, reader, path_to_mime, uri_cache
                    ).strip()
                    if css_rewritten:
                        css_blocks.append(css_rewritten)
            _remove_child_preserving_tail(elem, idx)
            continue
        if child_local == "style":
            style_text = "".join(child.itertext()).strip()
            if style_text:
                css_rewritten = _rewrite_css_urls(
                    style_text, chapter_dir, reader, path_to_mime, uri_cache
                ).strip()
                if css_rewritten:
                    css_blocks.append(css_rewritten)
            _remove_child_preserving_tail(elem, idx)
            continue

        _sanitize_and_rewrite_element(
            child,
            chapter_dir=chapter_dir,
            reader=reader,
            path_to_mime=path_to_mime,
            uri_cache=uri_cache,
            css_blocks=css_blocks,
            seen_css_paths=seen_css_paths,
            in_svg=in_svg,
        )
        idx += 1


class _FallbackChapterParser(HTMLParser):
    """Fallback HTML parser for malformed EPUB chapters that aren't well-formed XML."""

    def __init__(
        self,
        *,
        chapter_dir: str,
        reader: _ArchiveReader,
        path_to_mime: dict[str, str],
        uri_cache: dict[str, str],
        css_blocks: list[str],
        seen_css_paths: set[str],
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._chapter_dir = chapter_dir
        self._reader = reader
        self._path_to_mime = path_to_mime
        self._uri_cache = uri_cache
        self._css_blocks = css_blocks
        self._seen_css_paths = seen_css_paths
        self._script_depth = 0
        self._style_depth = 0
        self._style_chunks: list[str] = []
        self._has_body = False
        self._in_body = False
        self._body_parts: list[str] = []
        self._all_parts: list[str] = []

    def _emit(self, text: str) -> None:
        if self._in_body:
            self._body_parts.append(text)
        elif not self._has_body:
            self._all_parts.append(text)

    def _format_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        rendered: list[str] = []
        for raw_name, raw_val in attrs:
            name = raw_name.lower()
            if name.startswith("on"):
                continue
            val = raw_val or ""
            if name in ("href", "src", "data", "action", "formaction") and _is_javascript_uri(val):
                continue
            if tag == "img" and name == "src":
                resolved = _resolve_archive_path(self._chapter_dir, val, self._reader.path)
                if resolved is not None and resolved in self._reader.members:
                    val = _to_data_uri(self._reader, resolved, self._path_to_mime, self._uri_cache)
            elif tag in ("img", "source") and name == "srcset":
                val = _rewrite_srcset(
                    val, self._chapter_dir, self._reader, self._path_to_mime, self._uri_cache
                )
            elif name == "style" and "url(" in val.lower():
                val = _rewrite_css_urls(
                    val, self._chapter_dir, self._reader, self._path_to_mime, self._uri_cache
                )
            if raw_val is None:
                rendered.append(f" {raw_name}")
            else:
                rendered.append(f' {raw_name}="{html.escape(val, quote=True)}"')
        return "".join(rendered)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower == "script":
            self._script_depth += 1
            return
        if self._script_depth > 0:
            return
        if lower == "style":
            self._style_depth += 1
            self._style_chunks.clear()
            return
        if lower == "link":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            if "stylesheet" in attr_map.get("rel", "").lower().split():
                href = attr_map.get("href", "").strip()
                resolved = _resolve_archive_path(self._chapter_dir, href, self._reader.path)
                if (
                    resolved is not None
                    and resolved in self._reader.members
                    and resolved not in self._seen_css_paths
                ):
                    self._seen_css_paths.add(resolved)
                    css_raw = self._reader.read_text(resolved)
                    css_rewritten = _rewrite_css_urls(
                        css_raw,
                        posixpath.dirname(resolved),
                        self._reader,
                        self._path_to_mime,
                        self._uri_cache,
                    ).strip()
                    if css_rewritten:
                        self._css_blocks.append(css_rewritten)
            return
        if lower == "body":
            self._has_body = True
            self._in_body = True
            return
        if lower in ("html", "head", "meta", "title"):
            return
        self._emit(f"<{tag}{self._format_attrs(lower, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower == "script" or self._script_depth > 0:
            return
        if lower == "link":
            self.handle_starttag(tag, attrs)
            return
        if lower in ("meta", "title"):
            return
        self._emit(f"<{tag}{self._format_attrs(lower, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "script":
            if self._script_depth > 0:
                self._script_depth -= 1
            return
        if self._script_depth > 0:
            return
        if lower == "style":
            if self._style_depth > 0:
                self._style_depth -= 1
                if self._style_depth == 0:
                    css_text = "".join(self._style_chunks).strip()
                    if css_text:
                        rewritten = _rewrite_css_urls(
                            css_text,
                            self._chapter_dir,
                            self._reader,
                            self._path_to_mime,
                            self._uri_cache,
                        ).strip()
                        if rewritten:
                            self._css_blocks.append(rewritten)
            return
        if lower == "body":
            self._in_body = False
            return
        if lower in ("html", "head", "title") or lower in _VOID_HTML_TAGS:
            return
        self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._script_depth > 0:
            return
        if self._style_depth > 0:
            self._style_chunks.append(data)
            return
        self._emit(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._script_depth > 0:
            return
        ref = f"&{name};"
        if self._style_depth > 0:
            self._style_chunks.append(ref)
        else:
            self._emit(ref)

    def handle_charref(self, name: str) -> None:
        if self._script_depth > 0:
            return
        ref = f"&#{name};"
        if self._style_depth > 0:
            self._style_chunks.append(ref)
        else:
            self._emit(ref)

    @property
    def body_html(self) -> str:
        return "".join(self._body_parts if self._has_body else self._all_parts).strip()


def _render_chapter(
    chapter_path: str,
    *,
    reader: _ArchiveReader,
    path_to_mime: dict[str, str],
    uri_cache: dict[str, str],
    css_blocks: list[str],
    seen_css_paths: set[str],
) -> str:
    chapter_dir = posixpath.dirname(chapter_path)
    raw_markup = reader.read_text(chapter_path)
    prepared = _replace_html_entities(raw_markup)
    try:
        root = ET.fromstring(prepared)
    except ET.ParseError:
        parser = _FallbackChapterParser(
            chapter_dir=chapter_dir,
            reader=reader,
            path_to_mime=path_to_mime,
            uri_cache=uri_cache,
            css_blocks=css_blocks,
            seen_css_paths=seen_css_paths,
        )
        parser.feed(raw_markup)
        parser.close()
        return parser.body_html

    _sanitize_and_rewrite_element(
        root,
        chapter_dir=chapter_dir,
        reader=reader,
        path_to_mime=path_to_mime,
        uri_cache=uri_cache,
        css_blocks=css_blocks,
        seen_css_paths=seen_css_paths,
    )

    body: ET.Element | None = None
    if root.tag.lower() == "body":
        body = root
    else:
        for child in root:
            if child.tag.lower() == "body":
                body = child
                break

    if body is None:
        return ET.tostring(root, encoding="unicode", method="html").strip()

    parts: list[str] = []
    if body.text:
        parts.append(html.escape(body.text, quote=False))
    for child in body:
        parts.append(ET.tostring(child, encoding="unicode", method="html"))
    return "".join(parts).strip()


def render(
    path: Path,
    *,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
) -> HTML:
    """Open an EPUB archive and return a self-contained :class:`HTML` document.

    Parameters
    ----------
    path:
        Path to the ``.epub`` file.
    max_pages:
        Maximum number of spine chapters to include (``None`` for no cap).
    max_bytes:
        Maximum total uncompressed size across archive entries (``None`` for no cap).
        Raises :class:`~pixcell.docs.DocumentTooLarge` if exceeded.
    """
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1 or None")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative or None")

    try:
        archive = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise DocsError(f"Invalid EPUB archive {path}: {exc}") from exc

    with archive:
        reader = _ArchiveReader(archive, path, max_bytes=max_bytes)
        opf_path = _find_opf_path(reader)
        title, path_to_mime, chapters = _parse_opf(reader, opf_path)
        if max_pages is not None:
            chapters = chapters[:max_pages]

        uri_cache: dict[str, str] = {}
        css_blocks: list[str] = []
        seen_css_paths: set[str] = set()
        sections: list[str] = []

        for index, chapter_path in enumerate(chapters, start=1):
            chapter_html = _render_chapter(
                chapter_path,
                reader=reader,
                path_to_mime=path_to_mime,
                uri_cache=uri_cache,
                css_blocks=css_blocks,
                seen_css_paths=seen_css_paths,
            )
            escaped_href = html.escape(chapter_path, quote=True)
            sections.append(
                f'<section class="epub-chapter" data-chapter="{index}" '
                f'data-href="{escaped_href}">\n{chapter_html}\n</section>'
            )

    head_parts = ['<meta charset="utf-8">']
    if title:
        head_parts.append(f"<title>{html.escape(title)}</title>")
    for css in css_blocks:
        safe_css = re.sub(r"</style", r"<\/style", css, flags=re.IGNORECASE)
        head_parts.append(f"<style>\n{safe_css}\n</style>")

    head_html = "\n".join(head_parts)
    body_html = "\n".join(sections)
    document_html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        f"{head_html}\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )
    return HTML(html=document_html, title=title, source=path)
