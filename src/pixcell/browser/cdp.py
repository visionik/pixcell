"""A small Chrome DevTools Protocol client over ``--remote-debugging-pipe`` (stdlib only).

Ported from ``deftai/console`` (MIT).

Chrome reads commands on file descriptor 3 and writes replies and events on 4, as JSON
messages ended by a NUL byte. No port is opened, so nothing else on the machine can talk
to (or take over) the browser.

:class:`Browser` runs headless in a throwaway profile, with downloads denied, pop-ups opened
in the same page, and permission prompts refused. It works both headlessly without Textual
(:meth:`Browser.navigate`, :meth:`Browser.screenshot`, :meth:`Browser.eval`,
:meth:`Browser.close`) and as the streaming backend for :class:`~pixcell.browser.BrowserView`
when ``on_frame`` is provided. When given a :class:`~pixcell.browser.Policy` (or ``allow``
callable), every document the main frame loads, each redirect, and each pop-up is checked.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import os
import re
import secrets
import select
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, overload

from pixcell.browser import _portability
from pixcell.browser.install import find_browser
from pixcell.browser.policy import Policy

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from pixcell.docs.types import HTML

TIMEOUT = 15.0
"""Seconds to wait for a command's reply."""

CHECK_TIMEOUT = 0.25
"""Seconds a scroll step waits to learn whether the page can use it (then it's sent)."""

DEAD_PROXY = "127.0.0.1:9"
"""The discard port: nothing listens, so every network request sent there fails."""

NO_NETWORK_FLAGS = (
    f"--proxy-server={DEAD_PROXY}",
    "--proxy-bypass-list=<-loopback>",  # loopback through the proxy too (bypassed by default)
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",  # no WebRTC UDP around the proxy
    "--disable-background-networking",
    "--disable-component-update",
    "--no-pings",
    "--dns-prefetch-disable",
)
"""Browser flags that keep both the page and Chrome itself off the network."""

_CAN_SCROLL = """(x, y, dy) => {
  // Whenever this can't tell (a cross-origin frame, a closed shadow root, an error), the
  // answer is yes: dropping a step that mattered is worse than one bounce.
  const UNSURE = true;
  const wheeled = (node) => {
    try {
      const listeners = getEventListeners(node);
      return !!(listeners.wheel || listeners.mousewheel);
    } catch (_) { return UNSURE; }
  };
  const movable = (el, doc) => {
    const style = doc.defaultView.getComputedStyle(el);
    if (el !== doc.scrollingElement && !/(auto|scroll|overlay)/.test(style.overflowY)) return false;
    const max = el.scrollHeight - el.clientHeight;
    if (max <= 1) return false;
    const top = el.scrollTop;
    if (style.flexDirection === "column-reverse") {
      return dy < 0 ? top > -max + 1 : top < -0.5;  // 0 at the bottom, negative upwards
    }
    return dy < 0 ? top > 0.5 : top < max - 1;
  };
  const hit = (doc, x, y) => {  // the element under the point, inside open shadow roots
    let el = doc.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  };
  const check = (doc, x, y, depth) => {
    const start = hit(doc, x, y);
    if (!start) return UNSURE;
    if (start.localName === "iframe" || start.localName === "frame") {
      let inner = null;
      try { inner = start.contentDocument; } catch (_) {}
      if (!inner || depth > 5) return UNSURE;  // cross-origin: can't look inside
      const box = start.getBoundingClientRect();
      if (check(inner, x - box.left - start.clientLeft, y - box.top - start.clientTop, depth + 1)) return true;
    }
    for (let el = start; el; el = el.parentElement || (el.getRootNode() && el.getRootNode().host)) {
      if (wheeled(el) || movable(el, el.ownerDocument)) return true;
      if (el.localName.includes("-") && !el.shadowRoot && customElements.get(el.localName)) {
        return UNSURE;  // a component that may have a closed shadow root
      }
    }
    if (doc.scrollingElement && movable(doc.scrollingElement, doc)) return true;
    return wheeled(doc) || wheeled(doc.defaultView);
  };
  try { return check(document, x, y, 0); } catch (_) { return UNSURE; }
}"""
"""JavaScript: would a wheel step of ``dy`` at ``x, y`` do anything? From the element there
(inside open shadow roots and same-origin frames) up: a scroller with room that way, or a
wheel listener. Yes when it can't tell."""

