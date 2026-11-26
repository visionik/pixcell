"""Headless benchmark: direct (zlib/PNG) vs SHM (t=s) and output-queue gating on vs off.

Run with:
    .venv/bin/python bench/shm_gating_bench.py

Methodology:
1. Direct vs SHM over a real POSIX PTY (pty.openpty()):
   - Pre-renders 120 distinct 640x400 RGB frames from a scrolling synthetic page
     (matching pixcell's ``live scroll`` workload).
   - A reader thread drains the PTY master continuously; in SHM mode it also
     opens, reads, and unlinks each POSIX SHM object (simulating a local
     Kitty/Ghostty terminal).
   - Measures wall frames/s, producer-thread CPU ms/frame (thread_time), and
     wire KB/frame written to the PTY for:
       a) Direct t=d raw+zlib (level 1, ``kitty.transmit_pixels``)
       b) Direct t=d PNG (level 1, ``kitty.transmit``)
       c) Local SHM t=s (``ShmPool`` + ``kitty.transmit_shm``)

2. Output-queue gating ON vs OFF over a throttled stream using real kernel TIOCOUTQ:
   - Pushes 640x400 zlib frames at 60 Hz through a Textual-style async writer
     thread into an ``AF_UNIX`` stream socket whose reader drains at 256 KB/s
     (simulating a congested terminal or SSH link).
   - Note: Linux's PTY driver always returns 0 for ``TIOCOUTQ`` on PTY slave
     fds (unlike macOS/BSD PTYs), whereas Linux ``AF_UNIX``/TCP sockets report
     real kernel output-queue bytes via the exact same ``termios.TIOCOUTQ``
     ioctl. That lets the benchmark exercise ``ttyqueue.OutputGate`` unmodified
     against the real kernel ioctl.
   - Compares frames sent, frames held (latest wins), producer CPU time, wire
     volume, and post-burst drain lag with gating OFF vs ON.
"""

from __future__ import annotations

import base64
import io
import os
import pty
import queue
import re
import select
import socket
import sys
import threading
import time
import tty

from PIL import Image, ImageDraw

from pixcell import kitty, shm, ttyqueue

SIZE = (640, 400)
FRAMES = 120
_SHM_RE = re.compile(rb"\x1b_G[^;]*t=s[^;]*S=(\d+)[^;]*;([^\x1b]*)\x1b\\")


def _make_frames(count: int, size: tuple[int, int] = SIZE) -> list[Image.Image]:
    """Generate distinct detailed RGB frames (scrolling page)."""
    w, h = size
    # Reuse pixcell's synthetic scrolling page generator for realistic compressibility.
    from pixcell.__main__ import _tall_page

    page = _tall_page(w)
    out: list[Image.Image] = []
    max_top = max(1, page.height - h)
    for i in range(count):
        top = (i * 12) % max_top
        frame = page.crop((0, top, w, top + h))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((10 + (i * 5) % (w - 60), h - 24, 50 + (i * 5) % (w - 60), h - 8), fill=(220, 60, 40))
        out.append(frame)
    return out


def _png1(picture: Image.Image) -> bytes:
    buf = io.BytesIO()
    picture.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _bench_transport(frames: list[Image.Image], mode: str) -> dict[str, float | int | str]:
    master, slave = pty.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    stop = threading.Event()
    shm_consumed = [0]

    def drain_pty() -> None:
        import _posixshmem

        buf = b""
        while not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.02)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            if mode == "shm":
                buf += chunk
                last_end = 0
                for match in _SHM_RE.finditer(buf):
                    last_end = match.end()
                    size = int(match.group(1))
                    name = base64.standard_b64decode(match.group(2)).decode("utf-8")
                    try:
                        sfd = _posixshmem.shm_open(name, os.O_RDONLY, 0)
                        try:
                            _ = os.pread(sfd, size, 0)
                        finally:
                            os.close(sfd)
                        _posixshmem.shm_unlink(name)
                        shm_consumed[0] += 1
                    except FileNotFoundError:
                        pass
                if last_end:
                    buf = buf[last_end:]

    reader = threading.Thread(target=drain_pty, daemon=True)
    reader.start()

    pool: shm.ShmPool | None = None
    if mode == "shm":
        pool = shm.ShmPool(ready_per_size=8, background_refill=True)
        pool.warm(SIZE[0] * SIZE[1] * 3)

    image_id = 42
    wire_bytes = 0
    wall_start = time.perf_counter()
    cpu_start = time.thread_time()
    try:
        for frame in frames:
            if mode == "direct-zlib":
                code = kitty.transmit_pixels(image_id, frame)
            elif mode == "direct-png":
                code = kitty.transmit(image_id, _png1(frame))
            elif mode == "shm":
                assert pool is not None
                keys, data = kitty.raw_pixels(frame)
                name = pool.write(data)
                code = kitty.transmit_shm(
                    image_id, name, int(keys["s"]), int(keys["v"]), len(data), fmt=int(keys["f"])
                )
            else:
                raise ValueError(mode)
            encoded = code.encode("ascii")
            wire_bytes += len(encoded)
            _write_all(slave, encoded)
        cpu_elapsed = time.thread_time() - cpu_start
        wall_elapsed = time.perf_counter() - wall_start
    finally:
        # Let reader finish draining before closing PTY and pool.
        time.sleep(0.05)
        stop.set()
        reader.join(timeout=2.0)
        os.close(slave)
        os.close(master)
        hit_rate = 0.0
        if pool is not None:
            total = pool.hits + pool.misses
            hit_rate = (pool.hits / total * 100.0) if total else 0.0
            pool.close()

    n = len(frames)
    return {
        "mode": mode,
        "frames": n,
        "wall_s": wall_elapsed,
        "fps": n / wall_elapsed,
        "cpu_ms_per_frame": (cpu_elapsed / n) * 1000.0,
        "wire_kb_per_frame": (wire_bytes / n) / 1024.0,
        "pool_hit_pct": hit_rate,
        "shm_consumed": shm_consumed[0],
    }


