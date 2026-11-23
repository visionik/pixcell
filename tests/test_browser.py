"""Tests for pixcell.browser: protocol framing, CDP client, keys, docs HTML staging, and BrowserView."""

from __future__ import annotations

import ast
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import get_args

import pytest
from PIL import Image as PILImage

import pixcell.browser as browser_pkg
from pixcell.browser import _portability, cdp, install, keys, policy, view
from pixcell.browser.cdp import NO_NETWORK_FLAGS, Browser, BrowserError, open_html
from pixcell.browser.keys import press_for
from pixcell.browser.policy import Policy, normalize_url
from pixcell.docs.types import HTML


def posix_only(reason: str):
    return pytest.mark.skipif(sys.platform == "win32", reason=reason)


async def wait_for(pilot, condition: Callable[[], object], timeout: float = 10.0) -> bool:
    for _ in range(int(timeout / 0.05)):
        if condition():
            return True
        await pilot.pause(0.05)
    return bool(condition())


# --- Architecture & dependency guard rails -------------------------------------------------


def test_headless_modules_are_stdlib_only_without_textual():
    """cdp, install, keys, policy, _portability and __init__ never import textual or external drivers."""
    forbidden = {"textual", "playwright", "selenium", "httpx", "deft_console", "psutil"}
    for mod in (_portability, cdp, install, keys, policy, browser_pkg):
        source = Path(mod.__file__).read_text("utf-8")
        assert "Ported from ``deftai/console``" in source, mod.__name__
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # Ignore TYPE_CHECKING blocks when checking runtime imports.
            if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
                for child in ast.walk(node):
                    setattr(child, "_in_type_checking", True)
        for node in ast.walk(tree):
            if getattr(node, "_in_type_checking", False):
                continue
            # Lazy __getattr__ importing pixcell.browser.view is fine on attribute access.
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                for child in ast.walk(node):
                    setattr(child, "_in_getattr", True)
        for node in ast.walk(tree):
            if getattr(node, "_in_type_checking", False) or getattr(node, "_in_getattr", False):
                continue
            imported = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in imported:
                top = name.partition(".")[0]
                assert top not in forbidden, f"{mod.__name__} imports {name}"
                assert name != "pixcell.browser.view", f"{mod.__name__} eagerly imports view"


# --- URL normalization & keys --------------------------------------------------------------


def test_addresses(tmp_path: Path):
    (tmp_path / "page.html").write_text("<html></html>", "utf-8")
    assert normalize_url("https://example.com/a") == "https://example.com/a"
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("localhost:3000/app") == "http://localhost:3000/app"
    assert normalize_url("127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert normalize_url("page.html", tmp_path) == (tmp_path / "page.html").resolve().as_uri()
    assert normalize_url(str(tmp_path / "page.html")) == (tmp_path / "page.html").resolve().as_uri()
    assert normalize_url("about:blank") == "about:blank"
    assert normalize_url("en.wikipedia.org,") == "https://en.wikipedia.org"  # pasted from a sentence
    paren = "https://en.wikipedia.org/wiki/Function_(mathematics)"
    assert normalize_url(paren) == paren  # a bracket the address opens stays
    assert normalize_url(f"({paren}).") == paren  # one around it goes
    assert normalize_url("mailto:help@example.com") == "mailto:help@example.com"
    for nothing in (".", "...", " ,; "):
        with pytest.raises(ValueError):
            normalize_url(nothing)
    with pytest.raises(ValueError, match="javascript"):
        normalize_url("javascript:alert(1)")


def test_key_presses():
    enter = press_for("enter", "\r")
    assert enter is not None and enter.key_code == 13 and enter.text == "\r"
    letter = press_for("a", "a")
    assert letter is not None and letter.text == "a"
    upper = press_for("shift+a", "A")
    assert upper is not None and upper.key == "A" and upper.text == "A" and upper.modifiers == 0
    plus = press_for("+", "+")
    assert plus is not None and plus.key == "+" and plus.text == "+"
    shift_tab = press_for("shift+tab", None)
    assert shift_tab is not None and shift_tab.modifiers == 8 and shift_tab.key == "Tab"
    ctrl_a = press_for("ctrl+a", None)
    assert ctrl_a is not None
    assert (ctrl_a.key, ctrl_a.code, ctrl_a.modifiers, ctrl_a.text) == ("a", "KeyA", 2, "")
    ctrl_space = press_for("ctrl+space", " ")
    assert ctrl_space is not None and ctrl_space.text == "" and ctrl_space.modifiers == 2
    assert press_for("f5", None) is None


# --- CDP protocol framing, concurrency, gates & lifecycle ----------------------------------


def test_concurrent_calls_each_get_their_own_reply():
    """Many threads calling at once over one pipe: every reply reaches its own caller."""
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    browser = Browser("/fake", on_frame=lambda image: None)
    browser._pid, browser._commands, browser._replies = 0, to_child, from_child
    browser._reader = threading.Thread(target=browser._read, args=(from_child,), daemon=True)
    browser._reader.start()

    def chrome():  # answers each command with its own parameters, out of order in pairs
        buffer, held = b"", []
        while chunk := os.read(to_child_read, 65536):
            buffer += chunk
            *messages, buffer = buffer.split(b"\0")
            for raw in messages:
                message = json.loads(raw)
                held.append(json.dumps({"id": message["id"], "result": message["params"]}).encode() + b"\0")
                if len(held) == 2:
                    os.write(from_child_write, held.pop() + held.pop())
            if held:
                os.write(from_child_write, held.pop())

    threading.Thread(target=chrome, daemon=True).start()
    wrong = []

    def caller(n: int):
        for i in range(50):
            if browser.call("Echo", {"who": n, "i": i}) != {"who": n, "i": i}:
                wrong.append((n, i))

    callers = [threading.Thread(target=caller, args=(n,)) for n in range(8)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(30)
    try:
        assert wrong == [] and not browser._waiting
    finally:
        os.close(from_child_write)
        browser._reader.join(5)
        browser.stop()
        os.close(to_child_read)


def test_split_and_coalesced_frames_and_malformed_json():
    """NUL-delimited framing handles partial chunks, coalesced frames, bad JSON, and CDP errors."""
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    urls: list[str] = []
    browser = Browser("/fake", on_frame=lambda image: None, on_url=urls.append)
    browser._pid, browser._commands, browser._replies = 0, to_child, from_child
    browser._reader = threading.Thread(target=browser._read, args=(from_child,), daemon=True)
    browser._reader.start()

    def responder():
        buf = b""
        seen: list[dict] = []
        while len(seen) < 2 and (chunk := os.read(to_child_read, 4096)):
            buf += chunk
            *parts, buf = buf.split(b"\0")
            for part in parts:
                seen.append(json.loads(part))
        first_reply = json.dumps({"id": seen[0]["id"], "result": {"ok": "x" * 200}}).encode()
        event = json.dumps(
            {"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "https://a.example/"}}}
        ).encode()
        second_reply = json.dumps({"id": seen[1]["id"], "error": {"message": "bad method"}}).encode()
        # Split first reply across writes, then coalesce malformed frame + event + error reply.
        os.write(from_child_write, first_reply[:15])
        time.sleep(0.02)
        os.write(from_child_write, first_reply[15:] + b"\0not-json\0" + event + b"\0" + second_reply + b"\0")

    threading.Thread(target=responder, daemon=True).start()
    try:
        results: list = []
        errors: list[Exception] = []
        t1 = threading.Thread(target=lambda: results.append(browser.call("First")))
        t2 = threading.Thread(
            target=lambda: (
                errors.append(exc)
                if (exc := _catch(lambda: browser.call("Second"))) is not None
                else None
            )
        )
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)
        assert results == [{"ok": "x" * 200}]
        assert len(errors) == 1 and isinstance(errors[0], BrowserError) and "bad method" in str(errors[0])
        assert urls == ["https://a.example/"]
    finally:
        os.close(from_child_write)
        browser._reader.join(5)
        browser.stop()
        os.close(to_child_read)


def _catch(fn: Callable[[], object]) -> Exception | None:
    try:
        fn()
    except Exception as exc:
        return exc
    return None


@posix_only("pipes and descriptor numbers")
def test_stopping_never_closes_the_pipe_under_the_reader(monkeypatch):
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    browser = Browser("/fake", on_frame=lambda image: None)
    about_to_read, go = threading.Event(), threading.Event()
    real_read = os.read
    handled = []

    def slow_read(fd, size):
        about_to_read.set()
        go.wait(5)
        return real_read(fd, size)

    def killed(pid, grace=3.0):
        os.close(from_child_write)
        os.close(to_child_read)

    monkeypatch.setattr(cdp.os, "read", slow_read)
    monkeypatch.setattr(_portability, "stop_spawned", killed)
    monkeypatch.setattr(browser, "_handle", handled.append)
    browser._pid, browser._commands, browser._replies = 12345, to_child, from_child
    browser._reader = threading.Thread(target=browser._read, args=(from_child,), daemon=True)
    browser._reader.start()
    assert about_to_read.wait(5)
    stopping = threading.Thread(target=browser.stop)
    stopping.start()
    stopping.join(0.3)
    others = [os.pipe() for _ in range(4)]
    try:
        for _, other_write in others:
            os.write(other_write, b'{"id": 1, "result": {}}\0')
        go.set()
        stopping.join(5)
        assert not stopping.is_alive()
        browser._reader.join(5)
        assert not browser._reader.is_alive()
        for other_read, _ in others:
            os.set_blocking(other_read, False)
            assert real_read(other_read, 100) == b'{"id": 1, "result": {}}\0'
        assert handled == []
        with pytest.raises(OSError):
            os.fstat(from_child)
    finally:
        go.set()
        for ends in others:
            for fd in ends:
                with contextlib.suppress(OSError):
                    os.close(fd)


@posix_only("pipes")
def test_stopping_ends_the_reader_even_if_something_holds_the_pipe(monkeypatch):
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    monkeypatch.setattr(_portability, "spawn_with_pipe", lambda *a: (0, to_child, from_child))
    browser = Browser("/fake", on_frame=lambda image: None)
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: {"targetId": "t", "sessionId": "s"})
    browser.start("about:blank", (100, 100))
    try:
        began = time.monotonic()
        browser.stop()
        assert time.monotonic() - began < 2.0
        assert browser._reader is not None
        browser._reader.join(2)
        assert not browser._reader.is_alive()
        with pytest.raises(OSError):
            os.fstat(from_child)
    finally:
        os.close(from_child_write)
        os.close(to_child_read)


@posix_only("pipes")
def test_stopping_after_the_reader_ended_closes_the_wake_pipe(monkeypatch):
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    monkeypatch.setattr(_portability, "spawn_with_pipe", lambda *a: (0, to_child, from_child))
    browser = Browser("/fake", on_frame=lambda image: None)
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: {"targetId": "t", "sessionId": "s"})
    browser.start("about:blank", (100, 100))
    wake = browser._wake
    os.close(from_child_write)
    assert browser._reader is not None
    browser._reader.join(2)
    assert not browser._reader.is_alive()
    browser.stop()
    os.close(to_child_read)
    with pytest.raises(OSError):
        os.fstat(wake)


