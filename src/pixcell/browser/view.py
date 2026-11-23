"""A live browser widget for Textual: headless Chromium streamed via :class:`pixcell.live.LiveImage`.

Ported from ``deftai/console`` (MIT), without Console's side-pane abstraction. Requires
the ``pixcell[textual]`` extra.

The page is drawn by :class:`pixcell.live.LiveImage`, laid out at the widget's exact pixel
dimensions; clicks, keyboard input, paste, and scroll wheel steps go straight to the page.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

try:
    from textual import events, log, work
    from textual.app import ComposeResult
    from textual.containers import HorizontalGroup, Vertical, VerticalGroup
    from textual.content import Content
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import ProgressBar, Static
except ImportError as exc:  # pragma: no cover - exercised when [textual] is absent
    raise ImportError("pixcell.browser.view requires Textual: pip install pixcell[textual]") from exc

from pixcell.browser.cdp import BrowserError, resolve_policy, stage_html_document
from pixcell.browser.keys import press_for
from pixcell.browser.policy import LOCAL_HOSTS, Policy, host_of, normalize_url

if TYPE_CHECKING:
    from pixcell.browser.cdp import Browser
    from pixcell.docs.types import HTML

__all__ = [
    "BrowserHeader",
    "BrowserPhase",
    "BrowserView",
    "CloseButton",
    "DownloadOffer",
    "GRAPHICS",
    "LeaveBrowserScreen",
    "OfferButton",
    "PASSWORD_WARNING",
    "SCROLL_MERGE",
    "SCROLL_PIXELS",
    "has_graphics",
    "normalize_url",
]

GRAPHICS = ("kitty", "iterm2", "kitty-direct", "sixel", "host")
"""Protocols that draw real pixels (everything except half blocks and ``off``)."""


def has_graphics() -> bool:
    """Can the terminal show real pixels (not just coloured half blocks)?"""
    from pixcell.textual import capabilities

    return capabilities().protocol in GRAPHICS


BrowserPhase = Literal[
    "idle",
    "starting",
    "offering",
    "downloading",
    "running",
    "failed",
    "closed",
]
"""Where a :class:`BrowserView` is in its life.

- ``idle``: waiting for its first viewport size before starting a browser.
- ``starting``: launching a browser in the background.
- ``offering``: no browser found; offering to download Chrome headless shell.
- ``downloading``: downloading Chrome headless shell.
- ``running``: browser page is live and ready for input.
- ``failed``: startup or download failed (or was declined); can be retried.
- ``closed``: unmounted; stops its browser.
"""

_PHASE_MOVES: dict[BrowserPhase, frozenset[BrowserPhase]] = {
    "idle": frozenset({"starting", "failed", "closed"}),
    "starting": frozenset({"running", "offering", "failed", "closed"}),
    "offering": frozenset({"downloading", "failed", "closed"}),
    "downloading": frozenset({"idle", "failed", "closed"}),
    "running": frozenset({"closed"}),
    "failed": frozenset({"idle", "downloading", "closed"}),
    "closed": frozenset(),
}

PASSWORD_WARNING = "an agent opened this page: think twice before typing a password"
"""Shown while a page marked ``opened_by_agent=True`` has a password field focused."""

SCROLL_PIXELS = 40
"""How far one wheel event scrolls the page (trackpads send many small ones)."""

SCROLL_MERGE = 1 / 60
"""Wheel events this close together go to the page as one scroll."""


def _ignore(_: Any) -> None:
    """No-op callback once a view has closed."""


def _quietly_stop(browser: Any) -> None:
    """Stop a browser while cleaning up: a failure here mustn't hide the real one."""
    try:
        browser.stop()
    except Exception:
        pass


