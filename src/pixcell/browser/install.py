"""A browser for :mod:`pixcell.browser`, downloaded on demand when none is installed.

Ported from ``deftai/console`` (MIT), adapted to stdlib ``urllib`` and a configurable
pixcell user cache directory.

The browser view and headless :class:`~pixcell.browser.Browser` drive a Chromium-family
browser (Chrome, Chromium, Edge, Brave, Vivaldi, or ``PIXCELL_BROWSER``). When the user
has none, :func:`install` can fetch Google's Chrome headless shell — the Chrome for
Testing build made for headless automation (about 100 MB) — into pixcell's cache folder
(override with ``cache_dir=...``, :func:`set_cache_dir`, or ``PIXCELL_BROWSER_CACHE_DIR``).

Versions come from Google's Chrome for Testing list (the current stable one). Each is
unpacked into its own folder next to a ``ready`` marker, written last, so an interrupted
download is never used.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pixcell.browser import _portability

VERSIONS_URL = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
PRODUCT = "chrome-headless-shell"
READY = "ready"
DOWNLOAD_PREFIX = "https://storage.googleapis.com/chrome-for-testing-public/"
"""Where Google keeps Chrome for Testing's builds. A download from anywhere else isn't
run: the list gives no checksums, so this is what ties a download to Google (the bucket
in the path too, as anyone can have one on storage.googleapis.com)."""

MAX_REDIRECTS = 5
"""Redirects a download may take (each to Google's builds only)."""

_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
    "vivaldi",
    "chrome-headless-shell",
)

_configured_cache_dir: Path | None = None


class InstallError(Exception):
    """The browser couldn't be downloaded or unpacked (with a short reason)."""


def set_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """Configure the default directory where downloaded Chrome builds are cached."""
    global _configured_cache_dir
    _configured_cache_dir = Path(path).expanduser() if path is not None else None


def folder(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    """Where downloaded browsers live (pixcell's user cache directory by default)."""
    if cache_dir is not None:
        return Path(cache_dir).expanduser()
    if _configured_cache_dir is not None:
        return _configured_cache_dir
    if env := os.environ.get("PIXCELL_BROWSER_CACHE_DIR"):
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "pixcell" / "browsers"
    if xdg := os.environ.get("XDG_CACHE_HOME"):
        return Path(xdg).expanduser() / "pixcell" / "browsers"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pixcell" / "browsers"
    return Path.home() / ".cache" / "pixcell" / "browsers"


def _resolve_folder(cache_dir: str | os.PathLike[str] | None) -> Path:
    return Path(cache_dir).expanduser() if cache_dir is not None else folder()


@contextmanager
def _work_folder(parent: Path) -> Iterator[Path]:
    """A throwaway folder in ``parent``, for a download to be unpacked in and moved out of.

    Not ``tempfile``'s: its folders have their own permissions, which what's moved out keeps.
    """
    path = parent / f"download-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _check_source(url: str) -> None:
    """Refuse a download URL that isn't one of Google's Chrome for Testing builds."""
    parts = urllib.parse.urlsplit(str(url))
    expected = urllib.parse.urlsplit(DOWNLOAD_PREFIX)
    if (
        parts.scheme != expected.scheme
        or parts.netloc.lower() != expected.netloc
        or not parts.path.startswith(expected.path)
        or ".." in urllib.parse.unquote(parts.path).split("/")
    ):
        raise InstallError(f"refusing a download from outside Google's Chrome for Testing ({url})")


def platform_name() -> str | None:
    """Chrome for Testing's name for this machine, or None if it has no build for it."""
    return _portability.chrome_platform()


def _executable(root: Path, name: str) -> Path:
    suffix = ".exe" if name.startswith("win") else ""
    return root / f"{PRODUCT}-{name}" / f"{PRODUCT}{suffix}"


def installed(*, cache_dir: str | os.PathLike[str] | None = None) -> str | None:
    """The newest downloaded browser that finished unpacking, or None."""
    name = platform_name()
    if name is None:
        return None
    try:
        versions = [path for path in _resolve_folder(cache_dir).iterdir() if (path / READY).is_file()]
    except OSError:
        return None

    def version_key(path: Path) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in path.name.split("."))
        except ValueError:
            return (0,)

    for root in sorted(versions, key=version_key, reverse=True):
        executable = _executable(root, name)
        if executable.is_file():
            return str(executable)
    return None