@posix_only("patches os.posix_spawnp")
def test_closing_during_launch_closes_the_pipes(monkeypatch):
    browser = Browser("/fake", on_frame=lambda image: None)
    opened = []
    real_pipe = os.pipe

    def tracking_pipe():
        ends = real_pipe()
        opened.extend(ends)
        return ends

    def spawn_after_close(*args, **kwargs):
        browser._stopped = True
        return 0

    monkeypatch.setattr(os, "pipe", tracking_pipe)
    monkeypatch.setattr(os, "posix_spawnp", spawn_after_close)
    with pytest.raises(BrowserError):
        browser.start("about:blank", (100, 100))
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_browser_stop_and_fail_waiting_are_thread_safe(monkeypatch):
    browser = Browser("/fake", on_frame=lambda _: None)
    read_cmd, write_cmd = os.pipe()
    read_rep, write_rep = os.pipe()
    browser._pid = 424242
    browser._commands = write_cmd
    browser._replies = read_rep
    browser._profile = tempfile.TemporaryDirectory(prefix="pixcell-browser-test-", ignore_cleanup_errors=True)

    stopped_pids: list[int] = []
    monkeypatch.setattr(_portability, "stop_spawned", lambda pid, grace=3.0: stopped_pids.append(pid))

    seeded: list[Future] = [Future() for _ in range(20)]
    for idx, fut in enumerate(seeded, start=1000):
        browser._waiting[idx] = fut

    barrier = threading.Barrier(10)
    unexpected: list[BaseException] = []

    def stopper() -> None:
        try:
            barrier.wait(5)
            browser.stop()
        except BaseException as exc:
            unexpected.append(exc)

    def failer() -> None:
        try:
            barrier.wait(5)
            browser._fail_waiting("failed concurrently")
        except BaseException as exc:
            unexpected.append(exc)

    def caller() -> None:
        try:
            barrier.wait(5)
            for _ in range(10):
                try:
                    browser.call("Browser.getVersion", timeout=0.5)
                except BrowserError:
                    pass
        except BaseException as exc:
            unexpected.append(exc)

    threads = (
        [threading.Thread(target=stopper) for _ in range(4)]
        + [threading.Thread(target=failer) for _ in range(2)]
        + [threading.Thread(target=caller) for _ in range(4)]
    )
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    finally:
        for fd in (read_cmd, write_cmd, read_rep, write_rep):
            with contextlib.suppress(OSError):
                os.close(fd)

    assert unexpected == []
    assert stopped_pids == [424242]
    assert browser._waiting == {}
    for fut in seeded:
        assert fut.done()
        with pytest.raises(BrowserError):
            fut.result()


