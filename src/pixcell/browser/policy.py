"""URL allow/deny policy and address normalization for :mod:`pixcell.browser`.

Ported from ``deftai/console`` (MIT).

:class:`Policy` (aliased as :class:`URLPolicy`) is a small frozen dataclass that decides
which URLs a browser may load, redirect to, or open via pop-ups. Calling a :class:`Policy`
directly (``policy(url)``) returns ``policy.allows(url)``, so it passes straight to
:class:`~pixcell.browser.Browser` or :class:`~pixcell.browser.BrowserView`.

Use ``Policy.offline()`` (or ``Policy(no_network=True)``) for untrusted local documents
such as :mod:`pixcell.docs` HTML output: only ``file://`` and ``data:`` URLs (plus
``about:blank``) are allowed, and Chrome's own networking is shut off.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pixcell.browser import _portability

LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
PAGE_SCHEMES: frozenset[str] = frozenset({"http", "https", "file", "about", "data"})
OFFLINE_SCHEMES: frozenset[str] = frozenset({"file", "data", "about"})

_SYSTEM_TEMP = _portability.SYSTEM_TEMP
"""/tmp as well as the user's temp folder (macOS gives each user its own)."""


def host_of(url: str) -> str:
    """The site an address is on (lower case, no port), or ``""`` for local schemes."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def file_path(url: str) -> Path | None:
    """Resolved local :class:`Path` for a ``file:`` URL, or ``None`` if invalid or remote."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "file":
            return None
        if parsed.netloc.lower() not in ("", "localhost"):
            return None
        return _portability.path_from_file_uri(url).resolve()
    except (OSError, ValueError):
        return None


_file_path = file_path


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    """Match an exact lowercase host or a ``*.example.com`` subdomain pattern."""
    for raw in patterns:
        pattern = raw.lower().rstrip(".")
        if host == pattern:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False