def find_browser(*, cache_dir: str | os.PathLike[str] | None = None) -> str | None:
    """A headless-capable Chromium-family browser on this machine, or None.

    Checks ``PIXCELL_BROWSER``, standard OS application locations, ``PATH``, and finally
    any Chrome headless shell downloaded into ``cache_dir``.
    """
    if (named := os.environ.get("PIXCELL_BROWSER")) and Path(named).is_file():
        return named
    for binary in _portability.browser_locations():
        if binary.is_file():
            return str(binary)
    for command in _COMMANDS:
        if found := shutil.which(command):
            return found
    return installed(cache_dir=cache_dir)


find_browser.cache_clear = lambda: None  # type: ignore[attr-defined]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass
class _Response:
    status: int
    headers: Mapping[str, str]
    _body: Any = None

    @property
    def is_redirect(self) -> bool:
        return self.status in (301, 302, 303, 307, 308)

    def iter_bytes(self, chunk_size: int = 1 << 16) -> Iterator[bytes]:
        while self._body is not None:
            chunk = self._body.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def read(self) -> bytes:
        return b"".join(self.iter_bytes())


@contextmanager
def _stream(
    method: str,
    url: str,
    *,
    timeout: float = 60,
    follow_redirects: bool = False,
) -> Iterator[_Response]:
    """Open an HTTP(S) request using stdlib ``urllib.request``."""
    request = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect())
    try:
        raw = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            headers = {key.lower(): value for key, value in error.headers.items()}
            if not follow_redirects and error.code in (301, 302, 303, 307, 308):
                yield _Response(error.code, headers, None)
                return
            raise
        finally:
            with suppress(Exception):
                error.close()
    try:
        headers = {key.lower(): value for key, value in raw.headers.items()}
        yield _Response(getattr(raw, "status", 200), headers, raw)
    finally:
        with suppress(Exception):
            raw.close()


def latest(*, platform_override: str | None = None) -> tuple[str, str, int]:
    """The current stable version: ``(version, download URL, size in bytes or 0)``."""
    name = platform_override if platform_override is not None else platform_name()
    if name is None:
        raise InstallError(f"Google has no Chrome headless shell for this machine ({platform.platform()})")
    try:
        with _stream("GET", VERSIONS_URL, timeout=30, follow_redirects=True) as response:
            data = json.loads(response.read().decode("utf-8"))
        stable = data["channels"]["Stable"]
        version = str(stable["version"])
        url = next(str(item["url"]) for item in stable["downloads"][PRODUCT] if item["platform"] == name)
    except StopIteration:
        raise InstallError(f"Google lists no Chrome headless shell for {name}") from None
    except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError) as error:
        raise InstallError(f"couldn't read Google's list of versions ({error})") from None
    _check_source(url)
    size = 0
    try:
        with _stream("HEAD", url, timeout=30, follow_redirects=False) as head:
            if not head.is_redirect:
                size = int(head.headers.get("content-length", 0) or 0)
    except (OSError, urllib.error.URLError, ValueError):
        pass
    return version, url, size