def test_stop_kills_stuck_browser_blocked_in_write(monkeypatch):
    browser = Browser("/fake", on_frame=lambda _: None)
    read_cmd, write_cmd = os.pipe()
    read_rep, write_rep = os.pipe()
    browser._pid = 515151
    browser._commands = write_cmd
    browser._replies = read_rep

    entered_write = threading.Event()
    pipe_broken = threading.Event()
    writing_now = {"active": False}
    closed_while_writing = {"saw": False}
    stopped_pids: list[int] = []

    real_write = os.write
    real_close = os.close

    def blocking_write(fd: int, data) -> int:
        if fd == write_cmd:
            writing_now["active"] = True
            entered_write.set()
            try:
                assert pipe_broken.wait(5)
                raise BrokenPipeError("pipe closed")
            finally:
                writing_now["active"] = False
        return real_write(fd, data)

    def checked_close(fd: int) -> None:
        if fd == write_cmd and writing_now["active"]:
            closed_while_writing["saw"] = True
        real_close(fd)

    def kill_stuck(pid: int, grace: float = 3.0) -> None:
        stopped_pids.append(pid)
        pipe_broken.set()

    monkeypatch.setattr(cdp.os, "write", blocking_write)
    monkeypatch.setattr(cdp.os, "close", checked_close)
    monkeypatch.setattr(_portability, "stop_spawned", kill_stuck)

    errors: list[BaseException] = []

    def stuck_caller() -> None:
        try:
            browser.call("Input.insertText", {"text": "x" * 1024}, wait=False)
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=stuck_caller)
    try:
        writer.start()
        assert entered_write.wait(5)
        browser.stop()
        writer.join(5)
        assert not writer.is_alive()
    finally:
        pipe_broken.set()
        for fd in (read_cmd, write_cmd, read_rep, write_rep):
            with contextlib.suppress(OSError):
                real_close(fd)

    assert stopped_pids == [515151]
    assert not closed_while_writing["saw"]
    assert len(errors) == 1 and isinstance(errors[0], BrowserError)


def test_a_frame_is_acknowledged_after_it_is_decoded(monkeypatch):
    order = []
    browser = Browser("/fake", on_frame=lambda image: order.append(("frame", image.size)))
    browser._session = "session"
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: order.append((method,)) or {})
    out = io.BytesIO()
    PILImage.new("RGB", (4, 3)).save(out, "JPEG")
    data = base64.b64encode(out.getvalue()).decode()
    browser._handle({"method": "Page.screencastFrame", "params": {"sessionId": 7, "data": data}})
    assert order == [("frame", (4, 3)), ("Page.screencastFrameAck",)]
    order.clear()
    with pytest.raises(Exception):
        browser._handle({"method": "Page.screencastFrame", "params": {"sessionId": 8, "data": "bm90IGEganBlZw=="}})
    assert order == [("Page.screencastFrameAck",)]


def _recording_browser(monkeypatch, allow=None):
    notes, urls = [], []
    browser = Browser("/fake", on_frame=lambda image: None, on_url=urls.append, on_note=notes.append, allow=allow)
    browser._target, browser._session = "main", "session"
    sent = []
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: sent.append((method, params)) or {})
    return browser, sent, notes, urls


def _only_local(url: str) -> bool:
    return url.startswith(("http://localhost", "about:"))


def test_blank_popups_wait_for_their_address(monkeypatch):
    browser = Browser("/fake", on_frame=lambda image: None)
    browser._target = "main"
    sent = []
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: sent.append((method, params)) or {})
    monkeypatch.setattr(browser, "page", lambda method, params=None, **kw: sent.append((method, params)) or {})
    info = {"type": "page", "targetId": "pop", "openerId": "main", "url": "about:blank"}
    browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": info}})
    assert sent == []
    browser._handle(
        {"method": "Target.targetInfoChanged", "params": {"targetInfo": {**info, "url": "https://example.com/x"}}}
    )
    assert ("Target.closeTarget", {"targetId": "pop"}) in sent
    assert ("Page.navigate", {"url": "https://example.com/x"}) in sent
    assert browser.url == "https://example.com/x"


def test_a_popup_to_an_address_the_page_may_not_go_is_closed_not_opened(monkeypatch):
    browser, sent, notes, _ = _recording_browser(monkeypatch, allow=_only_local)
    info = {"type": "page", "targetId": "pop", "openerId": "main", "url": "https://evil.example/login"}
    browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": info}})
    assert ("Target.closeTarget", {"targetId": "pop"}) in sent
    assert not [call for call in sent if call[0] == "Page.navigate"]
    assert browser.url == "" and "didn't open https://evil.example/login" in notes[0]

    blank = {"type": "page", "targetId": "pop2", "openerId": "main", "url": "about:blank"}
    browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": blank}})
    browser._handle({"method": "Target.targetInfoChanged", "params": {"targetInfo": {**blank, "url": "file:///etc/hosts"}}})
    assert ("Target.closeTarget", {"targetId": "pop2"}) in sent
    assert not [call for call in sent if call[0] == "Page.navigate"]

    ok = {"type": "page", "targetId": "pop3", "openerId": "main", "url": "http://localhost:3000/next"}
    browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": ok}})
    assert ("Page.navigate", {"url": "http://localhost:3000/next"}) in sent


def test_a_popup_is_held_until_its_documents_wait_for_the_gate(monkeypatch):
    browser, _, notes, _ = _recording_browser(monkeypatch, allow=_only_local)
    sent = []
    monkeypatch.setattr(
        browser, "call", lambda method, params=None, session="", **kw: sent.append((session, method, params)) or {}
    )
    info = {"type": "page", "targetId": "pop", "openerId": "main", "url": ""}
    browser._handle(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "pop-s", "targetInfo": info, "waitingForDebugger": True},
        }
    )
    assert [(session, method) for session, method, _ in sent] == [
        ("pop-s", "Fetch.enable"),
        ("pop-s", "Runtime.runIfWaitingForDebugger"),
    ]
    sent.clear()
    paused = {"requestId": "r1", "frameId": "pop", "resourceType": "Document", "request": {"url": "https://evil.example/?k=v"}}
    browser._handle({"method": "Fetch.requestPaused", "sessionId": "pop-s", "params": paused})
    assert ("pop-s", "Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"}) in sent
    assert ("", "Target.closeTarget", {"targetId": "pop"}) in sent
    assert not [call for call in sent if call[1] in ("Page.navigate", "Fetch.continueRequest")]
    assert len(notes) == 1 and "evil.example" in notes[0]
    browser._handle(
        {"method": "Target.targetInfoChanged", "params": {"targetInfo": {**info, "url": "https://evil.example/?k=v"}}}
    )
    assert len(notes) == 1

    sent.clear()
    ok = {"type": "page", "targetId": "pop2", "openerId": "main", "url": ""}
    browser._handle(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "pop2-s", "targetInfo": ok, "waitingForDebugger": True},
        }
    )
    paused = {"requestId": "r2", "frameId": "pop2", "resourceType": "Document", "request": {"url": "http://localhost:3000/next"}}
    browser._handle({"method": "Fetch.requestPaused", "sessionId": "pop2-s", "params": paused})
    assert ("pop2-s", "Fetch.failRequest", {"requestId": "r2", "errorReason": "BlockedByClient"}) in sent
    assert ("session", "Page.navigate", {"url": "http://localhost:3000/next"}) in sent

    sent.clear()
    own = {"type": "page", "targetId": "main", "url": "about:blank"}
    browser._handle(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "own-s", "targetInfo": own, "waitingForDebugger": False},
        }
    )
    assert sent == [] and "own-s" not in browser._popup_sessions


