"""Errors raised by :mod:`pixcell.docs`."""

from __future__ import annotations

from pathlib import Path


class DocsError(Exception):
    """Base error for document opening and rendering."""


class MissingExtra(DocsError):
    """A renderer matched, but the optional extra providing its dependency isn't installed."""

    def __init__(
        self,
        extra: str,
        *,
        renderer: str | None = None,
        dependency: str | None = None,
    ) -> None:
        self.extra = extra
        self.renderer = renderer
        self.dependency = dependency
        who = f"renderer {renderer!r}" if renderer else f"the {extra!r} extra"
        missing = f" (missing {dependency!r})" if dependency else ""
        super().__init__(
            f"{who} requires optional dependencies{missing}. "
            f'Install with: pip install "pixcell[{extra}]"'
        )


class UnsupportedType(DocsError):
    """No registered renderer can handle this file."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        suffix: str = "",
        mime_type: str | None = None,
    ) -> None:
        self.path = path
        self.suffix = suffix
        self.mime_type = mime_type
        details: list[str] = []
        if path is not None:
            details.append(str(path))
        if mime_type:
            details.append(f"type {mime_type}")
        elif suffix:
            details.append(f"suffix {suffix!r}")
        detail = f" ({', '.join(details)})" if details else ""
        super().__init__(f"Unsupported document type{detail}")


class DocumentTooLarge(DocsError):
    """The file exceeds the ``max_bytes`` guard rail."""

    def __init__(self, path: Path, *, size: int, max_bytes: int) -> None:
        self.path = path
        self.size = size
        self.max_bytes = max_bytes
        super().__init__(
            f"{path} is {size:,} bytes, exceeding the {max_bytes:,}-byte limit "
            "(pass max_bytes=None to disable)"
        )