_FOCUSED_FIELD = """(() => {
  let e = document.activeElement;
  for (let i = 0; e && i < 20; i++) {
    if (e.shadowRoot && e.shadowRoot.activeElement) { e = e.shadowRoot.activeElement; continue; }
    if (e.tagName === "IFRAME" || e.tagName === "FRAME") {
      let inner = null;
      try { inner = e.contentDocument && e.contentDocument.activeElement; } catch (_) {}
      if (inner && inner !== e.contentDocument.body) { e = inner; continue; }
    }
    break;
  }
  return (e && e.type) || "";
})()"""
"""JavaScript: the type of the element that really has focus (into shadow roots and frames)."""

_WATCH_FIELDS = """(() => {
  // In an isolated world of pixcell's own (the page's scripts can't see it, call its
  // binding, or replace what it calls: composedPath, the type getter...), added before them.
  const send = window[%(name)s];
  if (typeof send !== "function") return;
  // A focused field can also turn into a password field without losing focus (its type
  // changed by a script): watched while it has focus.
  const watch = new MutationObserver(() => send(String((focused && focused.type) || "")));
  let focused = null;
  // Focus moving between two fields inside one shadow root isn't seen outside it: each
  // open shadow root focus goes into is listened to as well.
  const listened = new WeakSet();
  const listen = (where) => {
    listened.add(where);
    where.addEventListener("focusin", report, true);
    where.addEventListener("focusout", report, true);
  };
  const report = (event) => {
    if (!event.isTrusted) return;  // the page's own made-up events don't count
    const path = event.composedPath();
    for (const node of path) {
      if (node instanceof ShadowRoot && !listened.has(node)) listen(node);
    }
    watch.disconnect();
    focused = event.type === "focusin" ? path[0] || null : null;
    if (focused && focused.nodeType === 1) watch.observe(focused, { attributes: true, attributeFilter: ["type"] });
    send(String((focused && focused.type) || ""));
  };
  listen(window);
})()"""
"""JavaScript run in every document of a watched page, in an isolated world: reports the
type of each field that gets focus however it got it (autofocus, Tab, a script), and again
if the focused field's type changes, through a binding only that world has."""

BLOCKED_REASON = "BlockedByClient"
"""What a page that wasn't allowed sees (``net::ERR_BLOCKED_BY_CLIENT``)."""

_DOCUMENTS = {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}
"""The requests (``Fetch.enable`` patterns) that wait for the gate: every document."""


class BrowserError(Exception):
    """The browser couldn't start, or a command failed."""


def resolve_policy(
    policy: Policy | Callable[[str], bool] | None,
    allow: Callable[[str], bool] | None,
    no_network: bool,
) -> tuple[Policy | None, Callable[[str], bool] | None, bool]:
    """Combine ``policy``, ``allow`` and ``no_network`` into ``(policy_obj, allow_fn, is_no_network)``."""
    if no_network and policy is None:
        policy = Policy.offline()
    if isinstance(policy, Policy):
        is_offline = bool(no_network or policy.no_network)
        if allow is not None:
            combined: Callable[[str], bool] | None = lambda url: policy.allows(url) and bool(allow(url))
        else:
            combined = policy.allows
        return policy, combined, is_offline
    if policy is not None and allow is not None:
        combined = lambda url: bool(policy(url)) and bool(allow(url))
    else:
        combined = policy if policy is not None else allow
    return None, combined, bool(no_network)