class BrowserHeader(Static):
    """🌐 the page's address, notes, keyboard indicator, and optional agent badge."""

    def __init__(self, url: str, opened_by_agent: bool = False) -> None:
        super().__init__(classes="browser-header", markup=False)
        self.opened_by_agent = opened_by_agent
        self.url, self.note, self.keyboard, self.warning = url, "", False, ""
        self.show(url)

    def show(self, url: str, note: str = "") -> None:
        self.url, self.note = url, note
        self._draw()

    def set_keyboard(self, keyboard: bool) -> None:
        self.keyboard = keyboard
        self._draw()

    def set_warning(self, warning: str) -> None:
        self.warning = warning
        self._draw()

    def _draw(self) -> None:
        parts: list[tuple[str, str]] = [("🌐 ", ""), (self.url, "bold")]
        if self.opened_by_agent:
            parts.append(("  opened by agent", "italic $warning"))
        if self.warning:
            parts.append((f"  ⚠ {self.warning}", "bold $error"))
        if self.note:
            parts.append((f"  {self.note}", "$text-muted"))
        if self.keyboard:
            parts.append(("  ⌨ keys go to the page · Esc to leave", "$text-accent"))
        self.update(Content.assemble(*parts))


class CloseButton(Static):
    """✕: close the view (and the browser with it)."""

    def __init__(self) -> None:
        super().__init__("✕ close", classes="browser-close")
        self.tooltip = "Close this browser view"

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.query_ancestor(BrowserView).close()