def install(
    on_progress: Callable[[int, int], None] | None = None,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Download and unpack the current stable Chrome headless shell, if it isn't here
    already; returns its executable. Blocking: use a thread. ``on_progress(done, total)``
    is called as it downloads (``total`` is 0 when unknown).

    Two downloads at once take turns: the second waits for the first, then finds its copy.
    A finished copy is never replaced.

    Raises:
        InstallError: With a short reason.
    """
    version, url, size = latest()
    name = platform_name() or ""
    target_folder = _resolve_folder(cache_dir)
    try:
        _portability.make_private_folder(target_folder)
        with _locked(target_folder / ".lock"):
            path = _install_locked(target_folder, version, url, size, name, on_progress)
    except OSError as error:  # the cache folder isn't writable, the disk is full...
        raise InstallError(f"couldn't save the browser ({error.strerror or error})") from None
    return path


def _install_locked(
    target_folder: Path,
    version: str,
    url: str,
    size: int,
    name: str,
    on_progress: Callable[[int, int], None] | None,
) -> str:
    root = target_folder / version
    if (root / READY).is_file() and _executable(root, name).is_file():
        return str(_executable(root, name))  # here already (perhaps another process's)
    with _work_folder(target_folder) as work:
        archive = work / "browser.zip"
        try:
            # Redirects one at a time, each checked before it's followed: one elsewhere
            # is never even asked for.
            for _ in range(MAX_REDIRECTS + 1):
                _check_source(url)
                with _stream("GET", url, timeout=60, follow_redirects=False) as response:
                    if response.is_redirect:
                        url = urllib.parse.urljoin(url, response.headers["location"])
                        continue
                    total = int(response.headers.get("content-length", size) or 0)
                    done = 0
                    with archive.open("wb") as out:
                        for chunk in response.iter_bytes(1 << 16):
                            out.write(chunk)
                            done += len(chunk)
                            if on_progress is not None:
                                on_progress(done, total)
                    break
            else:
                raise InstallError("the download was redirected too many times")
        except (OSError, urllib.error.URLError, KeyError) as error:  # KeyError: redirect without Location
            raise InstallError(f"the download failed ({error})") from None
        unpacked = work / "unpacked"
        try:
            _unzip(archive, unpacked)
        except zipfile.BadZipFile as error:
            raise InstallError(f"couldn't unpack the browser ({error})") from None
        if not _executable(unpacked, name).is_file():
            raise InstallError("the download didn't contain the browser")
        (unpacked / READY).write_text(url + "\n", "utf-8")  # last: only a complete copy counts
        if root.exists():  # an unfinished copy (never a ready one: that returned above)
            shutil.rmtree(root)
        unpacked.replace(root)
    return str(_executable(root, name))


def _locked(path: Path) -> AbstractContextManager[None]:
    """Hold an exclusive lock on ``path`` (across processes and threads)."""
    return _portability.lock_file(path)


def _unzip(archive: Path, target: Path) -> None:
    """Unpack, keeping the files' Unix permissions (zipfile drops them), and refusing
    paths that would land outside ``target``."""
    target.mkdir(parents=True)
    base = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            destination = (target / member.filename).resolve()
            if base not in destination.parents and destination != base:
                raise zipfile.BadZipFile(f"unsafe path in archive: {member.filename}")
            mode = (member.external_attr >> 16) & 0o777
            if stat.S_ISLNK(member.external_attr >> 16):
                link = bundle.read(member).decode()
                resolved_link = (destination.parent / link).resolve()
                if resolved_link != base and base not in resolved_link.parents:
                    raise zipfile.BadZipFile(f"unsafe link in archive: {member.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(link)
                continue
            bundle.extract(member, target)
            if mode and not member.is_dir():
                destination.chmod(mode)


class _CallableInstallModule(sys.modules[__name__].__class__):  # type: ignore[misc]
    """Lets ``pixcell.browser.install`` work both as the submodule and as ``pixcell.browser.install()``."""

    def __call__(
        self,
        on_progress: Callable[[int, int], None] | None = None,
        *,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> str:
        return self.install(on_progress, cache_dir=cache_dir)


sys.modules[__name__].__class__ = _CallableInstallModule