def test_a_popup_still_in_flight_when_the_browser_stops_does_nothing():
    notes, urls = [], []
    browser = Browser("/fake", on_frame=lambda image: None, on_url=urls.append, on_note=notes.append, allow=_only_local)
    browser._target, browser._session = "main", "session"
    browser.stop()
    info = {"type": "page", "targetId": "pop", "openerId": "main", "url": ""}
    browser._handle(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "pop-s", "targetInfo": info, "waitingForDebugger": True},
        }
    )
    browser._popup_sessions["pop-s"] = "pop"
    for url in ("http://localhost:3000/next", "https://evil.example/"):
        paused = {"requestId": "r", "frameId": "pop", "resourceType": "Document", "request": {"url": url}}
        browser._handle({"method": "Fetch.requestPaused", "sessionId": "pop-s", "params": paused})
        browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": {**info, "targetId": url, "url": url}}})
    assert urls == [] and notes == [] and browser.url == ""


def test_the_main_frame_loads_only_allowed_documents(monkeypatch):
    browser, sent, notes, _ = _recording_browser(monkeypatch, allow=_only_local)

    def paused(request_id: str, url: str, frame: str = "main") -> None:
        browser._handle(
            {
                "method": "Fetch.requestPaused",
                "sessionId": "session",
                "params": {"requestId": request_id, "frameId": frame, "resourceType": "Document", "request": {"url": url}},
            }
        )

    browser._handle({"method": "Page.frameAttached", "params": {"frameId": "an-iframe", "parentFrameId": "main"}})
    paused("1", "http://localhost:3000/")
    paused("2", "https://evil.example/")
    paused("3", "http://192.168.1.1/")
    paused("4", "https://cdn.example/embed", frame="an-iframe")
    paused("5", "https://evil.example/", frame="main-frame-with-another-id")
    browser._handle({"method": "Page.frameDetached", "params": {"frameId": "an-iframe"}})
    paused("6", "https://cdn.example/embed", frame="an-iframe")
    assert sent == [
        ("Fetch.continueRequest", {"requestId": "1"}),
        ("Fetch.failRequest", {"requestId": "2", "errorReason": "BlockedByClient"}),
        ("Fetch.failRequest", {"requestId": "3", "errorReason": "BlockedByClient"}),
        ("Fetch.continueRequest", {"requestId": "4"}),
        ("Fetch.failRequest", {"requestId": "5", "errorReason": "BlockedByClient"}),
        ("Fetch.failRequest", {"requestId": "6", "errorReason": "BlockedByClient"}),
    ]
    assert len(notes) == 4 and "evil.example" in notes[0]


def test_a_gate_that_fails_refuses_and_unexpected_requests_are_answered(monkeypatch):
    def broken(url: str) -> bool:
        raise ValueError("can't tell")

    browser, sent, _, _ = _recording_browser(monkeypatch, allow=broken)
    browser._handle(
        {
            "method": "Fetch.requestPaused",
            "params": {"requestId": "1", "frameId": "main", "request": {"url": "https://example.com"}},
        }
    )
    assert sent[0][0] == "Fetch.failRequest"

    browser2, sent2, _, _ = _recording_browser(monkeypatch, allow=_only_local)
    browser2._handle({"method": "Fetch.requestPaused", "params": {"requestId": "1", "frameId": "main", "request": None}})
    assert sent2 == [("Fetch.failRequest", {"requestId": "1", "errorReason": "BlockedByClient"})]


def test_an_address_loaded_without_a_request_is_taken_down(monkeypatch):
    browser, sent, notes, urls = _recording_browser(monkeypatch, allow=_only_local)
    browser._handle({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "data:text/html,fake"}}})
    assert ("Page.navigate", {"url": "about:blank"}) in sent and notes and urls == []
    browser._handle({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "http://localhost:3000/"}}})
    browser._handle(
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "chrome-error://chromewebdata/"}}}
    )
    assert urls == ["http://localhost:3000/", "chrome-error://chromewebdata/"]


def test_a_page_without_policy_goes_anywhere(monkeypatch):
    browser, sent, notes, _ = _recording_browser(monkeypatch)
    browser._handle(
        {
            "method": "Fetch.requestPaused",
            "params": {"requestId": "1", "frameId": "main", "request": {"url": "https://example.com"}},
        }
    )
    browser._handle({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "https://example.com/"}}})
    info = {"type": "page", "targetId": "pop", "openerId": "main", "url": "https://example.com/x"}
    browser._handle({"method": "Target.targetCreated", "params": {"targetInfo": info}})
    assert ("Fetch.continueRequest", {"requestId": "1"}) in sent
    assert ("Page.navigate", {"url": "https://example.com/x"}) in sent and not notes


def test_only_gated_and_watched_pages_enable_fetch_and_bindings(monkeypatch):
    def started(**options):
        browser = Browser("/fake", on_frame=lambda image: None, **options)
        sent = []

        def call(method, params=None, **kw):
            sent.append(method)
            return {"targetId": "main", "sessionId": "session"}

        monkeypatch.setattr(_portability, "spawn_with_pipe", lambda *a: (0, -1, -1))
        monkeypatch.setattr(browser, "_read", lambda replies, wake=-1: None)
        monkeypatch.setattr(browser, "call", call)
        browser.start("about:blank", (100, 100))
        browser.stop()
        return sent

    plain = started()
    assert "Fetch.enable" not in plain and "Runtime.addBinding" not in plain and "Target.setAutoAttach" not in plain
    gated = started(allow=_only_local, on_field=lambda kind: None)
    assert gated.index("Fetch.enable") < gated.index("Page.navigate")
    assert gated.index("Target.setAutoAttach") < gated.index("Page.navigate")
    assert gated.index("Page.addScriptToEvaluateOnNewDocument") < gated.index("Page.navigate")