class DownloadOffer(Vertical):
    """There's no browser to drive: offer Google's Chrome headless shell."""

    DEFAULT_CSS = """
    DownloadOffer {
        height: auto;
        padding: 1 2;
        border: tall $warning 50%;
        & > .offer-text { color: $text; }
        & > .offer-buttons { height: 1; margin-top: 1; }
        & .offer-button { width: auto; padding: 0 1; text-style: bold; background: $primary 30%; margin-right: 2; }
        & .offer-button:hover { background: $primary 60%; }
        & .offer-later { background: $boost; text-style: none; }
        & > ProgressBar { display: none; margin-top: 1; }
        &.-downloading > ProgressBar { display: block; }
        &.-downloading > .offer-buttons { display: none; }
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "This browser view needs a Chromium-family browser (Chrome, Chromium, Edge or "
            "Brave), and there isn't one on this computer. It can download Google's Chrome "
            "headless shell for its own use: about 100 MB, kept in pixcell's cache folder. "
            "Your own browser isn't changed.",
            classes="offer-text",
        )
        with HorizontalGroup(classes="offer-buttons"):
            yield OfferButton("⬇ Download it", True, classes="offer-button")
            yield OfferButton("Not now", False, classes="offer-button offer-later")
        yield ProgressBar(total=None, show_eta=False)

    def answered(self, download: bool) -> None:
        self.query_ancestor(BrowserView).download_answered(download)

    def reset(self) -> None:
        self.remove_class("-downloading")
        self.query_one(ProgressBar).update(total=None, progress=0)

    def progress(self, done: int, total: int) -> None:
        bar = self.query_one(ProgressBar)
        if total:
            bar.update(total=total, progress=done)


class OfferButton(Static):
    """A choice in the download offer: click it, or focus it (Tab) and press Enter or Space."""

    can_focus = True
    BINDINGS = [("enter", "choose", "Choose"), ("space", "choose", "Choose")]
    DEFAULT_CSS = """
    OfferButton:focus { background: $primary 80%; text-style: bold underline; }
    """

    def __init__(self, label: str, download: bool, classes: str) -> None:
        super().__init__(label, classes=classes)
        self.download = download

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_choose()

    def action_choose(self) -> None:
        self.query_ancestor(DownloadOffer).answered(self.download)


@lru_cache(maxsize=1)
def _page_image_class() -> type:
    from pixcell.live import LiveImage

    class PageImage(LiveImage):
        """The page's picture: tells its view when its size (the page's layout) changes,
        and, once clicked (focused), sends the keyboard to the page."""

        # Only a click gives it the keyboard: it isn't focusable otherwise, so Textual
        # never moves focus to it on its own (on mount, or with Tab).
        can_focus = False
        # Presses are for the page, not a text selection / copy.
        ALLOW_SELECT = False

        def on_resize(self) -> None:  # (LiveImage's on_resize runs too)
            view = self.parent
            if isinstance(view, BrowserView):
                view.viewport_changed()

        def on_focus(self) -> None:
            if isinstance(self.parent, BrowserView):
                self.parent.keyboard_changed(True)

        def on_blur(self) -> None:
            self.can_focus = False
            if isinstance(self.parent, BrowserView):
                self.parent.keyboard_changed(False)

        def on_mouse_up(self, event: events.MouseUp) -> None:  # (LiveImage posts Clicked)
            if event.button == 1:
                self.take_keyboard()

        def take_keyboard(self) -> None:
            self.can_focus = True
            self.focus()

        def check_consume_key(self, key: str, character: str | None) -> bool:
            """Every key except Esc belongs to the page while focused, keeping app-wide
            priority bindings from stealing shortcuts."""
            return key != "escape"

        def on_paste(self, event: events.Paste) -> None:
            event.stop()
            if isinstance(self.parent, BrowserView) and event.text:
                self.parent.paste(event.text)

        async def on_key(self, event: events.Key) -> None:
            view = self.parent
            if not isinstance(view, BrowserView):
                return
            event.stop()
            event.prevent_default()  # every key is the page's (not Tab focus or bindings)
            if event.key == "escape":
                view.ask_to_leave()
                return
            view.send_key(event.key, event.character)

    return PageImage


class LeaveBrowserScreen(ModalScreen[bool]):
    """Esc in the browser view: leave it (Enter), or go back to the page (Esc)."""

    DEFAULT_CSS = """
    LeaveBrowserScreen {
        align: center middle;
        & > Vertical {
            width: auto;
            height: auto;
            padding: 1 3;
            border: thick $primary;
            background: $surface;
        }
        & Static { width: auto; }  /* the box fits its text (1fr would make both zero) */
        & .leave-title { text-style: bold; margin-bottom: 1; }
        & .leave-help { color: $text-muted; }
    }
    """

    BINDINGS = [
        ("enter", "leave", "Leave the page"),
        ("escape", "stay", "Back to the page"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Leave the browser view?", classes="leave-title")
            yield Static("Enter: give the keyboard back to the app", classes="leave-help")
            yield Static("Esc: back to the page", classes="leave-help")

    def action_leave(self) -> None:
        self.dismiss(True)

    def action_stay(self) -> None:
        self.dismiss(False)


class BrowserView(VerticalGroup):
    """A live headless-Chromium page streamed into a Textual app via :class:`LiveImage`."""

    DEFAULT_CLASSES = "block"
    DEFAULT_CSS = """
    BrowserView {
        height: auto;
        padding: 0 0 1 0;
        & > .browser-bar { height: 1; }
        & .browser-header { width: 1fr; color: $text; }
        & .browser-close { width: auto; color: $text-muted; text-style: underline; padding: 0 1; }
        & .browser-close:hover { color: $text; background: $boost; }
        & > LiveImage { width: 1fr; height: 24; }
        & > .browser-note { color: $text-muted; }
    }
    """

    @dataclass
    class PhaseChanged(Message):
        """The browser view moved to another phase (see :data:`BrowserPhase`)."""

        view: BrowserView
        previous: BrowserPhase
        phase: BrowserPhase

        @property
        def control(self) -> BrowserView:
            return self.view

    class Left(Message):
        """The user left the browser view (Esc, then Enter): focus something else."""

    def __init__(
        self,
        url: str,
        *,
        policy: Policy | Callable[[str], bool] | None = None,
        allow: Callable[[str], bool] | None = None,
        no_network: bool = False,
        opened_by_agent: bool = False,
        executable: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        show_bar: bool = True,
        allow_halfblock: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.url = url
        self.opened_by_agent = opened_by_agent
        self.policy, self.allow, self._no_network = resolve_policy(policy, allow, no_network)
        self._executable = executable
        self._cache_dir = cache_dir
        self._show_bar = show_bar
        self._allow_halfblock = allow_halfblock
        self._browser: Browser | None = None
        self._cleanups: list[Callable[[], None]] = []
        self._phase: BrowserPhase = "idle"
        self.started: Any = None
        self._new_started_future()
        self._viewport_size: tuple[int, int] | None = None
        self._applied_viewport_size: tuple[int, int] | None = None
        self._scroll_total = 0
        self._scroll_at = (0, 0)
        self._scroll_timer: Any = None
        self._scroller: ThreadPoolExecutor | None = None
        self._scroll_lock = threading.Lock()
        self._scroll_pending = (0, 0, 0)  # x, y, pixels: wheel input not yet sent
        self._scroll_busy = False  # a step is out (asking the page, or being sent)

    @classmethod
    def from_html(
        cls,
        doc: HTML,
        *,
        executable: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        show_bar: bool = True,
        allow_halfblock: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> BrowserView:
        """Create a :class:`BrowserView` for a :class:`pixcell.docs.HTML` document under the
        no-network policy (staged to a temp ``file://`` page cleaned up on unmount)."""
        uri, policy, cleanup = stage_html_document(doc)
        view = cls(
            uri,
            policy=policy,
            executable=executable,
            cache_dir=cache_dir,
            show_bar=show_bar,
            allow_halfblock=allow_halfblock,
            name=name,
            id=id,
            classes=classes,
        )
        view.add_cleanup(cleanup)
        return view

    def add_cleanup(self, callback: Callable[[], None]) -> None:
        """Register a cleanup callback to run when this view closes."""
        if self._phase == "closed":
            _quietly_call(callback)
        else:
            self._cleanups.append(callback)

    @property
    def browser(self) -> Browser | None:
        """The running :class:`~pixcell.browser.Browser`, once started."""
        return self._browser

    @property
    def phase(self) -> BrowserPhase:
        """Where this browser view is in its life (see :data:`BrowserPhase`)."""
        return self._phase

    def _set_phase(self, phase: BrowserPhase) -> bool:
        """Move to ``phase``, if that's a legal move; the only place the phase changes."""
        previous = self._phase
        if phase not in _PHASE_MOVES[previous]:
            log.debug(f"{type(self).__name__}: phase {previous} -> {phase} refused")
            return False
        self._phase = phase
        self.post_message(self.PhaseChanged(self, previous, phase))
        return True

    def _new_started_future(self) -> None:
        try:
            import asyncio

            self.started = asyncio.get_running_loop().create_future()
        except RuntimeError:  # made outside the event loop (tests): on mount, then
            self.started = None

    def _can_draw(self) -> bool:
        if has_graphics():
            return True
        if self._allow_halfblock:
            from pixcell.textual import capabilities

            return capabilities().protocol != "off"
        return False

    def compose(self) -> ComposeResult:
        PageImage = _page_image_class()

        if self._show_bar:
            with HorizontalGroup(classes="browser-bar"):
                yield BrowserHeader(self.url, self.opened_by_agent)
                yield CloseButton()
        if not self._can_draw():
            yield Static(
                "The browser view needs a terminal with graphics (Kitty, iTerm2 or Sixel).",
                classes="browser-note",
            )
            return
        yield PageImage((1280, 800), fps=60)

    def _viewport(self) -> tuple[int, int] | None:
        """The live picture's box in pixels: the page is laid out at exactly that size."""
        from pixcell.live import LiveImage
        from pixcell.textual import cell_size

        try:
            live = self.query_one(LiveImage)
        except Exception:
            return None
        columns, rows = live.content_size
        if columns <= 0 or rows <= 0:
            return None
        cell_width, cell_height = cell_size()
        return columns * cell_width, rows * cell_height

    def on_mount(self) -> None:
        import asyncio

        if self.started is None:
            self.started = asyncio.get_running_loop().create_future()
        if not self._can_draw():
            self._started_with(False, "this terminal can't show pictures")

    def _started_with(self, ok: bool, note: str = "") -> None:
        if ok:
            if not self._set_phase("running"):
                return
            if (
                self._browser is not None
                and self._viewport_size is not None
                and self._viewport_size != self._applied_viewport_size
            ):
                self._resize(self._viewport_size)
        elif self._phase not in ("failed", "closed"):
            self._set_phase("failed")
        if self.started is not None and not self.started.done():
            self.started.set_result((ok, note))

    def on_resize(self) -> None:
        self.viewport_changed()

    def viewport_changed(self) -> None:
        size = self._viewport()
        if size is None or size == self._viewport_size:
            return
        self._viewport_size = size
        if self._phase == "idle":
            if self._set_phase("starting"):
                self._start(size)
        elif self._phase == "running" and self._browser is not None:
            self._resize(size)

    @work(thread=True, exclusive=True, group="browser-start")
    def _start(self, size: tuple[int, int]) -> None:
        from pixcell.browser import cdp, install
        from pixcell.live import LiveImage

        if self._phase == "closed":
            return
        executable = self._executable or _call_find_browser(install.find_browser, self._cache_dir)
        if executable is None:
            if clear := getattr(install.find_browser, "cache_clear", None):
                clear()
            executable = _call_find_browser(install.find_browser, self._cache_dir)
        if executable is None:
            if install.platform_name() is not None:
                self._from_thread(self._offer_download)
                return
            self._from_thread(self._note, "no Chrome, Chromium, Edge or Brave found")
            self._from_thread(self._started_with, False, "no Chrome, Chromium, Edge or Brave found")
            return
        if self._phase == "closed":
            return
        try:
            live = self.query_one(LiveImage)
        except Exception:
            return
        extra: dict[str, Any] = {}
        if self._no_network:
            extra["no_network"] = True
        browser = cdp.Browser(
            executable,
            on_frame=live.push,  # thread-safe; the newest frame wins
            on_url=lambda url: self._from_thread(self._show_url, url),
            on_note=lambda note: self._from_thread(self._note, note),
            allow=self.allow,
            on_field=(lambda kind: self._from_thread(self._field_focused, kind)) if self.opened_by_agent else None,
            **extra,
        )
        self._browser = browser  # so closing during startup can stop it
        try:
            browser.start(self.url, size)
            applied = size
            if (
                self._phase != "closed"
                and (latest := self._viewport_size) is not None
                and latest != applied
            ):
                browser.resize(latest)  # resized while it was starting
                applied = latest
            self._applied_viewport_size = applied
            self._from_thread(self._started_with, True)
        except Exception as error:  # BrowserError, or anything else: say so, don't crash
            if self._browser is browser:
                self._browser = None
            _detach_browser(browser)
            if self._phase != "closed":  # reported first: stopping mustn't be able to swallow it
                self._from_thread(self._note, str(error))
                self._from_thread(self._started_with, False, str(error))
            _quietly_stop(browser)
        finally:
            if self._phase == "closed":
                if self._browser is browser:
                    self._browser = None
                _detach_browser(browser)
                _quietly_stop(browser)

    def _offer_download(self) -> None:
        """No browser to drive: offer to download Chrome headless shell."""
        if not self._set_phase("offering"):
            return
        if not self.query(DownloadOffer):
            bars = list(self.query(".browser-bar"))
            if bars:
                self.mount(DownloadOffer(), after=bars[0])
            else:
                self.mount(DownloadOffer())
        self._note("no browser to show pages with")

    def download_answered(self, download: bool) -> None:
        if not download:
            if self._phase not in ("offering", "failed"):
                return
            self.query(DownloadOffer).remove()
            self._note("no browser: Chrome, Chromium, Edge or Brave, or download one")
            if self._phase == "offering":
                self._started_with(False, "the user chose not to download a browser")
            return
        if not self._set_phase("downloading"):
            return
        if self.started is None or self.started.done():
            self._new_started_future()
        for offer in self.query(DownloadOffer):
            offer.add_class("-downloading")
        self._note("")
        self._download()

    @work(thread=True, exclusive=True, group="browser-download")
    def _download(self) -> None:
        from pixcell.browser import install

        def progress(done: int, total: int) -> None:
            self._from_thread(self._download_progress, done, total)

        try:
            _call_install(install.install, progress, self._cache_dir)
        except install.InstallError as error:
            self._from_thread(self._download_failed, str(error))
            return
        self._from_thread(self._downloaded)

    def _download_progress(self, done: int, total: int) -> None:
        for offer in self.query(DownloadOffer):
            offer.progress(done, total)

    def _download_failed(self, reason: str) -> None:
        if self._phase != "downloading":
            return
        for offer in self.query(DownloadOffer):
            offer.reset()
        self._note(f"couldn't download a browser: {reason}")
        self._started_with(False, f"couldn't download a browser ({reason})")

    def _downloaded(self) -> None:
        if not self._set_phase("idle"):
            return
        self.query(DownloadOffer).remove()
        self._note("")
        self._viewport_size = None
        self._applied_viewport_size = None
        self.call_after_refresh(self.viewport_changed)  # now start it

    def retry(self) -> bool:
        """Try starting the browser again after a failure."""
        if not self._set_phase("idle"):
            return False
        self.query(DownloadOffer).remove()
        self._note("")
        self._new_started_future()
        self._viewport_size = None
        self._applied_viewport_size = None
        if self.is_mounted:
            self.call_after_refresh(self.viewport_changed)
        return True

    def _from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        if self._phase != "closed":
            try:
                self.app.call_from_thread(callback, *args)
            except Exception:
                pass  # the app is closing

    @work(thread=True, exclusive=True, group="browser-resize")
    def _resize(self, size: tuple[int, int]) -> None:
        if self._phase == "running" and self._browser is not None:
            try:
                self._browser.resize(size)
                self._applied_viewport_size = size
            except Exception:
                pass

    def _input(self, send: Callable[[Any], None]) -> None:
        """Forward input to the page; a stopped browser is a note, not an error."""
        if self._phase != "running" or self._browser is None:
            return
        try:
            send(self._browser)
        except BrowserError as error:
            self._note(str(error))

    def _show_url(self, url: str) -> None:
        if url.startswith("chrome-error://"):
            return  # keep the address that failed (and the reason, from on_note)
        self.url = url
        if (header := self._header()) is not None:
            header.show(url)

    def _note(self, note: str) -> None:
        if (header := self._header()) is not None:
            header.show(self.url, note)

    def _header(self) -> BrowserHeader | None:
        """The header, or None while the view is being taken down (or ``show_bar=False``)."""
        try:
            return self.query_one(BrowserHeader)
        except Exception:
            return None

    def keyboard_changed(self, keyboard: bool) -> None:
        if (header := self._header()) is not None:
            header.set_keyboard(keyboard)

    def _warn(self, warning: str) -> None:
        if (header := self._header()) is not None:
            header.set_warning(warning)

    def send_key(self, key: str, character: str | None) -> None:
        if (press := press_for(key, character)) is not None:
            self._input(
                lambda browser: browser.key(
                    press.key,
                    text=press.text,
                    code=press.code,
                    key_code=press.key_code,
                    modifiers=press.modifiers,
                )
            )

    def paste(self, text: str) -> None:
        self._input(lambda browser: browser.insert_text(text))

    def ask_to_leave(self) -> None:
        def answered(leave: bool | None) -> None:
            if not leave:
                self._page_image().take_keyboard()  # back to the page
                return
            self._page_image().blur()
            self.post_message(self.Left())

        self.app.push_screen(LeaveBrowserScreen(), answered)

    def _page_image(self) -> Any:
        return self.query_one(_page_image_class())

    def on_live_image_clicked(self, message: Any) -> None:
        self._drop_pending_scroll()  # a click moves on: older wheel input is stale
        self._input(lambda browser: browser.click(message.x, message.y))
        if self.opened_by_agent and getattr(self._browser, "on_field", None) is None:
            # A browser that watches fields reports every focus change in order; a check
            # here as well could land after a newer report and undo it.
            self._check_password_field()

    def _field_focused(self, kind: str) -> None:
        """An agent's page focused a field (by a click, Tab, autofocus or a script)."""
        if self.opened_by_agent:
            self._warn(PASSWORD_WARNING if kind == "password" else "")

    @work(thread=True, exclusive=True, group="browser-field")
    def _check_password_field(self) -> None:
        """A page an agent opened: warn when a click lands in a password field."""
        import time

        time.sleep(0.15)  # the click has to land first
        browser = self._browser
        if self._phase != "running" or browser is None:
            return
        try:
            field = browser.focused_field_type()
        except Exception:
            return
        warning = PASSWORD_WARNING if field == "password" else ""
        self._from_thread(self._warn, warning)

    def on_live_image_scrolled(self, message: Any) -> None:
        if self._phase != "running" or self._browser is None:
            return
        # Merge a burst of wheel events into one scroll, sent once per frame or so.
        self._scroll_at = (message.x, message.y)
        self._scroll_total += message.delta * SCROLL_PIXELS
        if self._scroll_timer is None:
            self._scroll_timer = self.set_timer(SCROLL_MERGE, self._send_scroll)

    def _send_scroll(self) -> None:
        self._scroll_timer = None
        total, self._scroll_total = self._scroll_total, 0
        browser = self._browser
        if not total or self._phase != "running" or browser is None:
            return
        # Off the UI thread (it asks the page first), one step at a time. While a step is
        # out, newer wheel input merges into one pending step: a busy page gets the sum
        # once it answers, not a backlog replayed after the user has stopped.
        with self._scroll_lock:
            self._scroll_pending = (*self._scroll_at, self._scroll_pending[2] + total)
            if self._scroll_busy:
                return
            self._scroll_busy = True
        if self._scroller is None:
            self._scroller = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-scroll")
        self._scroller.submit(self._scroll_in_thread, browser)

    def _drop_pending_scroll(self) -> None:
        with self._scroll_lock:
            self._scroll_pending = (0, 0, 0)

    def _scroll_in_thread(self, browser: Any) -> None:
        while True:
            with self._scroll_lock:
                x, y, total = self._scroll_pending
                self._scroll_pending = (0, 0, 0)
                if not total or self._phase != "running":
                    self._scroll_busy = False
                    return
            try:
                browser.scroll(x, y, total)
            except BrowserError as error:
                self._from_thread(self._note, str(error))
            except Exception:
                pass

    @work(thread=True, exclusive=True, group="browser-nav")
    def _navigate_in_thread(self, url: str) -> None:
        browser = self._browser
        if self._phase != "running" or browser is None:
            return
        try:
            browser.navigate(url)
        except BrowserError as error:
            self._from_thread(self._note, str(error))

    def navigate(self, url: str) -> None:
        """Open ``url`` in the running browser."""
        self.url = url
        if self._phase == "running" and self._browser is not None:
            self._navigate_in_thread(url)

    def reload(self) -> None:
        """Reload the current page, skipping cache."""
        self._input(lambda browser: browser.reload())

    @property
    def is_local(self) -> bool:
        """Showing a local file or this machine (a dev server): worth reloading on edits."""
        return self.url.startswith("file:") or host_of(self.url) in LOCAL_HOSTS

    def reload_if_local(self) -> None:
        if self.is_local:
            self._input(lambda browser: browser.reload())

    def close(self) -> None:
        self.remove()

    def on_unmount(self) -> None:
        self._set_phase("closed")
        self._started_with(False, "the view was closed")  # nobody waits out a timeout
        if self._scroll_timer is not None:
            self._scroll_timer.stop()
            self._scroll_timer = None
        if self._scroller is not None:
            self._scroller.shutdown(wait=False, cancel_futures=True)
            self._scroller = None
        browser, self._browser = self._browser, None
        cleanups = list(self._cleanups)
        self._cleanups.clear()
        if browser is not None:
            _detach_browser(browser)
        if browser is not None or cleanups:
            threading.Thread(target=_stop_and_cleanup, args=(browser, cleanups), daemon=True).start()


def _detach_browser(browser: Any) -> None:
    with suppress_all():
        browser.on_frame = _ignore
        browser.on_url = _ignore
        browser.on_note = _ignore
        if getattr(browser, "on_field", None) is not None:
            browser.on_field = _ignore


def _quietly_call(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        pass


def _stop_and_cleanup(browser: Any, cleanups: list[Callable[[], None]]) -> None:
    if browser is not None:
        _quietly_stop(browser)
    for cleanup in cleanups:
        _quietly_call(cleanup)


class suppress_all:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool:
        return True


def _call_find_browser(fn: Callable[..., str | None], cache_dir: str | os.PathLike[str] | None) -> str | None:
    """Call ``find_browser`` passing ``cache_dir`` only when given (keeps zero-arg test doubles working)."""
    if cache_dir is None:
        return fn()
    return fn(cache_dir=cache_dir)


def _call_install(
    fn: Callable[..., str],
    progress: Callable[[int, int], None],
    cache_dir: str | os.PathLike[str] | None,
) -> str:
    """Call ``install`` passing ``cache_dir`` only when given (keeps single-arg test doubles working)."""
    if cache_dir is None:
        return fn(progress)
    return fn(progress, cache_dir=cache_dir)