def _bench_gating(
    frames: list[Image.Image],
    *,
    gating: bool,
    duration_s: float = 1.5,
    target_fps: float = 60.0,
    drain_bytes_per_s: float = 256 * 1024,
    threshold: int = 4096,
) -> dict[str, float | int | bool]:
    """Benchmark unmodified OutputGate (real kernel TIOCOUTQ) under a throttled reader."""
    writer_sock, reader_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    write_fd = writer_sock.fileno()
    stop_reader = threading.Event()
    write_queue: queue.Queue[bytes | None] = queue.Queue()
    bytes_drained = [0]

    def writer_thread() -> None:
        while True:
            chunk = write_queue.get()
            if chunk is None:
                return
            try:
                writer_sock.sendall(chunk)
            except OSError:
                return

    def slow_reader() -> None:
        chunk_size = 4096
        sleep_s = chunk_size / drain_bytes_per_s
        while not stop_reader.is_set():
            ready, _, _ = select.select([reader_sock], [], [], 0.02)
            if not ready:
                continue
            try:
                data = reader_sock.recv(chunk_size)
            except OSError:
                break
            if not data:
                break
            bytes_drained[0] += len(data)
            time.sleep(sleep_s)

    wt = threading.Thread(target=writer_thread, daemon=True)
    rt = threading.Thread(target=slow_reader, daemon=True)
    wt.start()
    rt.start()

    # Uses real kernel fcntl.ioctl(fd, termios.TIOCOUTQ, ...) with no mocks.
    gate = ttyqueue.OutputGate(enabled=gating, threshold=threshold)

    sent = 0
    gated = 0
    wire_bytes = 0
    pending_idx: int | None = None
    interval = 1.0 / target_fps
    start = time.perf_counter()
    next_tick = start
    cpu_start = time.thread_time()
    ticks = 0

    try:
        while time.perf_counter() - start < duration_s:
            # Producer pushes a new frame every tick (newest wins).
            pending_idx = ticks % len(frames)
            ticks += 1
            if gate.is_backed_up(fd=write_fd):
                gated += 1
            elif pending_idx is not None:
                code = kitty.transmit_pixels(42, frames[pending_idx]).encode("ascii")
                wire_bytes += len(code)
                write_queue.put(code)
                sent += 1
                pending_idx = None
            next_tick += interval
            remaining = next_tick - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        cpu_elapsed = time.thread_time() - cpu_start
        wall_elapsed = time.perf_counter() - start
        queued_at_end = ttyqueue.queued_bytes(write_fd) or 0
        backlog_bytes = max(0, wire_bytes - bytes_drained[0])
    finally:
        # Stop cleanly without waiting for seconds of ungated backlog to drain.
        stop_reader.set()
        try:
            reader_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        write_queue.put(None)
        writer_sock.close()
        reader_sock.close()
        wt.join(timeout=2.0)
        rt.join(timeout=2.0)

    return {
        "gating": gating,
        "ticks": ticks,
        "sent": sent,
        "gated": gated,
        "wall_s": wall_elapsed,
        "effective_fps": sent / wall_elapsed,
        "cpu_ms_total": cpu_elapsed * 1000.0,
        "cpu_pct": (cpu_elapsed / wall_elapsed) * 100.0,
        "wire_mb": wire_bytes / (1024 * 1024),
        "backlog_kb": backlog_bytes / 1024.0,
        "kernel_tiocoutq_kb": queued_at_end / 1024.0,
    }


def main() -> None:
    if not shm.available():
        sys.exit("POSIX shared memory is not available on this platform.")
    frames = _make_frames(FRAMES)
    print(f"Benchmark: {FRAMES} frames x {SIZE[0]}x{SIZE[1]} RGB (scrolling page) over headless PTY")
    print()
    print("1) Direct vs SHM (t=s) transport (real POSIX PTY, unthrottled reader, median of 3 runs):")
    print(f"{'Mode':<14} {'FPS':>8} {'CPU ms/f':>10} {'Wire KB/f':>11} {'Pool hit':>10}")
    for mode in ("direct-zlib", "direct-png", "shm"):
        runs = [_bench_transport(frames, mode) for _ in range(3)]
        r = sorted(runs, key=lambda item: float(item["fps"]))[1]
        hit = f"{r['pool_hit_pct']:.1f}%" if mode == "shm" else "-"
        print(
            f"{r['mode']:<14} {r['fps']:>8.1f} {r['cpu_ms_per_frame']:>10.2f} "
            f"{r['wire_kb_per_frame']:>11.1f} {hit:>10}"
        )

    print()
    print("2) Output-queue gating ON vs OFF (60 Hz producer, 256 KB/s throttled reader, real kernel TIOCOUTQ, 4 KiB limit):")
    print(
        f"{'Gating':<8} {'Ticks':>6} {'Sent':>6} {'Gated':>6} {'Eff FPS':>8} "
        f"{'CPU ms':>8} {'CPU %':>7} {'Wire MB':>8} {'Backlog KB':>11}"
    )
    for gating in (False, True):
        r = _bench_gating(frames, gating=gating)
        label = "ON" if gating else "OFF"
        print(
            f"{label:<8} {r['ticks']:>6} {r['sent']:>6} {r['gated']:>6} {r['effective_fps']:>8.1f} "
            f"{r['cpu_ms_total']:>8.1f} {r['cpu_pct']:>6.1f}% {r['wire_mb']:>8.2f} {r['backlog_kb']:>11.1f}"
        )


if __name__ == "__main__":
    main()