def test_the_field_watcher_runs_in_a_world_of_its_own_and_clears_on_navigation(monkeypatch):
    fields = []
    browser = Browser("/fake", on_frame=lambda image: None, on_field=fields.append)
    sent = {}

    def call(method, params=None, **kw):
        sent[method] = params
        return {"targetId": "main", "sessionId": "session"}

    monkeypatch.setattr(_portability, "spawn_with_pipe", lambda *a: (0, -1, -1))
    monkeypatch.setattr(browser, "_read", lambda replies, wake=-1: None)
    monkeypatch.setattr(browser, "call", call)
    browser.start("about:blank", (100, 100))
    world = sent["Page.addScriptToEvaluateOnNewDocument"].get("worldName")
    assert world and sent["Runtime.addBinding"].get("executionContextName") == world

    browser._handle({"method": "Runtime.bindingCalled", "params": {"name": browser._binding, "payload": "password"}})
    browser._handle({"method": "Runtime.bindingCalled", "params": {"name": "pagesOwn", "payload": "text"}})
    browser._handle({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": "https://example.com/"}}})
    browser._handle(
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "f", "parentId": "main", "url": "https://x/"}}}
    )
    assert fields == ["password", ""]
    assert browser._binding != Browser("/fake", on_frame=lambda image: None)._binding
    browser.stop()


# --- Headless Browser (navigate, eval, screenshot to PIL/PNG, close) -----------------------


def test_headless_browser_navigate_eval_screenshot_pil_and_png_and_close(tmp_path: Path, monkeypatch):
    buf = io.BytesIO()
    PILImage.new("RGB", (12, 8), (10, 120, 220)).save(buf, format="PNG")
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    spawned_args: list[list[str]] = []

    def fake_spawn(executable: str, arguments: list[str]):
        spawned_args.append(arguments)
        return 0, to_child, from_child

    monkeypatch.setattr(_portability, "spawn_with_pipe", fake_spawn)
    methods_seen: list[str] = []

    def fake_chrome():
        buffer = b""
        while chunk := os.read(to_child_read, 65536):
            buffer += chunk
            *messages, buffer = buffer.split(b"\0")
            for raw in messages:
                msg = json.loads(raw)
                method = msg["method"]
                methods_seen.append(method)
                result: dict = {}
                if method == "Target.createTarget":
                    result = {"targetId": "t-main"}
                elif method == "Target.attachToTarget":
                    result = {"sessionId": "s-main"}
                elif method == "Page.navigate":
                    url = msg["params"]["url"]
                    if url != "about:blank":
                        result = {"frameId": "f1", "loaderId": "load-1"}
                elif method == "Runtime.evaluate":
                    expr = msg["params"]["expression"]
                    if "throw" in expr:
                        result = {
                            "result": {"type": "object", "subtype": "error"},
                            "exceptionDetails": {"text": "Uncaught", "exception": {"description": "Error: boom"}},
                        }
                    else:
                        result = {"result": {"type": "string", "value": f"eval:{expr}"}}
                elif method == "Page.captureScreenshot":
                    result = {"data": png_b64}
                os.write(from_child_write, json.dumps({"id": msg["id"], "result": result}).encode() + b"\0")
                if method == "Page.navigate" and msg["params"]["url"] != "about:blank":
                    event = json.dumps({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}}).encode() + b"\0"
                    os.write(from_child_write, event)

    threading.Thread(target=fake_chrome, daemon=True).start()
    out_file = tmp_path / "shot.png"
    try:
        with Browser("/fake/chrome", size=(640, 480)) as browser:
            assert "Page.startScreencast" not in methods_seen  # headless mode skips JPEG screencast
            browser.navigate("https://example.com/hello")
            assert browser.url == "https://example.com/hello"
            assert browser.eval("document.title") == "eval:document.title"
            with pytest.raises(BrowserError, match="Error: boom"):
                browser.eval("throw new Error('boom')")
            pil_image = browser.screenshot()
            assert isinstance(pil_image, PILImage.Image)
            assert pil_image.size == (12, 8)
            raw_png = browser.screenshot(format="png", path=out_file)
            assert isinstance(raw_png, bytes) and raw_png.startswith(b"\x89PNG")
            assert out_file.read_bytes() == raw_png
            assert browser.screenshot_png() == raw_png
            with pytest.raises(ValueError, match="unsupported screenshot format"):
                browser.screenshot(format="bmp")  # type: ignore[call-overload]
        # Closed on context exit.
        with pytest.raises(BrowserError, match="isn't running"):
            browser.eval("1 + 1")
    finally:
        with contextlib.suppress(OSError):
            os.close(from_child_write)
        with contextlib.suppress(OSError):
            os.close(to_child_read)


def test_headless_no_network_passes_offline_flags_and_blocks_http(tmp_path: Path, monkeypatch):
    page = tmp_path / "local.html"
    page.write_text("<h1>Local</h1>", "utf-8")
    spawned_args: list[list[str]] = []

    def fake_spawn(executable: str, arguments: list[str]):
        spawned_args.append(arguments)
        return 0, -1, -1

    monkeypatch.setattr(_portability, "spawn_with_pipe", fake_spawn)
    browser = Browser("/fake/chrome", policy=Policy.offline(file_roots=[tmp_path]))
    monkeypatch.setattr(browser, "_read", lambda replies, wake=-1: None)
    monkeypatch.setattr(browser, "call", lambda method, params=None, **kw: {"targetId": "t", "sessionId": "s"})
    browser.start(page.as_uri(), (400, 300))
    try:
        assert spawned_args
        for flag in NO_NETWORK_FLAGS:
            assert flag in spawned_args[0]
        with pytest.raises(BrowserError, match="blocked by URL policy"):
            browser.navigate("https://example.com")
        with pytest.raises(BrowserError, match="blocked by URL policy"):
            browser.navigate("http://localhost:8000")
        with pytest.raises(BrowserError, match="blocked by URL policy"):
            browser.navigate(Path("/etc/hosts").as_uri())
    finally:
        browser.close()


# --- pixcell.docs.HTML integration ---------------------------------------------------------


