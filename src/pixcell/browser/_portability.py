"""OS-specific helpers for :mod:`pixcell.browser` (POSIX and Windows, stdlib only).

Ported from ``deftai/console`` (MIT). Contains only the process-spawning, file-locking,
browser-discovery, and ``file:`` URI helpers needed by the browser package.
"""

from __future__ import annotations

import errno
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import unquote


def _path_from_uri(uri: str) -> Path:
    """``Path.from_uri`` (Python 3.13+), with the same rules on 3.11 and 3.12."""
    if hasattr(Path, "from_uri"):
        return Path.from_uri(uri)
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    parts = urlsplit(uri)
    if parts.scheme.lower() != "file":
        raise ValueError(f"URI does not start with 'file:': {uri!r}")
    if parts.netloc and parts.netloc.lower() != "localhost":
        if sys.platform != "win32":
            raise ValueError(f"URI is not local: {uri!r}")
        path = Path("\\\\" + parts.netloc + url2pathname(parts.path))
    else:
        path = Path(url2pathname(parts.path))
    if not path.is_absolute():
        raise ValueError(f"URI is not absolute: {uri!r}")
    return path

if sys.platform == "win32":
    import msvcrt

    SYSTEM_TEMP: Path | None = None
    """No shared temp folder beyond the user's own on Windows."""

    def make_private_folder(path: Path) -> None:
        """Make a folder (and its parents) that only this user can use, if it isn't there."""
        path.mkdir(exist_ok=True, parents=True)

    def path_from_file_uri(uri: str) -> Path:
        """The local path a ``file:`` URI names (``file:///C:/a%20b`` is ``C:\\a b``). Also takes
        ``file://C:\\a``; ValueError if it doesn't name an absolute local path."""
        rest = uri[len("file://") :]
        if uri[: len("file://")].lower() == "file://" and re.match(r"[a-zA-Z]:[\\/]", rest):
            return Path(unquote(rest))
        path = _path_from_uri(uri)
        if str(path).startswith("\\\\"):
            raise ValueError(f"refusing remote UNC file URI: {uri}")
        return path

    def browser_locations() -> list[Path]:
        """Where Chromium-family browsers install themselves (Edge comes with Windows)."""
        roots = {
            "ProgramFiles": [("Google", "Chrome"), ("Microsoft", "Edge"), ("BraveSoftware", "Brave-Browser")],
            "ProgramFiles(x86)": [("Google", "Chrome"), ("Microsoft", "Edge"), ("BraveSoftware", "Brave-Browser")],
            "LocalAppData": [("Google", "Chrome"), ("Chromium",), ("BraveSoftware", "Brave-Browser")],
        }
        programs = {
            "Chrome": "chrome.exe",
            "Chromium": "chrome.exe",
            "Edge": "msedge.exe",
            "Brave-Browser": "brave.exe",
        }
        found: list[Path] = []
        for variable, folders in roots.items():
            if not (root := os.environ.get(variable)):
                continue
            for folder in folders:
                found.append(Path(root, *folder, "Application", programs[folder[-1]]))
        return found

    def chrome_platform() -> str | None:
        """Chrome for Testing's name for this machine (a 64-bit build runs on Arm too)."""
        return "win64" if platform.machine().lower() in ("amd64", "x86_64", "arm64") else "win32"

    _spawned: dict[int, subprocess.Popen[bytes]] = {}
    """``spawn_with_pipe`` children, kept so ``stop_spawned`` can reap them."""

    def spawn_with_pipe(executable: str, arguments: list[str]) -> tuple[int, int, int]:
        """Start ``executable`` in its own process group with no stdin or output, and a pipe
        pair Chrome takes as ``--remote-debugging-io-pipes=<read handle>,<write handle>``.

        Returns:
            ``(pid, to_child, from_child)``: write commands to ``to_child``, read replies
            from ``from_child``. ``stop_spawned`` stops it; the caller closes both.
        """
        to_child_read, to_child = os.pipe()
        from_child, from_child_write = os.pipe()
        handles = [msvcrt.get_osfhandle(to_child_read), msvcrt.get_osfhandle(from_child_write)]
        try:
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": handles}
            process = subprocess.Popen(
                [*arguments, f"--remote-debugging-io-pipes={handles[0]},{handles[1]}"],
                executable=executable,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startup,
                close_fds=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except BaseException:
            os.close(to_child)
            os.close(from_child)
            raise
        finally:
            os.close(to_child_read)
            os.close(from_child_write)
        _spawned[process.pid] = process
        return process.pid, to_child, from_child

    def stop_spawned(pid: int, grace: float = 3.0) -> None:
        """Stop a ``spawn_with_pipe`` child and everything it started. Blocking."""
        process = _spawned.pop(pid, None)
        deadline = time.monotonic() + grace
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.5, grace),
                check=False,
            )
        if process is not None:
            with suppress(OSError, subprocess.SubprocessError):
                if process.poll() is None:
                    process.kill()
                process.wait(max(0.0, deadline - time.monotonic()))

    @contextmanager
    def lock_file(path: Path) -> Iterator[None]:
        """Hold an exclusive lock on ``path`` (across processes and threads); waits for it."""
        with path.open("a+b") as handle:
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as error:
                    if error.errno != errno.EDEADLOCK:
                        raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl
    import signal

    SYSTEM_TEMP = Path("/tmp").resolve()
    """/tmp as well as the user's temp folder (macOS gives each user its own)."""

    _MAC_APPS = ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Vivaldi")

    def make_private_folder(path: Path) -> None:
        """Make a folder (and its parents) that only this user can use, if it isn't there."""
        path.mkdir(0o700, exist_ok=True, parents=True)

    def path_from_file_uri(uri: str) -> Path:
        """The local path a ``file:`` URI names (``file:///a%20b`` is ``/a b``). ValueError if
        it doesn't name an absolute local path."""
        return _path_from_uri(uri)

    def browser_locations() -> list[Path]:
        """Where Chromium-family browsers are installed outside the PATH (macOS app bundles)."""
        if sys.platform != "darwin":
            return []
        return [
            root / f"{app}.app" / "Contents" / "MacOS" / app
            for app in _MAC_APPS
            for root in (Path("/Applications"), Path.home() / "Applications")
        ]

    def chrome_platform() -> str | None:
        """Chrome for Testing's name for this machine, or None if it has no build for it."""
        arm = platform.machine().lower() in ("arm64", "aarch64")
        if sys.platform == "darwin":
            return "mac-arm64" if arm else "mac-x64"
        if sys.platform.startswith("linux"):
            return "linux-arm64" if arm else "linux64"
        return None

    def _high(fd: int) -> int:
        """The same file, on a descriptor numbered 10 or more (the original is closed)."""
        moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(fd)
        return moved

    def spawn_with_pipe(executable: str, arguments: list[str]) -> tuple[int, int, int]:
        """Start ``executable`` in its own process group with no stdin or output, and a pipe
        pair on its descriptors 3 (it reads) and 4 (it writes), as Chrome's
        ``--remote-debugging-pipe`` wants. No shell and no pre-exec hook are involved.

        Returns:
            ``(pid, to_child, from_child)``: write commands to ``to_child``, read replies
            from ``from_child``. ``stop_spawned`` stops it; the caller closes both.
        """
        to_child_read, to_child = os.pipe()
        from_child, from_child_write = os.pipe()
        # The child's ends well away from 3 and 4, so moving them there can't collide.
        to_child_read, from_child_write = _high(to_child_read), _high(from_child_write)
        devnull = os.devnull
        actions = [
            (os.POSIX_SPAWN_OPEN, 0, devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_DUP2, to_child_read, 3),
            (os.POSIX_SPAWN_DUP2, from_child_write, 4),
        ]
        try:
            pid = os.posix_spawnp(executable, arguments, os.environ, file_actions=actions, setpgroup=0)
        except BaseException:
            os.close(to_child)
            os.close(from_child)
            raise
        finally:
            os.close(to_child_read)
            os.close(from_child_write)
        return pid, to_child, from_child

    def _reaped(pid: int, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a child to exit (and collect it)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if done:
                return True
            time.sleep(0.05)
        return False

    def stop_spawned(pid: int, grace: float = 3.0) -> None:
        """Stop a ``spawn_with_pipe`` child and its process group: asked first, then (after
        ``grace`` seconds) killed. Blocking."""
        with suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
        if not _reaped(pid, grace):
            with suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
            _reaped(pid, 2.0)

    @contextmanager
    def lock_file(path: Path) -> Iterator[None]:
        """Hold an exclusive lock on ``path`` (across processes and threads); waits for it."""
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
