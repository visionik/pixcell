"""Tests for pixcell.browser.install: URL selection, cache directory config, unpacking, and DownloadOffer."""

from __future__ import annotations

import io
import json
import stat
import sys
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from pixcell.browser import cdp, install, view
from test_browser import FakeBrowser, wait_for


def posix_only(reason: str):
    return pytest.mark.skipif(sys.platform == "win32", reason=reason)


def _zip(entries: dict[str, tuple[bytes, int]], links: dict[str, str] | None = None) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        for name, (body, mode) in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            bundle.writestr(info, body)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, target)
    return data.getvalue()


@pytest.fixture
def mac(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "platform_name", lambda: "mac-arm64")
    monkeypatch.setattr(install, "folder", lambda cache_dir=None: Path(cache_dir) if cache_dir else tmp_path / "browsers")
    yield tmp_path / "browsers"


GOOGLE_ZIP = "https://storage.googleapis.com/chrome-for-testing-public/157.0.1.2/mac-arm64/b.zip"
SHELL = "chrome-headless-shell-mac-arm64/chrome-headless-shell"


# --- Cache directory & URL selection -------------------------------------------------------


def test_cache_dir_defaults_to_pixcell_and_is_configurable(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PIXCELL_BROWSER_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    install.set_cache_dir(None)
    try:
        default = install.folder()
        assert "pixcell" in default.parts
        assert "deft" not in str(default).lower() and "toad" not in str(default).lower()

        if sys.platform != "win32":
            monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
            assert install.folder() == tmp_path / "xdg" / "pixcell" / "browsers"

        monkeypatch.setenv("PIXCELL_BROWSER_CACHE_DIR", str(tmp_path / "env-cache"))
        assert install.folder() == tmp_path / "env-cache"

        install.set_cache_dir(tmp_path / "configured-cache")
        assert install.folder() == tmp_path / "configured-cache"
        assert install.folder(tmp_path / "explicit-arg") == tmp_path / "explicit-arg"
    finally:
        install.set_cache_dir(None)


def test_latest_selects_platform_url_and_validates_google_bucket(monkeypatch):
    catalog = {
        "channels": {
            "Stable": {
                "version": "157.0.1.2",
                "downloads": {
                    "chrome-headless-shell": [
                        {
                            "platform": "linux64",
                            "url": "https://storage.googleapis.com/chrome-for-testing-public/157.0.1.2/linux64/chrome-headless-shell-linux64.zip",
                        },
                        {
                            "platform": "mac-arm64",
                            "url": GOOGLE_ZIP,
                        },
                        {
                            "platform": "win64",
                            "url": "https://storage.googleapis.com/chrome-for-testing-public/157.0.1.2/win64/chrome-headless-shell-win64.zip",
                        },
                    ]
                },
            }
        }
    }
    payload = json.dumps(catalog).encode("utf-8")

    @contextmanager
    def fake_stream(method: str, url: str, *, timeout: float = 60, follow_redirects: bool = False):
        if method == "GET" and url == install.VERSIONS_URL:
            yield install._Response(200, {}, io.BytesIO(payload))
        elif method == "HEAD":
            yield install._Response(200, {"content-length": "98765432"}, io.BytesIO(b""))
        else:
            raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(install, "_stream", fake_stream)
    version, url, size = install.latest(platform_override="linux64")
    assert version == "157.0.1.2"
    assert url.endswith("/linux64/chrome-headless-shell-linux64.zip")
    assert size == 98765432

    version_win, url_win, _ = install.latest(platform_override="win64")
    assert version_win == "157.0.1.2" and url_win.endswith("/win64/chrome-headless-shell-win64.zip")

    with pytest.raises(install.InstallError, match="no Chrome headless shell for freebsd"):
        install.latest(platform_override="freebsd")

    # Tampered URL outside Google's Chrome-for-Testing bucket is rejected even in latest().
    bad_catalog = {
        "channels": {
            "Stable": {
                "version": "157.0.1.2",
                "downloads": {
                    "chrome-headless-shell": [
                        {"platform": "linux64", "url": "https://storage.googleapis.com/other-bucket/b.zip"}
                    ]
                },
            }
        }
    }

    @contextmanager
    def bad_stream(method: str, url: str, **kwargs):
        yield install._Response(200, {}, io.BytesIO(json.dumps(bad_catalog).encode("utf-8")))

    monkeypatch.setattr(install, "_stream", bad_stream)
    with pytest.raises(install.InstallError, match="outside Google"):
        install.latest(platform_override="linux64")


# --- Archive download, verification & locking ----------------------------------------------


def _serve(monkeypatch, archive: bytes, version: str = "157.0.1.2", url: str = GOOGLE_ZIP, landed: str | None = None):
    monkeypatch.setattr(install, "latest", lambda: (version, url, len(archive)))
    asked: list[str] = []

    @contextmanager
    def stream(method: str, asked_for: str, *, timeout: float = 60, follow_redirects: bool = False):
        assert follow_redirects is False, "each redirect must be checked before it's followed"
        asked.append(asked_for)
        if landed is not None and asked_for == url:
            yield install._Response(302, {"location": landed}, None)
        else:
            yield install._Response(200, {"content-length": str(len(archive))}, io.BytesIO(archive))

    monkeypatch.setattr(install, "_stream", stream)
    return asked


@posix_only("checks chmod bits and symlinks")
def test_install_unpacks_with_permissions_and_links(mac: Path, monkeypatch, tmp_path: Path):
    archive = _zip(
        {SHELL: (b"#!/bin/sh\n", 0o755), "chrome-headless-shell-mac-arm64/lib/libx.1.dylib": (b"x", 0o644)},
        links={"chrome-headless-shell-mac-arm64/lib/libx.dylib": "libx.1.dylib"},
    )
    _serve(monkeypatch, archive)
    seen = []
    path = Path(install.install(lambda done, total: seen.append((done, total))))
    assert path == mac / "157.0.1.2" / SHELL
    assert path.stat().st_mode & 0o111
    assert (path.parent / "lib" / "libx.dylib").is_symlink()
    assert seen[-1] == (len(archive), len(archive))
    assert install.installed() == str(path)

    monkeypatch.delenv("PIXCELL_BROWSER", raising=False)
    monkeypatch.setattr("pixcell.browser._portability.browser_locations", lambda: [])
    monkeypatch.setattr(install, "_COMMANDS", ())
    assert install.find_browser() == str(path)

    # Explicit PIXCELL_BROWSER env override takes priority.
    override_bin = tmp_path / "my-chrome"
    override_bin.write_text("#!/bin/sh\n", "utf-8")
    monkeypatch.setenv("PIXCELL_BROWSER", str(override_bin))
    assert install.find_browser() == str(override_bin)


def test_install_supports_explicit_cache_dir(mac: Path, monkeypatch, tmp_path: Path):
    import pixcell.browser

    custom_cache = tmp_path / "custom-browsers"
    _serve(monkeypatch, _zip({SHELL: (b"#!/bin/sh\n", 0o755)}))
    installed_path = pixcell.browser.install(cache_dir=custom_cache)
    assert Path(installed_path) == custom_cache / "157.0.1.2" / SHELL
    assert install.installed(cache_dir=custom_cache) == installed_path
    assert install.installed() is None  # default folder untouched


def test_install_refuses_paths_outside_its_folder(mac: Path, monkeypatch):
    _serve(monkeypatch, _zip({SHELL: (b"x", 0o755), "../escape.txt": (b"x", 0o644)}))
    with pytest.raises(install.InstallError, match="unpack"):
        install.install()
    assert not (mac / "escape.txt").exists() and install.installed() is None

    _serve(monkeypatch, _zip({SHELL: (b"x", 0o755)}, links={"chrome-headless-shell-mac-arm64/evil": "../../../../etc"}))
    with pytest.raises(install.InstallError, match="unpack"):
        install.install()


def test_install_only_runs_a_download_from_google(mac: Path, monkeypatch):
    archive = _zip({SHELL: (b"#!/bin/sh\n", 0o755)})
    _serve(monkeypatch, archive, url="https://evil.example/b.zip")
    with pytest.raises(install.InstallError, match="outside Google"):
        install.install()
    assert install.installed() is None

    asked = _serve(monkeypatch, archive, landed="http://10.0.0.1/b.zip")
    with pytest.raises(install.InstallError, match="outside Google"):
        install.install()
    assert install.installed() is None
    assert asked == [GOOGLE_ZIP]

    other = GOOGLE_ZIP.replace("b.zip", "c.zip")
    asked = _serve(monkeypatch, archive, landed=other)
    assert install.install() == str(mac / "157.0.1.2" / SHELL)
    assert asked == [GOOGLE_ZIP, other]


def test_install_stops_after_too_many_redirects(mac: Path, monkeypatch):
    _serve(monkeypatch, _zip({SHELL: (b"x", 0o755)}))

    @contextmanager
    def loop_stream(method: str, asked_for: str, **kwargs):
        yield install._Response(302, {"location": GOOGLE_ZIP}, None)

    monkeypatch.setattr(install, "_stream", loop_stream)
    with pytest.raises(install.InstallError, match="too many"):
        install.install()


def test_an_unfinished_copy_is_never_used(mac: Path):
    partial = mac / "158.0.0.0" / SHELL
    partial.parent.mkdir(parents=True)
    partial.write_text("x", "utf-8")
    assert install.installed() is None
    done = mac / "157.0.0.0" / SHELL
    done.parent.mkdir(parents=True)
    done.write_text("x", "utf-8")
    (mac / "157.0.0.0" / install.READY).write_text("", "utf-8")
    assert install.installed() == str(done)


def test_a_download_missing_the_browser_fails(mac: Path, monkeypatch):
    _serve(monkeypatch, _zip({"something-else/readme.txt": (b"x", 0o644)}))
    with pytest.raises(install.InstallError, match="didn't contain"):
        install.install()
    assert install.installed() is None


def test_two_downloads_at_once_take_turns(mac: Path, monkeypatch):
    archive = _zip({SHELL: (b"#!/bin/sh\n", 0o755)})
    _serve(monkeypatch, archive)
    real_stream = install._stream
    downloads = []

    def counting_stream(*args, **kwargs):
        downloads.append(1)
        return real_stream(*args, **kwargs)

    monkeypatch.setattr(install, "_stream", counting_stream)
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(install.install())) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 3 and len(set(results)) == 1
    assert downloads == [1]
    assert install.installed() == results[0]