def test_open_html_stages_temp_file_under_no_network_policy_and_cleans_up(tmp_path: Path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "diagram.png").write_bytes(b"png")
    doc = HTML(
        html="<!DOCTYPE html><html><head><title>Memo</title></head><body><img src='diagram.png'></body></html>",
        base=assets,
        title="Memo",
        source=tmp_path / "Quarterly Report!.docx",
    )

    started: list[tuple[str, tuple[int, int], list[str]]] = []

    def fake_spawn(executable: str, arguments: list[str]):
        started.append(("", (0, 0), arguments))
        return 0, -1, -1

    monkeypatch.setattr(_portability, "spawn_with_pipe", fake_spawn)
    monkeypatch.setattr(Browser, "_read", lambda self, replies, wake=-1: None)
    monkeypatch.setattr(Browser, "call", lambda self, method, params=None, **kw: {"targetId": "t", "sessionId": "s"})

    browser = open_html(doc, executable="/fake/chrome", size=(800, 600))
    staged_file = policy.file_path(browser.url)
    assert staged_file is not None and staged_file.is_file()
    assert staged_file.name == "Quarterly-Report.html"
    written = staged_file.read_text("utf-8")
    assert f'<base href="{assets.resolve().as_uri()}/">' in written

    assert browser.policy is not None and browser.policy.no_network is True
    assert browser.policy.allows(browser.url)
    assert browser.policy.allows((assets / "diagram.png").as_uri())
    assert browser.policy.allows("data:image/png;base64,AAAA")
    assert not browser.policy.allows("https://example.com")
    assert not browser.policy.allows("http://127.0.0.1:8080")
    assert not browser.policy.allows((tmp_path / "outside.html").as_uri())
    assert all(flag in started[0][2] for flag in NO_NETWORK_FLAGS)

    staged_dir = staged_file.parent
    browser.close()
    assert not staged_dir.exists()

    # HTML.open_browser() delegates to open_html and cleans up on startup failure too.
    def boom_spawn(executable: str, arguments: list[str]):
        raise OSError("no chrome binary")

    monkeypatch.setattr(_portability, "spawn_with_pipe", boom_spawn)
    uri, staged_policy, cleanup = cdp.stage_html_document(HTML(html="<h1>Hi</h1>"))
    staged_path = policy.file_path(uri)
    assert staged_path is not None and staged_path.is_file()
    cleanup()
    assert not staged_path.parent.exists()

    with pytest.raises(BrowserError, match="couldn't start"):
        doc.open_browser(executable="/missing/chrome")


# --- Textual BrowserView widget tests with fake frames -------------------------------------


class FakeBrowser:
    """Records what BrowserView asks of the browser; produces one frame on start."""

    made: list["FakeBrowser"] = []

    def __init__(self, executable, *, on_frame, on_url=None, on_note=None, **options):
        self.on_frame, self.on_url, self.on_note = on_frame, on_url, on_note
        self.options = options
        self.calls: list[tuple] = []
        FakeBrowser.made.append(self)

    def start(self, url, size):
        self.calls.append(("start", url, size))
        if self.on_url is not None:
            self.on_url(url)
        self.on_frame(PILImage.new("RGB", size, (200, 30, 30)))

    def resize(self, size):
        self.calls.append(("resize", size))

    def click(self, x, y, button="left"):
        self.calls.append(("click", x, y))

    def scroll(self, x, y, delta):
        self.calls.append(("scroll", x, y, delta))

    def stop(self):
        self.calls.append(("stop",))


async def _with_view(monkeypatch, browser_class, test, *, url="https://example.com", **view_kwargs):
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult

    FakeBrowser.made.clear()
    monkeypatch.setattr(cdp, "Browser", browser_class)
    monkeypatch.setattr(install, "find_browser", lambda: "/fake/chrome")
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"

        def compose(self) -> ComposeResult:
            yield view.BrowserView(url, **view_kwargs)

    try:
        async with One().run_test(size=(80, 30)) as pilot:
            await test(pilot)
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


async def test_view_starts_the_browser_at_its_pixel_size_and_forwards_clicks(monkeypatch):
    from pixcell.live import LiveImage

    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        browser = FakeBrowser.made[0]
        assert browser.calls[0] == ("start", "https://example.com", (320, 160))  # 40x10 cells of 8x16
        live = pilot.app.query_one(LiveImage)
        assert await wait_for(pilot, lambda: live.frames_shown > 0)
        await pilot.click(LiveImage, offset=(5, 2))
        await pilot.pause(0.1)
        clicks = [call for call in browser.calls if call[0] == "click"]
        assert clicks and 40 <= clicks[0][1] < 48 and 32 <= clicks[0][2] < 48
        await pilot.click(view.CloseButton)
        assert await wait_for(pilot, lambda: not pilot.app.query(view.BrowserView))
        assert await wait_for(pilot, lambda: ("stop",) in browser.calls)

    await _with_view(monkeypatch, FakeBrowser, check)


class SlowBrowser(FakeBrowser):
    release: threading.Event | None = None

    def start(self, url, size):
        assert SlowBrowser.release is not None
        SlowBrowser.release.wait(5)
        super().start(url, size)


async def test_closing_while_starting_stops_the_browser(monkeypatch):
    SlowBrowser.release = threading.Event()

    async def check(pilot):
        assert await wait_for(pilot, lambda: bool(FakeBrowser.made))
        pilot.app.query_one(view.BrowserView).remove()
        await pilot.pause(0.1)
        assert SlowBrowser.release is not None
        SlowBrowser.release.set()
        assert await wait_for(pilot, lambda: ("stop",) in FakeBrowser.made[0].calls)

    await _with_view(monkeypatch, SlowBrowser, check)


async def test_a_resize_during_startup_is_applied_after(monkeypatch):
    SlowBrowser.release = threading.Event()

    async def check(pilot):
        assert await wait_for(pilot, lambda: bool(FakeBrowser.made))
        await pilot.resize_terminal(60, 30)
        pilot.app.query_one(view.BrowserView).query_one("LiveImage").styles.width = 30
        await pilot.pause(0.2)
        assert SlowBrowser.release is not None
        SlowBrowser.release.set()
        assert await wait_for(pilot, lambda: any(call[0] == "resize" for call in FakeBrowser.made[0].calls))
        resizes = [call for call in FakeBrowser.made[0].calls if call[0] == "resize"]
        assert resizes[-1][1] == (240, 160)

    await _with_view(monkeypatch, SlowBrowser, check)


async def test_resize_between_worker_finish_and_running_transition_is_applied(monkeypatch):
    started_with = view.BrowserView._started_with
    held: list[tuple[view.BrowserView, bool, str]] = []

    def hold_start(browser_view: view.BrowserView, ok: bool, note: str = "") -> None:
        if ok:
            held.append((browser_view, ok, note))
        else:
            started_with(browser_view, ok, note)

    monkeypatch.setattr(view.BrowserView, "_started_with", hold_start)

    async def check(pilot):
        from pixcell.live import LiveImage

        assert await wait_for(pilot, lambda: bool(held))
        browser_view = pilot.app.query_one(view.BrowserView)
        assert browser_view.phase == "starting"
        [browser] = FakeBrowser.made
        initial_size = browser_view._viewport_size
        assert initial_size is not None

        live = browser_view.query_one(LiveImage)
        live.styles.width = 60
        live.styles.height = 15
        await pilot.pause()
        browser_view.viewport_changed()
        assert await wait_for(pilot, lambda: browser_view._viewport_size != initial_size)
        resized_to = browser_view._viewport_size
        assert resized_to is not None and ("resize", resized_to) not in browser.calls

        held_view, ok, note = held.pop()
        started_with(held_view, ok, note)
        assert browser_view.phase == "running"
        assert await wait_for(pilot, lambda: ("resize", resized_to) in browser.calls)

    await _with_view(monkeypatch, FakeBrowser, check)


class DeadBrowser(FakeBrowser):
    def click(self, x, y, button="left"):
        raise BrowserError("the browser stopped")