class Browser:
    """One headless Chromium browser, showing one page.

    Works standalone without Textual::

        with Browser() as browser:
            browser.navigate("https://example.com")
            title = browser.eval("document.title")
            image = browser.screenshot()          # PIL.Image.Image
            png = browser.screenshot(format="png")

    When ``on_frame`` is provided (as :class:`~pixcell.browser.BrowserView` does), Chrome
    streams screencast frames to ``on_frame`` on a background reader thread; ``on_frame``
    and ``on_url`` must hand work to the UI thread themselves.
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        url: str = "",
        size: tuple[int, int] = (1280, 800),
        policy: Policy | Callable[[str], bool] | None = None,
        allow: Callable[[str], bool] | None = None,
        no_network: bool = False,
        on_frame: Callable[[Any], None] | None = None,
        on_url: Callable[[str], None] | None = None,
        on_note: Callable[[str], None] | None = None,
        on_field: Callable[[str], None] | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.executable = executable
        self.on_frame = on_frame
        self.on_url = on_url or (lambda url: None)
        self.on_note = on_note or (lambda note: None)
        """Called with why a page didn't load (e.g. ``net::ERR_NAME_NOT_RESOLVED``)."""
        self.policy, self.allow, self._no_network = resolve_policy(policy, allow, no_network)
        self.on_field = on_field
        self._cache_dir = cache_dir
        self._binding = f"__pixcell_field_{secrets.token_hex(8)}"
        self._world = f"pixcell-fields-{secrets.token_hex(8)}"
        """The isolated world the field watcher runs in (the page's scripts are elsewhere)."""
        self._subframes: set[str] = set()  # frames inside the page (Page.frameAttached)
        self.url = url
        self._ids = itertools.count(1)
        self._waiting: dict[int, Future[dict[str, Any]]] = {}
        self._waiting_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._load_event = threading.Event()
        self._cleanups: list[Callable[[], None]] = []
        self._session = ""
        self._target = ""
        self._pid = 0
        self._stopped = False  # stop() was called (perhaps while starting)
        self._dead = False  # the browser exited or its pipe broke
        self._popups: set[str] = set()  # pop-up pages waiting for an address
        self._popup_sessions: dict[str, str] = {}  # pop-ups held for the gate: session -> target
        self._popups_done: set[str] = set()  # pop-ups already closed (and opened here, or refused)
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._commands = self._replies = -1
        self._reader: threading.Thread | None = None  # owns (and closes) the replies pipe
        self._wake = -1  # written by stop() to end the reader (POSIX: see _read)
        self.size = size

    def add_cleanup(self, callback: Callable[[], None]) -> None:
        """Register a cleanup callback to run once when the browser closes."""
        with self._waiting_lock:
            stopped = self._stopped
            if not stopped:
                self._cleanups.append(callback)
        if stopped:
            with suppress(Exception):
                callback()

    # Starting and stopping ---------------------------------------------------------------

    def start(self, url: str = "about:blank", size: tuple[int, int] | None = None) -> None:
        """Launch the browser, open ``url`` at ``size`` (CSS pixels), and stream frames when
        ``on_frame`` is set. Blocking."""
        target_size = size if size is not None else self.size
        if url != "about:blank" and not self._allowed(url):
            self._blocked(url)
            raise BrowserError(f"didn't open {url}: blocked by URL policy")
        executable = self.executable or find_browser(cache_dir=self._cache_dir)
        if not executable:
            self.stop()
            raise BrowserError("no Chrome, Chromium, Edge or Brave found")
        self.executable = executable
        # Ignoring errors: on Windows a browser process can hold its files for a moment after
        # it's killed, and a leftover temp folder shouldn't make closing the page fail.
        self._profile = tempfile.TemporaryDirectory(prefix="pixcell-browser-", ignore_cleanup_errors=True)
        arguments = [
            executable,
            "--headless",
            "--remote-debugging-pipe",
            f"--user-data-dir={self._profile.name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--mute-audio",
            "--deny-permission-prompts",
            "--hide-scrollbars",
            *(NO_NETWORK_FLAGS if self._no_network else ()),
            f"--window-size={target_size[0]},{target_size[1]}",
            "about:blank",
        ]
        # Its own process group, so it's stopped whole (Chrome starts helpers), with the
        # pipe on the descriptors --remote-debugging-pipe uses.
        try:
            pid, commands, replies = _portability.spawn_with_pipe(executable, arguments)
        except OSError as error:
            self.stop()
            raise BrowserError(f"couldn't start the browser ({error.strerror or error})") from None
        with self._waiting_lock:
            self._pid, self._commands, self._replies = pid, commands, replies
            stopped = self._stopped
            if not stopped:  # from now on the reader closes the replies pipe, not stop()
                wake = -1
                if os.name != "nt":  # (select doesn't take pipes on Windows)
                    wake, self._wake = os.pipe()
                self._reader = threading.Thread(
                    target=self._read, args=(replies, wake), name="pixcell-browser-reader", daemon=True
                )
                self._reader.start()
        # stop() closes the pipe.
        if stopped:  # closed while it was starting
            self.stop()
            raise BrowserError("closed")

        self.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        self.call("Target.setDiscoverTargets", {"discover": True})
        self._target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self._session = self.call("Target.attachToTarget", {"targetId": self._target, "flatten": True})["sessionId"]
        self.page("Page.enable")
        if self.allow is not None:
            # Every document the page loads waits here to be checked (see _gate).
            self.page("Fetch.enable", {"patterns": [_DOCUMENTS]})
            # And every page opened later (a pop-up) starts held, until its documents wait
            # there too (see _popup_attached): closing it once it shows an address would be
            # too late, as its request would already have gone out.
            self.call(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True, "filter": [{"type": "page"}]},
            )
        if self.on_field is not None:
            self.page("Runtime.enable")  # (bindings only report with it on)
            self.page("Runtime.addBinding", {"name": self._binding, "executionContextName": self._world})
            self.page(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _WATCH_FIELDS % {"name": json.dumps(self._binding)}, "worldName": self._world},
            )
        self.resize(target_size)
        self.navigate(url)

    def _ensure_started(self) -> None:
        if self.on_frame is None and not self._session and not self._stopped and not self._dead:
            self.start(self.url or "about:blank", self.size)

    def stop(self) -> None:
        """Close the browser and remove its profile. Safe at any time, more than once,
        even while :meth:`start` is still running (it then stops when it can)."""
        with self._stop_lock:
            with self._waiting_lock:
                self._stopped = True
                pid, self._pid = self._pid, 0
                commands, replies = self._commands, self._replies
                self._commands = self._replies = -1
                profile, self._profile = self._profile, None
                reader = self._reader
                wake, self._wake = self._wake, -1
                cleanups = list(self._cleanups)
                self._cleanups.clear()
            self._load_event.set()
            if pid:
                _portability.stop_spawned(pid, grace=3.0)
            if wake >= 0:
                # Ends the reader even if something still holds Chrome's end of the pipe
                # (a helper that outlived it), so the reader closes its pipe now.
                with suppress(OSError):
                    os.write(wake, b"x")  # (fails if the reader has already ended: that's fine)
                with suppress(OSError):
                    os.close(wake)
            if reader is None:
                if replies >= 0:
                    with suppress(OSError):
                        os.close(replies)
            elif reader is not threading.current_thread():
                # The reader closes its pipe when it ends (Chrome's exit ends it). Closing
                # it here, while it may be in os.read, could let a file opened meanwhile
                # take the same number, and the reader would read that instead.
                reader.join(timeout=3.0)
            self._fail_waiting("the browser stopped")
            if commands >= 0:
                with self._write_lock, suppress(OSError):
                    os.close(commands)
            if profile is not None:
                profile.cleanup()
            for cleanup in cleanups:
                with suppress(Exception):
                    cleanup()

    def close(self) -> None:
        """Close the browser and clean up its temporary profile (alias for :meth:`stop`)."""
        self.stop()

    def __enter__(self) -> Self:
        self._ensure_started()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _fail_waiting(self, reason: str) -> None:
        with self._waiting_lock:
            waiting = list(self._waiting.values())
            self._waiting.clear()
        for future in waiting:
            if not future.done():
                with suppress(Exception):
                    future.set_exception(BrowserError(reason))

    # Commands ---------------------------------------------------------------------------

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, session: str = "", wait: bool = True, timeout: float = 0
    ) -> dict[str, Any]:
        """Send a command (to the page when ``session`` is given) and wait for its reply."""
        message: dict[str, Any] = {"id": next(self._ids), "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future: Future[dict[str, Any]] = Future()
        data = json.dumps(message).encode() + b"\0"
        died = False
        with self._write_lock:
            with self._waiting_lock:
                commands = self._commands
                if commands < 0 or self._dead or self._stopped:
                    raise BrowserError("the browser isn't running")
                if wait:
                    self._waiting[message["id"]] = future
            view = memoryview(data)
            try:
                while view:
                    view = view[os.write(commands, view) :]
            except OSError:  # its end of the pipe is gone: it exited
                with self._waiting_lock:
                    self._waiting.pop(message["id"], None)
                died = True
        if died:
            self._died()
            raise BrowserError("the browser stopped") from None
        if not wait:
            return {}
        try:
            return future.result(timeout=timeout or TIMEOUT)
        except TimeoutError:
            with self._waiting_lock:
                self._waiting.pop(message["id"], None)
            raise BrowserError(f"{method}: no answer from the browser") from None

    def page(
        self, method: str, params: dict[str, Any] | None = None, *, wait: bool = True, timeout: float = 0
    ) -> dict[str, Any]:
        session = self._session
        if not session:
            raise BrowserError("the browser isn't ready")
        return self.call(method, params, session=session, wait=wait, timeout=timeout)

    def navigate(self, url: str, *, wait: bool = True, timeout: float = 0) -> None:
        """Open ``url`` in the page. Starts the browser first when used headlessly."""
        if self.on_frame is None and not self._session and not self._stopped and not self._dead and wait:
            self.start(url, self.size)
            return
        if url != "about:blank" and not self._allowed(url):
            self._blocked(url)
            if self.on_frame is None and wait:
                raise BrowserError(f"didn't open {url}: blocked by URL policy")
            return
        self.url = url
        self.on_url(url)
        if wait and self.on_frame is None:
            self._load_event.clear()
        result = self.page("Page.navigate", {"url": url}, wait=wait, timeout=timeout)
        if error := result.get("errorText"):
            self.on_note(f"{url}: {error}")
            if self.on_frame is None and wait:
                raise BrowserError(f"{url}: {error}")
            return
        if wait and self.on_frame is None and url != "about:blank" and result.get("loaderId"):
            self._load_event.wait(timeout=timeout or TIMEOUT)
            if self._dead or self._stopped:
                raise BrowserError("the browser stopped")

    def eval(self, expression: str, *, await_promise: bool = True, timeout: float = 0) -> Any:
        """Evaluate JavaScript ``expression`` in the page and return its JSON-serialisable value."""
        self._ensure_started()
        result = self.page(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        if details := result.get("exceptionDetails"):
            exc = details.get("exception") or {}
            description = exc.get("description") or exc.get("value") or details.get("text") or "JavaScript error"
            raise BrowserError(str(description))
        return result.get("result", {}).get("value")

    @overload
    def screenshot(
        self,
        *,
        format: Literal["pil"] = "pil",
        full_page: bool = False,
        path: str | os.PathLike[str] | None = None,
        timeout: float = 0,
    ) -> PILImage: ...

    @overload
    def screenshot(
        self,
        *,
        format: Literal["png"],
        full_page: bool = False,
        path: str | os.PathLike[str] | None = None,
        timeout: float = 0,
    ) -> bytes: ...

    def screenshot(
        self,
        *,
        format: Literal["pil", "png"] = "pil",
        full_page: bool = False,
        path: str | os.PathLike[str] | None = None,
        timeout: float = 0,
    ) -> PILImage | bytes:
        """Capture a screenshot of the current page as a Pillow ``Image`` (``format="pil"``)
        or PNG ``bytes`` (``format="png"``), optionally saving PNG bytes to ``path``."""
        if format not in ("pil", "png"):
            raise ValueError(f"unsupported screenshot format {format!r} (expected 'pil' or 'png')")
        self._ensure_started()
        result = self.page(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            timeout=timeout,
        )
        png_bytes = base64.b64decode(result["data"])
        if path is not None:
            Path(path).write_bytes(png_bytes)
        if format == "png":
            return png_bytes
        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes))
        image.load()
        return image

    def screenshot_png(
        self,
        *,
        full_page: bool = False,
        path: str | os.PathLike[str] | None = None,
        timeout: float = 0,
    ) -> bytes:
        """Capture a PNG screenshot of the current page as ``bytes``."""
        return self.screenshot(format="png", full_page=full_page, path=path, timeout=timeout)

    def resize(self, size: tuple[int, int]) -> None:
        """Lay the page out for ``size`` CSS pixels, and stream frames at that size when
        ``on_frame`` is set."""
        self.size = size
        self.page(
            "Emulation.setDeviceMetricsOverride",
            {"width": size[0], "height": size[1], "deviceScaleFactor": 1, "mobile": False},
        )
        if self.on_frame is not None:
            self.page("Page.stopScreencast")
            self.page(
                "Page.startScreencast",
                {"format": "jpeg", "quality": 80, "maxWidth": size[0], "maxHeight": size[1], "everyNthFrame": 1},
            )

    def reload(self) -> None:
        """Load the page again, skipping the cache (e.g. after its files changed)."""
        self.page("Page.reload", {"ignoreCache": True}, wait=False)

    def click(self, x: int, y: int, button: str = "left") -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.page(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": button, "clickCount": 1},
                wait=False,
            )

    def key(self, key: str, *, text: str = "", code: str = "", key_code: int = 0, modifiers: int = 0) -> None:
        """Press and release a key in the page (``Input.dispatchKeyEvent``). ``text`` is what
        it types, if anything; ``key_code`` is the Windows virtual key code special keys
        need (Enter, arrows...); ``modifiers``: Alt 1, Ctrl 2, Meta 4, Shift 8."""
        down: dict[str, object] = {"type": "keyDown" if text else "rawKeyDown", "key": key, "modifiers": modifiers}
        if text:
            down["text"] = down["unmodifiedText"] = text
        if code:
            down["code"] = code
        if key_code:
            down["windowsVirtualKeyCode"] = down["nativeVirtualKeyCode"] = key_code
        self.page("Input.dispatchKeyEvent", down, wait=False)
        up = {k: value for k, value in down.items() if k not in ("text", "unmodifiedText")}
        self.page("Input.dispatchKeyEvent", {**up, "type": "keyUp"}, wait=False)

    def insert_text(self, text: str) -> None:
        """Type ``text`` into the focused field in one go (a paste)."""
        self.page("Input.insertText", {"text": text}, wait=False)

    def focused_field_type(self) -> str:
        """The ``type`` of the page's focused input (``password``, ``text``...), or ``""``.
        Follows focus into shadow roots and same-origin frames; a cross-origin frame's
        document can't be read from the page, so a field in one isn't seen."""
        result = self.page("Runtime.evaluate", {"expression": _FOCUSED_FIELD, "returnByValue": True})
        return str(result.get("result", {}).get("value") or "")

    def scroll(self, x: int, y: int, delta_y: int) -> None:
        """Scroll the page under ``x, y`` by ``delta_y`` pixels, as a wheel would. A step
        nothing there can use is dropped: at a page's ends Chrome would bounce the page for
        each one, and a trackpad's stream of steps makes it shake. Waits for the page (up to
        ``CHECK_TIMEOUT``): call it off the UI thread."""
        if not self.can_scroll(x, y, delta_y):
            return
        self.page(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta_y},
            wait=False,
        )

    def can_scroll(self, x: int, y: int, delta_y: int) -> bool:
        """Whether a wheel step at ``x, y`` would do anything: something there can still
        scroll that way, or listens for the wheel itself (a map's zoom, say). If unsure, yes."""
        if self._dead or self._stopped:
            raise BrowserError("the browser isn't running")
        try:
            result = self.page(
                "Runtime.evaluate",
                {
                    "expression": f"({_CAN_SCROLL})({int(x)}, {int(y)}, {int(delta_y)})",
                    "returnByValue": True,
                    "includeCommandLineAPI": True,  # for getEventListeners
                },
                timeout=CHECK_TIMEOUT,
            )
        except Exception:  # a busy page, say: send the step anyway
            return True
        value = result.get("result", {}).get("value")
        return value is not False

    # Replies and events -------------------------------------------------------------------

    def _died(self) -> None:
        with self._waiting_lock:
            if self._dead or self._stopped:
                return
            self._dead = True
        self._load_event.set()
        self._fail_waiting("the browser stopped")
        self.on_note("the browser stopped")

    def _read(self, replies: int, wake: int = -1) -> None:
        """Read replies and events until the pipe ends, or ``wake`` (a pipe ``stop()`` writes
        to) is readable, then close them (only this thread reads the replies pipe, and only
        it closes it: see :meth:`stop`)."""
        buffer = b""
        try:
            while True:
                try:
                    if wake >= 0 and wake in select.select([replies, wake], [], [])[0]:
                        return  # stopped
                    chunk = os.read(replies, 1 << 16)
                except OSError:
                    chunk = b""
                if not chunk:
                    self._died()  # the pipe closed: Chrome exited (or we stopped it)
                    return
                buffer += chunk
                *messages, buffer = buffer.split(b"\0")
                for raw in messages:
                    try:
                        self._handle(json.loads(raw))
                    except Exception:
                        pass  # one bad message never stops the stream
        finally:
            with suppress(OSError):
                os.close(replies)
            if wake >= 0:
                with suppress(OSError):
                    os.close(wake)

    def _allowed(self, url: str) -> bool:
        if self.allow is None:
            return True
        try:
            return bool(self.allow(url))
        except Exception:
            return False  # can't tell: not opened

    def _blocked(self, url: str) -> None:
        self.on_note(f"didn't open {url[:200]}: blocked by URL policy")

    def _handle(self, message: dict[str, Any]) -> None:
        if "id" in message:
            with self._waiting_lock:
                future = self._waiting.pop(message["id"], None)
            if future is not None and not future.done():
                with suppress(Exception):
                    if "error" in message:
                        future.set_exception(BrowserError(message["error"].get("message", "error")))
                    else:
                        future.set_result(message.get("result", {}))
            return
        method, params = message.get("method", ""), message.get("params", {})
        if method == "Page.screencastFrame":
            # Acknowledged once it's decoded and handed on: Chrome sends the next frame only
            # then, so a slow terminal slows the stream instead of piling frames up here.
            try:
                if self.on_frame is not None:
                    from PIL import Image

                    self.on_frame(Image.open(io.BytesIO(base64.b64decode(params["data"]))).convert("RGB"))
            finally:
                self.page("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, wait=False)
        elif method == "Page.loadEventFired":
            self._load_event.set()
        elif method == "Fetch.requestPaused":
            if (popup := self._popup_sessions.get(message.get("sessionId", ""))) is not None:
                self._popup_request(popup, message["sessionId"], params)
            else:
                self._gate(params)
        elif method == "Target.attachedToTarget":
            self._popup_attached(params)
        elif method == "Target.detachedFromTarget":
            self._popup_sessions.pop(params.get("sessionId", ""), None)
        elif method == "Runtime.bindingCalled":
            if params.get("name") == self._binding and self.on_field is not None:
                self.on_field(str(params.get("payload", ""))[:40])
        elif method == "Page.frameAttached":
            if params.get("parentFrameId") and params.get("frameId"):
                self._subframes.add(params["frameId"])
        elif method == "Page.frameDetached":
            self._subframes.discard(params.get("frameId", ""))
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if frame.get("parentId"):
                self._subframes.add(frame.get("id", ""))
            else:  # the main frame, not an iframe
                url = frame.get("url", self.url)
                if not url.startswith("chrome-error://") and url != "about:blank" and not self._allowed(url):
                    # Something _gate doesn't see (an address Chrome loads without a request):
                    # taken down at once.
                    self._blocked(url)
                    self.page("Page.navigate", {"url": "about:blank"}, wait=False)
                    return
                self.url = url
                self.on_url(self.url)
                if self.on_field is not None:
                    self.on_field("")  # a new document: nothing in it has focus yet
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            # A pop-up: open its address here instead, and close the extra page. One that
            # opens blank and gets its address later is kept until it has one.
            info = params.get("targetInfo", {})
            if info.get("type") != "page":
                return
            target = info.get("targetId", "")
            if method == "Target.targetCreated" and info.get("openerId") == self._target:
                self._popups.add(target)
            if target in self._popups:
                url = info.get("url", "")
                if url and url != "about:blank":
                    self._popup_to(target, url)

    def _popup_to(self, target: str, url: str) -> None:
        """A pop-up that wants ``url``: closed, and ``url`` opened in the page instead if
        it may go there. Once per pop-up (its request and its address can both say)."""
        self._popups.discard(target)
        if target in self._popups_done or self._gone():
            return
        self._popups_done.add(target)
        self.call("Target.closeTarget", {"targetId": target}, wait=False)
        if self._gone():  # stopped meanwhile
            return
        if not self._allowed(url):
            self._blocked(url)
            return
        self.navigate(url, wait=False)  # never wait on the reader thread

    def _gone(self) -> bool:
        """Stopped, or exited: a pop-up's events read after that are dropped (nothing to
        send them to, and the view has moved on)."""
        return self._stopped or self._dead

    def _popup_attached(self, params: dict[str, Any]) -> None:
        """A page opened after the main one (``Target.setAutoAttach``): held before it loads
        anything. Its documents are made to wait for the gate, then it's let go."""
        session, info = params.get("sessionId", ""), params.get("targetInfo", {})
        target = info.get("targetId", "")
        if not session or self._gone():
            return
        if info.get("type") != "page" or target == self._target:
            # Not a pop-up: this page itself (its own session, or seen by auto-attach), left be.
            if params.get("waitingForDebugger"):
                self.call("Runtime.runIfWaitingForDebugger", session=session, wait=False)
            return
        self._popup_sessions[session] = target
        self._popups.add(target)  # (if it shows an address without a request, as before)
        self.call("Fetch.enable", {"patterns": [_DOCUMENTS]}, session=session, wait=False)
        self.call("Runtime.runIfWaitingForDebugger", session=session, wait=False)

    def _popup_request(self, target: str, session: str, params: dict[str, Any]) -> None:
        """A document a pop-up asked for: never loaded there. The pop-up is closed, and the
        address opened in the page instead if it may go there (as :meth:`_popup_to`)."""
        if self._gone():  # (Chrome is going or gone, and the request with it)
            return
        self.call(
            "Fetch.failRequest",
            {"requestId": params.get("requestId", ""), "errorReason": BLOCKED_REASON},
            session=session,
            wait=False,
        )
        try:
            url = str(params["request"]["url"])
        except Exception:  # a message not as expected: refused
            url = ""
        if url and url != "about:blank":
            self._popup_to(target, url)
        elif target not in self._popups_done:
            self._popups_done.add(target)
            self.call("Target.closeTarget", {"targetId": target}, wait=False)
            self._blocked(url)

    def _gate(self, params: dict[str, Any]) -> None:
        """A document waiting to load (``Fetch.requestPaused``): the main frame's goes ahead
        only if ``allow`` says so; a frame inside the page is part of the page. Any frame
        not known to be inside the page counts as the main one."""
        request_id = params.get("requestId", "")
        try:
            url = str(params["request"]["url"])
            refused = params.get("frameId") not in self._subframes and not self._allowed(url)
        except Exception:  # a message not as expected: refused, but answered (or it waits forever)
            url, refused = "", True
        if refused:
            self.page("Fetch.failRequest", {"requestId": request_id, "errorReason": BLOCKED_REASON}, wait=False)
            self._blocked(url)
        else:
            self.page("Fetch.continueRequest", {"requestId": request_id}, wait=False)


# --- pixcell.docs.HTML staging helper -----------------------------------------------------

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BASE_RE = re.compile(r"<base\b", re.IGNORECASE)


def _inject_base(markup: str, base_href: str) -> str:
    """Insert ``<base href="...">`` if ``markup`` doesn't already declare one."""
    if _BASE_RE.search(markup):
        return markup
    import html

    tag = f'<base href="{html.escape(base_href, quote=True)}">'
    if match := _HEAD_RE.search(markup):
        return markup[: match.end()] + "\n" + tag + markup[match.end() :]
    return f"{tag}\n{markup}"


def stage_html_document(doc: HTML) -> tuple[str, Policy, Callable[[], None]]:
    """Write ``doc`` (:class:`pixcell.docs.HTML`) to a private temp file and build a
    no-network :class:`Policy` scoped to that file (plus ``doc.base`` when it is a local
    :class:`Path`). Returns ``(file_uri, policy, cleanup)``."""
    temp_dir = tempfile.TemporaryDirectory(prefix="pixcell-html-", ignore_cleanup_errors=True)
    temp_root = Path(temp_dir.name).resolve()
    stem = "document"
    if doc.source is not None and doc.source.stem:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", doc.source.stem).strip("-") or "document"
    page_path = temp_root / f"{stem}.html"

    roots: list[Path] = [temp_root]
    markup = doc.html
    if isinstance(doc.base, Path):
        resolved_base = doc.base.resolve()
        roots.append(resolved_base)
        href = resolved_base.as_uri()
        if (resolved_base.is_dir() or not resolved_base.suffix) and not href.endswith("/"):
            href += "/"
        markup = _inject_base(markup, href)
    elif isinstance(doc.base, str) and doc.base.lower().startswith("file:"):
        from pixcell.browser.policy import file_path

        if (resolved_base := file_path(doc.base)) is not None:
            roots.append(resolved_base)
        markup = _inject_base(markup, doc.base)

    page_path.write_text(markup, encoding="utf-8")
    policy = Policy.offline(file_roots=roots)
    return page_path.as_uri(), policy, temp_dir.cleanup


def open_html(
    doc: HTML,
    *,
    size: tuple[int, int] = (1280, 800),
    executable: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    start: bool = True,
) -> Browser:
    """Open a :class:`pixcell.docs.HTML` document in a headless :class:`Browser` under the
    no-network policy (writes ``doc.html`` to a private temp file and loads ``file://``).

    The temp file is removed automatically when :meth:`Browser.close` runs (or at the end of
    ``with open_html(doc) as browser:``).
    """
    uri, policy, cleanup = stage_html_document(doc)
    browser = Browser(executable, url=uri, size=size, policy=policy, cache_dir=cache_dir)
    browser.add_cleanup(cleanup)
    if start:
        try:
            browser.start(uri, size)
        except BaseException:
            browser.close()
            raise
    return browser