@dataclass(frozen=True)
class Policy:
    """URL allow/deny policy for :class:`~pixcell.browser.Browser` and ``BrowserView``.

    Parameters
    ----------
    schemes:
        Permitted URL schemes (default: ``http``, ``https``, ``file``, ``about``, ``data``).
        Non-page schemes like ``javascript:``, ``mailto:``, ``chrome:``, ``blob:`` are denied.
    allow_hosts:
        When set, only network URLs matching one of these hosts (exact or ``*.domain``) are
        allowed. ``None`` allows any host not in ``deny_hosts``.
    deny_hosts:
        Network hosts (exact or ``*.domain``) that are always denied.
    file_roots:
        When set, ``file://`` URLs must resolve inside one of these directories (or equal a
        permitted file path). ``None`` allows any local ``file://`` path.
    no_network:
        No-network mode for local documents: permits only ``file://`` and ``data:`` URLs
        (plus ``about:blank``), denies ``http:``/``https:`` and all other network schemes,
        and launches Chrome with network access disabled.
    """

    schemes: frozenset[str] = PAGE_SCHEMES
    allow_hosts: frozenset[str] | None = None
    deny_hosts: frozenset[str] = frozenset()
    file_roots: tuple[Path, ...] | None = None
    no_network: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "schemes", frozenset(s.lower() for s in self.schemes))
        if self.allow_hosts is not None:
            object.__setattr__(self, "allow_hosts", frozenset(h.lower().rstrip(".") for h in self.allow_hosts))
        object.__setattr__(self, "deny_hosts", frozenset(h.lower().rstrip(".") for h in self.deny_hosts))
        if self.file_roots is not None:
            object.__setattr__(self, "file_roots", tuple(Path(root).resolve() for root in self.file_roots))

    @classmethod
    def offline(cls, *, file_roots: Iterable[str | os.PathLike[str]] | None = None) -> Policy:
        """No-network policy for local documents: ``file://`` and ``data:`` only."""
        roots = tuple(Path(r) for r in file_roots) if file_roots is not None else None
        return cls(schemes=OFFLINE_SCHEMES, file_roots=roots, no_network=True)

    @classmethod
    def localhost(cls, *, file_roots: Iterable[str | os.PathLike[str]] | None = None) -> Policy:
        """Allow only localhost (``localhost``, ``127.0.0.1``, ``::1``) and local ``file://`` URLs."""
        roots = tuple(Path(r) for r in file_roots) if file_roots is not None else None
        return cls(
            schemes=frozenset({"http", "https", "file", "about"}),
            allow_hosts=LOCAL_HOSTS,
            file_roots=roots,
        )

    def allows(self, url: str) -> bool:
        """Return ``True`` if ``url`` is permitted by this policy."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        scheme = parsed.scheme.lower()
        if not scheme or scheme not in self.schemes:
            return False
        if self.no_network:
            if scheme == "about":
                return url.lower() == "about:blank"
            if scheme not in ("file", "data"):
                return False
        if scheme == "about":
            return True
        if scheme == "data":
            return True
        if scheme == "file":
            resolved = file_path(url)
            if resolved is None:
                return False
            if self.file_roots is not None:
                return any(resolved == root or root in resolved.parents for root in self.file_roots)
            return True
        host = host_of(url)
        if not host:
            return False
        if _host_matches(host, self.deny_hosts):
            return False
        if self.allow_hosts is not None and not _host_matches(host, self.allow_hosts):
            return False
        return True

    def __call__(self, url: str) -> bool:
        return self.allows(url)


URLPolicy = Policy


# --- Console-compatible agent page gate & URL normalizer ----------------------------------


def _in_workspace(path: Path | None, project: Path | None) -> bool:
    """Inside the project, or a temporary folder (where agents write reports)."""
    if path is None:
        return False
    roots = [Path(tempfile.gettempdir()).resolve()]
    if _SYSTEM_TEMP is not None:
        roots.append(_SYSTEM_TEMP)
    if project is not None:
        roots.append(project.resolve())
    return any(path == root or root in path.parents for root in roots)


def decide(url: str, setting: str, allowed_hosts: set[str], project: Path | None = None) -> str:
    """``open``, ``ask`` or ``refuse`` for an agent asking to open ``url``."""
    scheme = urlparse(url).scheme.lower()
    if scheme not in PAGE_SCHEMES:
        return "refuse"  # mailto:, tel:, javascript: ... aren't pages
    if scheme == "about":
        return "open"
    if scheme == "file":
        # The agent's own reports and pages: in the project, or the temp folder agents
        # write to. A file anywhere else (say, in the user's home) is asked about first.
        if _in_workspace(file_path(url), project):
            return "open"
        return {"any": "open", "local": "refuse"}.get(setting, "ask")
    if scheme == "data":
        # A page made up on the spot has no real use for an agent, and is the classic way
        # to make a fake one: never opened unasked.
        return {"any": "open", "local": "refuse"}.get(setting, "ask")
    host = host_of(url)
    if host in LOCAL_HOSTS or host in allowed_hosts:
        return "open"
    return {"any": "open", "local": "refuse"}.get(setting, "ask")


def navigation_gate(
    first: str, setting: Callable[[], str], allowed_hosts: set[str], project: Path | None = None
) -> Callable[[str], bool]:
    """Whether a page an agent opened at ``first`` may go on to ``url`` by itself (a link,
    a script, a redirect, a pop-up): where :func:`decide` would open it unasked, or the
    page the user agreed to (``first``, and for a site, its other pages). Anything else
    an agent could only reach by asking."""
    first_host = host_of(first) if urlparse(first).scheme.lower() in ("http", "https") else ""
    first_file = file_path(first) if urlparse(first).scheme.lower() == "file" else None

    def allow(url: str) -> bool:
        if url == first:
            return True
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https") and first_host and host_of(url) == first_host:
            return True
        if scheme == "file" and first_file is not None and file_path(url) == first_file:
            return True
        return decide(url, setting(), allowed_hosts, project) == "open"

    return allow


def normalize_url(text: str, base: Path | None = None) -> str:
    """What the user typed as a URL: a path becomes ``file://``, localhost ``http://``, and a
    bare domain ``https://``."""
    text = _trim(text.strip())
    if not text:
        raise ValueError("Expected an address: a URL, localhost:3000, or a file.")
    if text.lower().startswith("javascript:"):
        raise ValueError("javascript: addresses aren't opened.")
    if re.match(r"^[a-zA-Z][\w+.-]*://", text) or re.match(r"^(about|data|mailto|tel|blob):", text, re.IGNORECASE):
        return text
    if (
        text.startswith(("/", "~", "./", "../"))
        or Path(text).is_absolute()
        or (base is not None and (base / text).exists())
    ):
        path = Path(text).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        return path.resolve().as_uri()
    if re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", text):
        return f"http://{text}"
    return f"https://{text}"


def _trim(text: str) -> str:
    """Drop what a sentence leaves around an address ("see (example.com), then"). A
    bracket the address itself opens stays: wiki/Function_(mathematics)."""
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">", '"': '"', "'": "'"}
    while text:
        before = text
        text = text.rstrip(",;.:!?")
        if not text:
            break
        if len(text) > 1 and text[0] in pairs and text[-1] == pairs[text[0]]:
            text = text[1:-1]  # enclosed whole
        elif text[0] in pairs and text.count(text[0]) > text.count(pairs[text[0]]):
            text = text[1:]  # an opener with no closer
        elif text[-1] in ")]}>" and text.count(text[-1]) > text.count({")": "(", "]": "[", "}": "{", ">": "<"}[text[-1]]):
            text = text[:-1]  # a closer with no opener
        if text == before:
            break
    return text