async def test_input_to_a_dead_browser_is_a_note(monkeypatch):
    from pixcell.live import LiveImage

    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        await pilot.click(LiveImage, offset=(5, 2))
        await pilot.pause(0.1)
        header = str(pilot.app.query_one(view.BrowserHeader).render())
        assert "the browser stopped" in header

    await _with_view(monkeypatch, DeadBrowser, check)


class ReloadingBrowser(FakeBrowser):
    def reload(self):
        self.calls.append(("reload",))


def test_only_local_views_reload():
    for url, local in (
        ("http://localhost:3000/", True),
        ("http://127.0.0.1:8000", True),
        ("file:///tmp/report.html", True),
        ("https://example.com", False),
    ):
        browser_view = view.BrowserView(url)
        browser = ReloadingBrowser("/fake", on_frame=lambda image: None)
        browser_view._browser = browser  # type: ignore[assignment]
        browser_view.reload_if_local()
        assert browser.calls == []
        assert browser_view._set_phase("starting")
        browser_view.reload_if_local()
        assert browser.calls == []
        assert browser_view._set_phase("running")
        browser_view.reload_if_local()
        assert (("reload",) in browser.calls) is local, url


class KeyboardBrowser(FakeBrowser):
    field = ""

    def key(self, key, *, text="", code="", key_code=0, modifiers=0):
        self.calls.append(("key", key, text, modifiers))

    def insert_text(self, text):
        self.calls.append(("paste", text))

    def focused_field_type(self):
        return KeyboardBrowser.field


async def _with_view_and_input(monkeypatch, browser_class, test, *, opened_by_agent=False, allow=None):
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Input

    FakeBrowser.made.clear()
    monkeypatch.setattr(cdp, "Browser", browser_class)
    monkeypatch.setattr(install, "find_browser", lambda: "/fake/chrome")
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"
        BINDINGS = [Binding("ctrl+f", "find", "Find", priority=True)]
        found = 0

        def action_find(self) -> None:
            self.found += 1

        def compose(self) -> ComposeResult:
            yield view.BrowserView("https://example.com", opened_by_agent=opened_by_agent, allow=allow)
            yield Input()

    try:
        async with One().run_test(size=(80, 30)) as pilot:
            await test(pilot)
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


async def test_keyboard_goes_to_the_page_until_escape_then_enter(monkeypatch):
    from textual import events
    from textual.widgets import Input, Static

    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        browser = FakeBrowser.made[0]
        page = pilot.app.query_one(view.BrowserView)._page_image()
        assert not page.has_focus
        pilot.app.screen.focus_next()
        await pilot.pause()
        assert not page.has_focus

        await pilot.click(type(page), offset=(5, 2))
        await pilot.pause(0.1)
        assert page.has_focus
        assert "keys go to the page" in str(pilot.app.query_one(view.BrowserHeader).render())
        await pilot.press("h", "i", "enter", "tab")
        await pilot.pause(0.1)
        pressed = [call[1] for call in browser.calls if call[0] == "key"]
        assert pressed == ["h", "i", "Enter", "Tab"] and page.has_focus

        await pilot.press("ctrl+f")
        await pilot.pause(0.1)
        assert pilot.app.found == 0 and browser.calls[-1][:2] == ("key", "f")

        page.post_message(events.Paste("pasted text"))
        await pilot.pause(0.1)
        assert ("paste", "pasted text") in browser.calls

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, view.LeaveBrowserScreen)
        lines = list(pilot.app.screen.query(Static))
        assert lines and all(line.size.width > 0 for line in lines)
        shown = "\n".join(str(line.render()) for line in lines)
        assert "Leave the browser view?" in shown and "Enter" in shown and "Esc" in shown

        await pilot.press("escape")  # stay
        await pilot.pause(0.1)
        assert not isinstance(pilot.app.screen, view.LeaveBrowserScreen) and page.has_focus
        assert "escape" not in [call[1] for call in browser.calls if call[0] == "key"]

        await pilot.press("escape", "enter")  # leave
        await pilot.pause(0.1)
        assert not page.has_focus
        assert "keys go to the page" not in str(pilot.app.query_one(view.BrowserHeader).render())

        await pilot.click(type(page), offset=(5, 2))
        await pilot.pause(0.1)
        assert page.has_focus
        await pilot.click(Input)
        await pilot.pause(0.1)
        assert not page.has_focus
        await pilot.press("ctrl+f")
        await pilot.pause(0.1)
        assert pilot.app.found == 1

    await _with_view_and_input(monkeypatch, KeyboardBrowser, check)


