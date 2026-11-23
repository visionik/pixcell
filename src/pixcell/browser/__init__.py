"""Headless Chromium browser over CDP and a live Textual ``BrowserView`` widget.

Ported from ``deftai/console`` (MIT).

- :class:`Browser` (:mod:`pixcell.browser.cdp`) — stdlib-only CDP client over
  ``--remote-debugging-pipe``: ``navigate``, ``screenshot`` (Pillow image or PNG bytes),
  ``eval``, ``close``. Does not require Textual.
- :class:`Policy` (:mod:`pixcell.browser.policy`) — URL allow/deny policy dataclass,
  including ``Policy.offline()`` (``file://`` and ``data:`` only) for local documents.
- :func:`open_html` — open a :class:`pixcell.docs.HTML` document in a headless
  :class:`Browser` under the no-network policy.
- :func:`install` / :func:`find_browser` (:mod:`pixcell.browser.install`) — locate an
  installed Chromium browser or download Chrome for Testing into pixcell's cache directory.
- :class:`BrowserView` (:mod:`pixcell.browser.view`) — live Textual widget behind
  ``pixcell[textual]``, streaming frames via :class:`pixcell.live.LiveImage`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pixcell.browser import install
from pixcell.browser.cdp import Browser, BrowserError, open_html, stage_html_document
from pixcell.browser.install import (
    InstallError,
    find_browser,
    folder,
    installed,
    latest,
    platform_name,
    set_cache_dir,
)
from pixcell.browser.keys import Press, press_for
from pixcell.browser.policy import (
    LOCAL_HOSTS,
    OFFLINE_SCHEMES,
    PAGE_SCHEMES,
    Policy,
    URLPolicy,
    decide,
    file_path,
    host_of,
    navigation_gate,
    normalize_url,
)

if TYPE_CHECKING:
    from pixcell.browser.view import BrowserView

__all__ = [
    "Browser",
    "BrowserError",
    "BrowserView",
    "InstallError",
    "LOCAL_HOSTS",
    "OFFLINE_SCHEMES",
    "PAGE_SCHEMES",
    "Policy",
    "Press",
    "URLPolicy",
    "decide",
    "file_path",
    "find_browser",
    "folder",
    "host_of",
    "install",
    "installed",
    "latest",
    "navigation_gate",
    "normalize_url",
    "open_html",
    "platform_name",
    "press_for",
    "set_cache_dir",
    "stage_html_document",
]


def __getattr__(name: str) -> Any:
    if name == "BrowserView":
        from pixcell.browser.view import BrowserView

        return BrowserView
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