def test_an_unwritable_cache_folder_is_an_install_error(mac: Path, monkeypatch, tmp_path: Path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", "utf-8")
    monkeypatch.setattr(install, "folder", lambda cache_dir=None: blocker / "browsers")
    _serve(monkeypatch, _zip({SHELL: (b"x", 0o755)}))
    with pytest.raises(install.InstallError, match="couldn't save"):
        install.install()


# --- DownloadOffer in BrowserView ----------------------------------------------------------


async def _click_offer(pilot, selector: str) -> None:
    assert await wait_for(pilot, lambda: pilot.app.query_one(selector).region.area > 0)
    assert await pilot.click(selector), f"the click missed {selector}"


async def _view_without_a_browser(monkeypatch, test, download: str | None = None):
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult

    FakeBrowser.made.clear()
    found: dict[str, str | None] = {"browser": None}
    monkeypatch.setattr(cdp, "Browser", FakeBrowser)
    monkeypatch.setattr(install, "find_browser", lambda: found["browser"])
    monkeypatch.setattr(install, "platform_name", lambda: "mac-arm64")

    def fake_install(on_progress=None):
        if download is not None:
            raise install.InstallError(download)
        if on_progress:
            on_progress(50, 100)
            on_progress(100, 100)
        found["browser"] = "/downloaded/chrome-headless-shell"
        return found["browser"]

    monkeypatch.setattr(install, "install", fake_install)
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"

        def compose(self) -> ComposeResult:
            yield view.BrowserView("https://example.com", opened_by_agent=True)

    try:
        async with One().run_test(size=(100, 40)) as pilot:
            assert await wait_for(pilot, lambda: bool(pilot.app.query(view.DownloadOffer)))
            await test(pilot)
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


async def test_no_browser_offers_a_download_that_then_starts_the_page(monkeypatch):
    async def check(pilot):
        page = pilot.app.query_one(view.BrowserView)
        assert pilot.app.query(view.DownloadOffer) and not page.started.done()
        await _click_offer(pilot, ".offer-button")
        assert await wait_for(pilot, lambda: page.started.done() and not pilot.app.query(view.DownloadOffer))
        assert page.started.result() == (True, "")
        assert FakeBrowser.made and FakeBrowser.made[0].calls[0][0] == "start"

    await _view_without_a_browser(monkeypatch, check)


async def test_declining_the_download_tells_the_caller(monkeypatch):
    async def check(pilot):
        page = pilot.app.query_one(view.BrowserView)
        await _click_offer(pilot, ".offer-later")
        await pilot.pause(0.1)
        assert not pilot.app.query(view.DownloadOffer)
        ok, note = page.started.result()
        assert not ok and "chose not to download" in note
        assert not FakeBrowser.made

    await _view_without_a_browser(monkeypatch, check)


async def test_a_failed_download_is_reported_and_can_be_retried(monkeypatch):
    import pixcell.textual as pixcell_textual
    from pixcell.probe import Capabilities
    from textual.app import App, ComposeResult

    FakeBrowser.made.clear()
    state: dict[str, object] = {"browser": None, "fail": True}
    monkeypatch.setattr(cdp, "Browser", FakeBrowser)
    monkeypatch.setattr(install, "find_browser", lambda: state["browser"])
    monkeypatch.setattr(install, "platform_name", lambda: "mac-arm64")

    def flaky_install(on_progress=None):
        if state["fail"]:
            raise install.InstallError("no network")
        if on_progress:
            on_progress(100, 100)
        state["browser"] = "/downloaded/chrome-headless-shell"
        return state["browser"]

    monkeypatch.setattr(install, "install", flaky_install)
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"

        def compose(self) -> ComposeResult:
            yield view.BrowserView("https://example.com")

    try:
        async with One().run_test(size=(100, 40)) as pilot:
            page = pilot.app.query_one(view.BrowserView)
            assert await wait_for(pilot, lambda: bool(pilot.app.query(view.DownloadOffer)))
            assert page.phase == "offering"

            first_started = page.started
            await _click_offer(pilot, ".offer-button")
            assert await wait_for(pilot, first_started.done)
            ok, note = first_started.result()
            assert not ok and "no network" in note
            assert page.phase == "failed"
            assert not pilot.app.query_one(view.DownloadOffer).has_class("-downloading")

            state["fail"] = False
            await _click_offer(pilot, ".offer-button")
            assert page.started is not first_started
            assert await wait_for(pilot, lambda: page.started.done() and not pilot.app.query(view.DownloadOffer))
            assert page.started.result() == (True, "")
            assert page.phase == "running"
            assert FakeBrowser.made and FakeBrowser.made[0].calls[0][0] == "start"
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


async def test_the_offer_works_from_the_keyboard(monkeypatch):
    async def check(pilot):
        page = pilot.app.query_one(view.BrowserView)
        later = pilot.app.query_one(".offer-later", view.OfferButton)
        later.focus()
        await pilot.press("enter")
        await pilot.pause(0.1)
        ok, note = page.started.result()
        assert not ok and "chose not to download" in note

    await _view_without_a_browser(monkeypatch, check)