async def test_password_field_warnings_and_focus_reporting(monkeypatch):
    def gate(url: str) -> bool:
        return False

    async def check_reported(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        [browser] = FakeBrowser.made
        assert browser.options["allow"] is gate
        report = browser.options["on_field"]
        header = pilot.app.query_one(view.BrowserHeader)
        threading.Thread(target=report, args=("password",)).start()
        assert await wait_for(pilot, lambda: "think twice before typing a password" in str(header.render()))
        threading.Thread(target=report, args=("text",)).start()
        assert await wait_for(pilot, lambda: "think twice" not in str(header.render()))

        # Fallback click-time focused_field_type() check when browser.on_field is None.
        KeyboardBrowser.field = "password"
        page = pilot.app.query_one(view.BrowserView)._page_image()
        await pilot.click(type(page), offset=(5, 2))
        assert await wait_for(pilot, lambda: "think twice before typing a password" in str(header.render()))
        KeyboardBrowser.field = "text"
        await pilot.click(type(page), offset=(5, 2))
        assert await wait_for(pilot, lambda: "think twice" not in str(header.render()))

    await _with_view_and_input(monkeypatch, KeyboardBrowser, check_reported, opened_by_agent=True, allow=gate)


class WatchingBrowser(KeyboardBrowser):
    asked = 0

    def __init__(self, *args, **options):
        super().__init__(*args, **options)
        self.on_field = options.get("on_field")

    def focused_field_type(self):
        WatchingBrowser.asked += 1
        return "password"


async def test_a_click_doesnt_second_guess_a_browser_that_reports_focus(monkeypatch):
    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        WatchingBrowser.asked = 0
        page = pilot.app.query_one(view.BrowserView)._page_image()
        await pilot.click(type(page), offset=(5, 2))
        await pilot.pause(0.3)
        assert WatchingBrowser.asked == 0
        assert "think twice" not in str(pilot.app.query_one(view.BrowserHeader).render())

    await _with_view_and_input(monkeypatch, WatchingBrowser, check, opened_by_agent=True)


async def test_a_page_the_user_opened_isnt_gated_or_watched(monkeypatch):
    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        [browser] = FakeBrowser.made
        assert browser.options == {"allow": None, "on_field": None}

    await _with_view_and_input(monkeypatch, KeyboardBrowser, check)


class FailingBrowser(FakeBrowser):
    def start(self, url, size):
        raise RuntimeError("couldn't start the browser (boom)")

    def stop(self):
        raise RuntimeError("stopping went wrong too")


async def test_failed_start_is_reported_and_can_retry(monkeypatch):
    async def check(pilot):
        browser_view = pilot.app.query_one(view.BrowserView)
        assert await wait_for(pilot, lambda: browser_view.started is not None and browser_view.started.done())
        assert browser_view.started.result() == (False, "couldn't start the browser (boom)")
        assert browser_view.phase == "failed" and browser_view.browser is None

        first_started = browser_view.started
        monkeypatch.setattr(cdp, "Browser", FakeBrowser)
        assert browser_view.retry()
        assert browser_view.started is not first_started
        assert await wait_for(pilot, lambda: browser_view.phase == "running")
        assert browser_view.started.result() == (True, "")

    await _with_view(monkeypatch, FailingBrowser, check)


async def test_input_ignored_until_browser_is_running_and_session_ready(monkeypatch):
    from pixcell.live import LiveImage

    raw = Browser("/fake", on_frame=lambda _: None)
    with pytest.raises(BrowserError, match="ready"):
        raw.page("Input.insertText", {"text": "early"}, wait=False)

    entered = threading.Event()
    release = threading.Event()

    class GatedBrowser(KeyboardBrowser, ReloadingBrowser):
        def start(self, url, size):
            entered.set()
            release.wait(5)
            super().start(url, size)

    async def check(pilot):
        browser_view = pilot.app.query_one(view.BrowserView)
        try:
            assert await wait_for(pilot, entered.is_set)
            [browser] = FakeBrowser.made
            assert browser_view.phase == "starting"
            page = pilot.app.query_one(LiveImage)
            browser_view.on_live_image_clicked(LiveImage.Clicked(page, 10, 10, 1))
            browser_view.on_live_image_scrolled(LiveImage.Scrolled(page, 10, 10, 1))
            browser_view._send_scroll()
            browser_view.send_key("a", "a")
            browser_view.paste("early")
            browser_view.reload_if_local()
            assert browser.calls == []
        finally:
            release.set()

        assert await wait_for(pilot, lambda: browser_view.phase == "running")
        page = pilot.app.query_one(LiveImage)
        browser_view.on_live_image_clicked(LiveImage.Clicked(page, 12, 14, 1))
        browser_view.send_key("a", "a")
        browser_view.paste("ready")
        browser_view.reload_if_local()
        kinds = [call[0] for call in browser.calls]
        assert {"click", "key", "paste", "reload"} <= set(kinds)

    await _with_view(monkeypatch, GatedBrowser, check, url="http://localhost:3000/")


class ThreadNotingBrowser(FakeBrowser):
    def scroll(self, x, y, delta):
        self.calls.append(("scroll", delta, threading.current_thread() is threading.main_thread()))


async def test_scroll_steps_go_out_in_order_off_the_ui_thread(monkeypatch):
    from pixcell.live import LiveImage

    async def check(pilot):
        assert await wait_for(pilot, lambda: pilot.app.query_one(view.BrowserView).phase == "running")
        [browser] = FakeBrowser.made
        page = pilot.app.query_one(LiveImage)
        for delta in (1, 1, -1):
            page.post_message(LiveImage.Scrolled(page, 10, 10, delta))
            await pilot.pause(0.1)
        await pilot.pause(0.2)
        scrolls = [call[1:] for call in browser.calls if call[0] == "scroll"]
        assert [delta for delta, _ in scrolls] == [40, 40, -40]
        assert not any(on_main for _, on_main in scrolls)

    await _with_view(monkeypatch, ThreadNotingBrowser, check)


class BusyPageBrowser(FakeBrowser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = threading.Event()
        self.release = threading.Event()

    def scroll(self, x, y, delta):
        self.calls.append(("scroll", delta))
        if len([call for call in self.calls if call[0] == "scroll"]) == 1:
            self.started.set()
            self.release.wait(5)


async def test_wheel_input_during_a_slow_step_merges_into_one(monkeypatch):
    from pixcell.live import LiveImage

    async def check(pilot):
        browser_view = pilot.app.query_one(view.BrowserView)
        assert await wait_for(pilot, lambda: browser_view.phase == "running")
        [browser] = FakeBrowser.made
        page = pilot.app.query_one(LiveImage)

        def pending_pixels():
            with browser_view._scroll_lock:
                return browser_view._scroll_pending[2]

        try:
            page.post_message(LiveImage.Scrolled(page, 10, 10, 1))
            assert await wait_for(pilot, browser.started.is_set)
            for index in range(8):
                page.post_message(LiveImage.Scrolled(page, 10, 10, 1))
                assert await wait_for(pilot, lambda: pending_pixels() == (index + 1) * 40)
        finally:
            browser.release.set()
        assert await wait_for(pilot, lambda: not browser_view._scroll_busy)
        scrolls = [call[1] for call in browser.calls if call[0] == "scroll"]
        assert scrolls == [40, 320]

    await _with_view(monkeypatch, BusyPageBrowser, check)


def test_browser_phase_moves():
    phases = get_args(view.BrowserPhase)
    assert set(view._PHASE_MOVES) == set(phases)
    for source in phases:
        for target in phases:
            browser_view = view.BrowserView("https://example.com")
            browser_view._phase = source
            allowed = target in view._PHASE_MOVES[source]
            assert browser_view._set_phase(target) is allowed, (source, target)
            assert browser_view.phase == (target if allowed else source)


async def test_browser_view_from_html_cleans_up_staged_file(monkeypatch):
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult

    FakeBrowser.made.clear()
    monkeypatch.setattr(cdp, "Browser", FakeBrowser)
    monkeypatch.setattr(install, "find_browser", lambda: "/fake/chrome")
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    doc = HTML(html="<h1>Hello View</h1>", title="Hello")
    html_view = doc.to_browser_view()
    staged_path = policy.file_path(html_view.url)
    assert staged_path is not None and staged_path.is_file()
    assert html_view.policy is not None and html_view.policy.no_network is True

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"

        def compose(self) -> ComposeResult:
            yield html_view

    try:
        async with One().run_test(size=(80, 30)) as pilot:
            assert await wait_for(pilot, lambda: html_view.phase == "running")
            assert FakeBrowser.made[0].options.get("no_network") is True
            html_view.close()
            assert await wait_for(pilot, lambda: not staged_path.parent.exists())
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


async def test_view_without_graphics_reports_unavailable():
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult

    pixcell_textual.configure(Capabilities("halfblock"))

    class One(App):
        def compose(self) -> ComposeResult:
            yield view.BrowserView("https://example.com")

    async with One().run_test(size=(80, 20)) as pilot:
        browser_view = pilot.app.query_one(view.BrowserView)
        assert await wait_for(pilot, lambda: browser_view.started is not None and browser_view.started.done())
        ok, reason = browser_view.started.result()
        assert not ok and "pictures" in reason
        assert browser_view.phase == "failed"
