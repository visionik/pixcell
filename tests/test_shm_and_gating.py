"""Unit tests for the POSIX SHM frame pool (t=s) and TIOCOUTQ output-queue gating."""

from __future__ import annotations

import base64
import errno
import os
import pty
import re
import select
import struct
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw
from textual.app import App, ComposeResult

import pixcell.textual as ct
from pixcell import kitty, probe, shm, ttyqueue
from pixcell.live import LiveImage
from pixcell.probe import Capabilities

POSIX_SHM = shm.available()
requires_posix_shm = pytest.mark.skipif(not POSIX_SHM, reason="POSIX shared memory required")


def _clean_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear SSH, multiplexer, and opt-out env vars for deterministic local tests."""
    for var in (
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "TMUX",
        "STY",
        "PIXCELL_SHM",
        "PIXCELL_OUTPUT_GATING",
        "PIXCELL_OUTPUT_QUEUE_LIMIT",
        "PIXCELL_PLACEHOLDERS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")


def _shm_exists(name: str) -> bool:
    import _posixshmem

    try:
        fd = _posixshmem.shm_open(name, os.O_RDONLY, 0)
    except FileNotFoundError:
        return False
    os.close(fd)
    return True


def _shm_read(name: str, length: int) -> bytes:
    import _posixshmem

    fd = _posixshmem.shm_open(name, os.O_RDONLY, 0)
    try:
        return os.pread(fd, length, 0)
    finally:
        os.close(fd)


# Kitty t=s escape codes --------------------------------------------------------------


def test_kitty_shm_commands():
    name = "/pxc-1-1-1"
    encoded = base64.standard_b64encode(name.encode()).decode("ascii")
    assert kitty.transmit_shm(7, name, 10, 4, 120) == f"\x1b_Ga=t,t=s,i=7,f=24,s=10,v=4,S=120,q=2;{encoded}\x1b\\"
    assert (
        kitty.edit_shm(7, name, 2, 3, 4, 5, 80, fmt=32)
        == f"\x1b_Ga=f,t=s,i=7,r=1,x=2,y=3,f=32,s=4,v=5,S=80,X=1,q=2;{encoded}\x1b\\"
    )
    assert kitty.query_shm(99, name) == f"\x1b_Gi=99,s=1,v=1,S=3,a=q,t=s,f=24;{encoded}\x1b\\"


# ShmPool reuse, pre-faulting, and cleanup --------------------------------------------


def test_size_class_rounds_to_power_of_two():
    assert shm.size_class(1) == shm.MIN_CLASS_BYTES
    assert shm.size_class(shm.MIN_CLASS_BYTES) == shm.MIN_CLASS_BYTES
    assert shm.size_class(shm.MIN_CLASS_BYTES + 1) == shm.MIN_CLASS_BYTES * 2
    assert shm.size_class(640 * 400 * 3) == 1024 * 1024
    with pytest.raises(ValueError):
        shm.size_class(0)
    with pytest.raises(ValueError):
        shm.size_class(-10)


@requires_posix_shm
def test_shm_pool_prefault_hits_and_reuse():
    with shm.ShmPool(ready_per_size=3, background_refill=False) as pool:
        assert pool.warm(1024) == 3
        assert pool.ready_count == 3

        # Release returns an unused segment to the pool so the same segment is reused.
        seg = pool.acquire(1024)
        assert pool.hits == 1
        assert len(seg.name) <= 30  # macOS PSHMNAMLEN safety
        first_name = seg.name
        assert pool.release(seg) is True
        assert pool.ready_count == 3

        # Pop until we see first_name again (FIFO bucket -> 3rd acquire gets it back).
        popped = [pool.acquire(1024) for _ in range(3)]
        assert popped[-1].name == first_name
        for item in popped:
            pool.release(item)

        # Writing payloads hits pre-faulted segments and stores exact bytes.
        payload = b"ABCD" * 500
        name = pool.write(payload)
        assert _shm_exists(name)
        assert _shm_read(name, len(payload)) == payload
        assert pool.pending_count == 1
    # Context manager exit closes and unlinks all ready and pending segments.
    assert not _shm_exists(name)
    assert pool.closed


@requires_posix_shm
def test_shm_pool_background_refill_and_class_eviction():
    with shm.ShmPool(ready_per_size=2, max_classes=2, background_refill=True) as pool:
        names = []
        # First write misses (triggers background refill for 64 KiB class).
        names.append(pool.write(b"x" * 1000))
        deadline = time.monotonic() + 2.0
        while pool.ready_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.ready_count >= 1

        # Next write hits the pre-faulted pool.
        hits_before = pool.hits
        names.append(pool.write(b"y" * 1000))
        assert pool.hits == hits_before + 1

        # Warming 2 larger size classes evicts the oldest class (max_classes=2).
        pool.warm(256 * 1024, count=1)
        pool.warm(512 * 1024, count=1)
        assert pool.unlinked >= 1
    for name in names:
        assert not _shm_exists(name)


@requires_posix_shm
def test_shm_pool_stale_sweep_and_error_cleanup(monkeypatch):
    with shm.ShmPool(ready_per_size=2, stale_after=0.5, max_pending=2, background_refill=False) as pool:
        n1 = pool.write(b"1" * 64)
        n2 = pool.write(b"2" * 64)
        assert _shm_exists(n1) and _shm_exists(n2)

        # Simulate terminal unlinking n1 already, while n2 was leaked: sweep must not error.
        shm._unlink_quiet(n1)
        removed = pool.sweep(now=time.monotonic() + 10.0)
        assert removed == 1
        assert not _shm_exists(n2)
        assert pool.pending_count == 0

        # Empty payload raises ValueError without leaking.
        with pytest.raises(ValueError):
            pool.write(b"")

        # Write failure mid-copy closes and unlinks the segment immediately.
        seg = pool.acquire(64)
        bad_name = seg.name
        monkeypatch.setattr(pool, "acquire", lambda _n: seg)
        monkeypatch.setattr(seg, "write", lambda _d: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError, match="boom"):
            pool.write(b"abc")
        assert not _shm_exists(bad_name)
        assert seg.mapping is None


@requires_posix_shm
def test_shm_pool_atexit_cleanup_in_subprocess():
    code = (
        "from pixcell import shm\n"
        "pool = shm.ShmPool(ready_per_size=2, background_refill=False)\n"
        "pool.warm(4096)\n"
        "name = pool.write(b'hello' * 20)\n"
        "print(name)\n"
        "# Intentionally do NOT call pool.close(): rely on atexit.\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    name = proc.stdout.strip()
    assert name.startswith("/pxc-")
    assert not _shm_exists(name)


# Selection & fallback rules ----------------------------------------------------------


@requires_posix_shm
def test_shm_selection_and_fallbacks(monkeypatch):
    _clean_local_env(monkeypatch)

    # Local Kitty and Ghostty (known kitty.shm = true) -> SHM enabled.
    assert Capabilities("kitty", terminal="kitty", version="0.50.0").shm is True
    assert Capabilities("kitty", terminal="ghostty", version="1.4.0").shm is True
    assert Capabilities("kitty", terminal="ghostty", version="1.3.1").shm is True

    # iTerm2 (both Kitty placeholders and iTerm2 inline) -> direct fallback.
    assert Capabilities("kitty", terminal="iterm2", version="3.7.7").shm is False
    assert Capabilities("kitty", terminal="iterm2", version="3.7.7", shm_probed=True).shm is False
    assert Capabilities("iterm2", terminal="iterm2", version="3.7.7").shm is False

    # Terminals without t=s -> direct fallback.
    assert Capabilities("kitty", terminal="rex").shm is False
    assert Capabilities("kitty", terminal="rex", shm_probed=True).shm is False
    assert Capabilities("kitty", terminal="unknown").shm is False
    assert Capabilities("sixel", terminal="kitty").shm is False
    assert Capabilities("kitty-direct", terminal="kitty").shm is False
    assert Capabilities("halfblock", terminal="kitty").shm is False

    # Live probe overrides: unknown terminal that passed t=s probe -> True;
    # Kitty in an isolated /dev/shm container where probe failed -> False.
    assert Capabilities("kitty", terminal="custom-term", shm_probed=True).shm is True
    assert Capabilities("kitty", terminal="kitty", shm_probed=False).shm is False

    # Over SSH -> direct fallback.
    for ssh_var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        _clean_local_env(monkeypatch)
        monkeypatch.setenv(ssh_var, "1.2.3.4 12345 5.6.7.8 22")
        assert Capabilities("kitty", terminal="kitty", shm_probed=True).shm is False

    # Inside tmux or GNU screen -> direct fallback.
    _clean_local_env(monkeypatch)
    assert Capabilities("kitty", terminal="kitty", tmux=True).shm is False
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert Capabilities("kitty", terminal="kitty").shm is False
    _clean_local_env(monkeypatch)
    monkeypatch.setenv("STY", "1234.pts-0.host")
    assert Capabilities("kitty", terminal="kitty").shm is False
    _clean_local_env(monkeypatch)
    monkeypatch.setenv("TERM", "screen-256color")
    assert Capabilities("kitty", terminal="kitty").shm is False
    monkeypatch.setenv("TERM", "tmux-256color")
    assert Capabilities("kitty", terminal="kitty").shm is False

    # Opt-out via PIXCELL_SHM env var.
    _clean_local_env(monkeypatch)
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv("PIXCELL_SHM", off)
        assert Capabilities("kitty", terminal="kitty", shm_probed=True).shm is False


def test_probe_parses_shm_query_reply(monkeypatch):
    _clean_local_env(monkeypatch)
    # Both direct and SHM queries answered OK.
    answer = b"\x1b_Gi=101;OK\x1b\\\x1b_Gi=202;OK\x1b\\\x1bP>|kitty(0.50.0)\x1b\\\x1b[?62c"
    caps = probe.capabilities_from(answer, 80, 24, None, shm_query_id=202)
    assert caps.protocol == "kitty" and caps.shm_probed is True
    if POSIX_SHM:
        assert caps.shm is True

    # Direct OK, SHM returned ENOENT -> shm_probed is False -> fallback to direct.
    failed = b"\x1b_Gi=101;OK\x1b\\\x1b_Gi=202;ENOENT:no such file\x1b\\\x1bP>|kitty(0.50.0)\x1b\\\x1b[?62c"
    caps_fail = probe.capabilities_from(failed, 80, 24, None, shm_query_id=202)
    assert caps_fail.protocol == "kitty" and caps_fail.shm_probed is False and caps_fail.shm is False


@requires_posix_shm
def test_detect_live_shm_probe_over_pty_cleans_up_segment(monkeypatch):
    _clean_local_env(monkeypatch)
    master, slave = pty.openpty()
    slave_path = os.ttyname(slave)
    seen_shm_name: list[str] = []

    def fake_terminal() -> None:
        buf = b""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if not ready:
                continue
            chunk = os.read(master, 4096)
            if not chunk:
                break
            buf += chunk
            if b"\x1b[c" in buf:
                # Reply OK to direct query, capture SHM query id & name (without unlinking it).
                direct = re.search(rb"\x1b_Gi=(\d+),[^;]*t=d[^;]*;[^\x1b]*\x1b\\", buf)
                shm_q = re.search(rb"\x1b_Gi=(\d+),[^;]*t=s[^;]*;([^\x1b]*)\x1b\\", buf)
                reply = b""
                if direct:
                    reply += b"\x1b_Gi=" + direct.group(1) + b";OK\x1b\\"
                if shm_q:
                    name = base64.standard_b64decode(shm_q.group(2)).decode("utf-8")
                    seen_shm_name.append(name)
                    if _shm_exists(name):
                        reply += b"\x1b_Gi=" + shm_q.group(1) + b";OK\x1b\\"
                reply += b"\x1bP>|kitty(0.50.0)\x1b\\\x1b[6;16;8t\x1b[?62c"
                os.write(master, reply)
                return

    thread = threading.Thread(target=fake_terminal, daemon=True)
    thread.start()
    try:
        caps = probe.detect(timeout=2.0, tty_path=slave_path)
    finally:
        thread.join(timeout=2.0)
        os.close(master)
        os.close(slave)

    assert caps.protocol == "kitty"
    assert caps.terminal == "kitty"
    assert caps.shm_probed is True
    assert caps.shm is True
    assert len(seen_shm_name) == 1
    # Even though fake_terminal did not unlink the probe segment, detect() cleaned it up.
    assert not _shm_exists(seen_shm_name[0])


# LiveImage SHM integration & opt-outs ------------------------------------------------


class _LiveApp(App):
    CSS = "LiveImage { width: 20; height: 5; }"

    def __init__(self, live: LiveImage, written: list[str]) -> None:
        super().__init__()
        self.live = live
        self.written = written

    def compose(self) -> ComposeResult:
        yield self.live

    def on_mount(self) -> None:
        original = self._driver.write
        self._driver.write = lambda data: (self.written.append(data), original(data))[1]


def _sample_frame(x: int) -> PILImage.Image:
    picture = PILImage.new("RGB", (160, 80), (20, 40, 80))
    ImageDraw.Draw(picture).rectangle((x, 20, x + 16, 40), fill=(255, 60, 40))
    return picture


@requires_posix_shm
async def test_live_image_uses_shm_locally_and_cleans_up_on_unmount(monkeypatch):
    _clean_local_env(monkeypatch)
    ct.configure(Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0"))
    live = LiveImage((160, 80), fps=50)
    written: list[str] = []
    shm_names: list[str] = []
    try:
        async with _LiveApp(live, written).run_test() as pilot:
            await pilot.pause(0.15)
            first = "".join(written)
            written.clear()
            assert "a=t,t=s," in first and "t=d" not in first
            live.push(_sample_frame(20))
            await pilot.pause(0.1)
            live.push(_sample_frame(40))
            await pilot.pause(0.1)
            updates = "".join(written)
            assert "a=f,t=s," in updates and "t=d" not in updates
            assert live.shm_frames >= 3
            assert live._shm_pool is not None and not live._shm_pool.closed

            for encoded in re.findall(r"\x1b_G[^;]*t=s[^;]*;([^\x1b]*)\x1b\\", first + updates):
                shm_names.append(base64.standard_b64decode(encoded).decode("utf-8"))
            assert len(shm_names) >= 3
            # Before unmount, segments exist (headless test driver doesn't unlink them).
            assert any(_shm_exists(name) for name in shm_names)

        # After unmount, LiveImage.on_unmount closed the pool and unlinked all segments.
        assert live._shm_pool is None
        for name in shm_names:
            assert not _shm_exists(name)
    finally:
        ct.configure(Capabilities("halfblock"))


@requires_posix_shm
async def test_live_image_shm_opt_outs_and_error_fallback(monkeypatch):
    _clean_local_env(monkeypatch)
    kitty_caps = Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0")
    try:
        # 1. Constructor opt-out: LiveImage(..., shm=False)
        ct.configure(kitty_caps, shm=True)
        live = LiveImage((160, 80), fps=50, shm=False)
        written: list[str] = []
        async with _LiveApp(live, written).run_test() as pilot:
            await pilot.pause(0.15)
            assert "a=t,t=d," in "".join(written) and "t=s" not in "".join(written)
            assert live.shm_frames == 0

        # 2. Config opt-out: configure(..., shm=False)
        ct.configure(kitty_caps, shm=False)
        live2 = LiveImage((160, 80), fps=50)
        written2: list[str] = []
        async with _LiveApp(live2, written2).run_test() as pilot:
            await pilot.pause(0.15)
            assert "a=t,t=d," in "".join(written2) and "t=s" not in "".join(written2)

        # 3. Env var opt-out: PIXCELL_SHM=0
        monkeypatch.setenv("PIXCELL_SHM", "0")
        ct.configure(kitty_caps, shm=True)
        live3 = LiveImage((160, 80), fps=50)
        written3: list[str] = []
        async with _LiveApp(live3, written3).run_test() as pilot:
            await pilot.pause(0.15)
            assert "a=t,t=d," in "".join(written3) and "t=s" not in "".join(written3)

        # 4. Runtime SHM OSError falls back cleanly to direct path.
        monkeypatch.delenv("PIXCELL_SHM", raising=False)
        ct.configure(kitty_caps, shm=True)
        monkeypatch.setattr(shm, "ShmPool", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "no space")))
        live4 = LiveImage((160, 80), fps=50)
        written4: list[str] = []
        async with _LiveApp(live4, written4).run_test() as pilot:
            await pilot.pause(0.15)
            assert "a=t,t=d," in "".join(written4)
            assert live4._shm_failed is True
    finally:
        ct.configure(Capabilities("halfblock"))


# TIOCOUTQ output-queue gating --------------------------------------------------------


def test_ttyqueue_ioctl_and_noops(monkeypatch):
    _clean_local_env(monkeypatch)

    # Mock TIOCOUTQ returning exact byte counts.
    depth = [0]

    def fake_ioctl(fd: int, req: int, buf: bytes) -> bytes:
        assert fd == 7
        return struct.pack("i", depth[0])

    assert ttyqueue.queued_bytes(7, ioctl_fn=fake_ioctl) == 0
    depth[0] = 8192
    assert ttyqueue.queued_bytes(7, ioctl_fn=fake_ioctl) == 8192

    # Errors / invalid fds / missing TIOCOUTQ -> clean None (no-op).
    def fail_ioctl(_fd: int, _req: int, _buf: bytes) -> bytes:
        raise OSError(errno.ENOTTY, "not a tty")

    assert ttyqueue.queued_bytes(7, ioctl_fn=fail_ioctl) is None
    assert ttyqueue.queued_bytes(None, ioctl_fn=fake_ioctl) is None
    assert ttyqueue.queued_bytes(-1, ioctl_fn=fake_ioctl) is None

    monkeypatch.setattr(ttyqueue, "TIOCOUTQ", None)
    assert ttyqueue.queued_bytes(7) is None

    # OutputGate threshold & opt-outs.
    gate = ttyqueue.OutputGate(threshold=4096, ioctl_fn=fake_ioctl)
    depth[0] = 4096
    assert gate.is_backed_up(fd=7) is False  # at threshold, not past it
    depth[0] = 4097
    assert gate.is_backed_up(fd=7) is True  # past threshold

    monkeypatch.setenv("PIXCELL_OUTPUT_GATING", "0")
    assert gate.is_backed_up(fd=7) is False

    monkeypatch.delenv("PIXCELL_OUTPUT_GATING", raising=False)
    monkeypatch.setenv("PIXCELL_OUTPUT_QUEUE_LIMIT", "10000")
    env_gate = ttyqueue.OutputGate(ioctl_fn=fake_ioctl)
    depth[0] = 9000
    assert env_gate.is_backed_up(fd=7) is False
    depth[0] = 10001
    assert env_gate.is_backed_up(fd=7) is True


async def test_live_image_output_gating_holds_latest_frame_and_respects_opt_out(monkeypatch):
    _clean_local_env(monkeypatch)
    queued = [0]

    class _FakeFile:
        def fileno(self) -> int:
            return 42

    def fake_ioctl(fd: int, _req: int, _buf: bytes) -> bytes:
        if fd == 42:
            return struct.pack("i", queued[0])
        raise OSError(errno.ENOTTY, "not a tty")

    monkeypatch.setattr(ttyqueue.fcntl, "ioctl", fake_ioctl)
    ct.configure(Capabilities("kitty", (8, 16), terminal="rex"), output_gating=True)
    try:
        live = LiveImage((160, 80), fps=50, output_queue_limit=4096)
        written: list[str] = []
        async with _LiveApp(live, written).run_test() as pilot:
            pilot.app._driver._file = _FakeFile()
            await pilot.pause(0.15)
            written.clear()
            initial_shown = live.frames_shown

            # Back up the tty output queue past the threshold.
            queued[0] = 16384
            frame_a = PILImage.new("RGB", (160, 80), (10, 20, 30))
            frame_b = PILImage.new("RGB", (160, 80), (200, 10, 10))
            live.push(frame_a)
            await pilot.pause(0.08)
            assert live.frames_gated >= 1
            assert live.frames_shown == initial_shown
            assert written == []

            # Push a newer frame while still backed up -> latest frame wins.
            live.push(frame_b)
            await pilot.pause(0.08)
            assert live.frames_shown == initial_shown

            # Drain the queue -> only frame_b is shown (1 frame, not 2).
            queued[0] = 0
            await pilot.pause(0.1)
            assert live.frames_shown == initial_shown + 1
            assert live.image is frame_b
            assert "".join(written).count("a=f,") == 1

        # Opt-out via constructor flag: writes immediately even while backed up.
        queued[0] = 32768
        live_off = LiveImage((160, 80), fps=50, output_gating=False, output_queue_limit=4096)
        written_off: list[str] = []
        async with _LiveApp(live_off, written_off).run_test() as pilot:
            pilot.app._driver._file = _FakeFile()
            await pilot.pause(0.15)
            written_off.clear()
            live_off.push(PILImage.new("RGB", (160, 80), (50, 150, 50)))
            await pilot.pause(0.1)
            assert live_off.frames_gated == 0
            assert live_off.frames_shown >= 1
            assert "a=f," in "".join(written_off)

        # Opt-out via configure(output_gating=False).
        ct.configure(Capabilities("kitty", (8, 16), terminal="rex"), output_gating=False)
        live_cfg_off = LiveImage((160, 80), fps=50, output_queue_limit=4096)
        written_cfg: list[str] = []
        async with _LiveApp(live_cfg_off, written_cfg).run_test() as pilot:
            pilot.app._driver._file = _FakeFile()
            await pilot.pause(0.15)
            written_cfg.clear()
            live_cfg_off.push(PILImage.new("RGB", (160, 80), (50, 50, 200)))
            await pilot.pause(0.1)
            assert live_cfg_off.frames_gated == 0
            assert "a=f," in "".join(written_cfg)

        # Opt-out via PIXCELL_OUTPUT_GATING=0.
        ct.configure(Capabilities("kitty", (8, 16), terminal="rex"), output_gating=True)
        monkeypatch.setenv("PIXCELL_OUTPUT_GATING", "0")
        live_env_off = LiveImage((160, 80), fps=50, output_queue_limit=4096)
        written_env: list[str] = []
        async with _LiveApp(live_env_off, written_env).run_test() as pilot:
            pilot.app._driver._file = _FakeFile()
            await pilot.pause(0.15)
            written_env.clear()
            live_env_off.push(PILImage.new("RGB", (160, 80), (150, 50, 200)))
            await pilot.pause(0.1)
            assert live_env_off.frames_gated == 0
            assert "a=f," in "".join(written_env)
    finally:
        ct.configure(Capabilities("halfblock"))
