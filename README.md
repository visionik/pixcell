# pixcell

Images in terminal cells, MIT-licensed. (Formerly *cellpix*; renamed in 0.3.0, with its
`CELLPIX_*` environment variables now `PIXCELL_*`.)

- **Kitty graphics** with Unicode placeholders, for Kitty, Ghostty, iTerm2 (3.7+) and Rex. An image is
  sent to the terminal once, then drawn as ordinary coloured text, so it scrolls, clips
  and redraws with everything around it.
- **iTerm2 inline images, Sixel, and Kitty placements** for terminals without
  placeholders (older iTerm2, WezTerm, foot, xterm, Konsole...): painted over the image's cells
  after each frame, cropped to what's visible.
- **Half blocks** (`▀`, two pixels per cell) for any terminal with 24-bit colour.
- **Animated pictures** (GIF, WebP, APNG): played by the terminal where it can (Kitty,
  Ghostty 1.4+, Rex, iTerm2), otherwise stepped through by pixcell, a frame at a time
  (other placeholder terminals, Sixel, half blocks). In WezTerm, which shows iTerm2 GIFs
  still, moving pictures go to Sixel. `pixcell.textual.animations_play()` says whether
  they move here; `PIXCELL_ANIMATION=0` keeps them still.
- **Live pictures** (`pixcell.live.LiveImage`): push frames from any thread (a browser
  view, a camera, a game); only what changed is sent where the terminal allows, and
  clicks come back in the frame's own pixels.
- A **probe** that asks the terminal which it supports, and its cell size in pixels.
- A **Textual** widget (`pip install pixcell[textual]`).

```python
import pixcell.textual
from PIL import Image

pixcell.textual.configure()   # probe the terminal, before the app starts

class Viewer(App):
    def compose(self):
        yield pixcell.textual.Image(Image.open("photo.png"))

Viewer().run()
```

A live picture:

```python
from pixcell.live import LiveImage

live = LiveImage((640, 400), fps=30)   # in compose(), like any widget
live.push(frame)                       # a Pillow image, from any thread: the newest wins
```

`PIXCELL_PROTOCOL=kitty|iterm2|kitty-direct|sixel|halfblock|off|host` overrides the probe.
`host` is never detected, only forced: something hosting the app, such as a browser, draws the
pictures itself. The widgets then write nothing, and the host reads `Image.image`,
`Image.animated`, `Image.alt` and `Image.live` from any widget whose `protocol` is `"host"`.
`python -m pixcell [image ...]` shows pictures; `python -m pixcell live [fps] [scroll]`
runs a live picture with its frame rate and cost.

## Documents

`pixcell.docs` opens files into displayable outputs (`Picture` with lazy pages, `SVG`, or `HTML`) by file suffix or magic-byte sniffing, keeping heavy renderers behind optional extras:

```python
import pixcell.docs

doc = pixcell.docs.open("diagram.svg")       # SVG(svg=..., width=..., height=...)
picture = doc.to_picture(background="#fff")  # Picture with lazy pages (needs pixcell[svg])
first_page = picture.pages[0]                # PIL.Image.Image
pdf = pixcell.docs.open("report.pdf")        # Picture with lazy pages (needs pixcell[pdf])
book = pixcell.docs.open("novel.epub")       # HTML(html=..., title=...) (built-in)
word = pixcell.docs.open("memo.docx")        # HTML(html=..., title=...) (needs pixcell[docx])
sheet = pixcell.docs.open("budget.xlsx")     # HTML(html=..., title=...) (needs pixcell[xlsx])
```

Raster images (`png`, `jpg`, `gif`, `webp`, `bmp`, `tiff`, `ico`, `qoi`, ...) work out of the box via Pillow. Guard rails default to `max_bytes=50 MiB` and `max_pages=200`.

| Extra | Formats | Status |
| --- | --- | --- |
| *(built-in)* | PNG, JPEG, GIF, WebP, BMP, TIFF, ICO, QOI | Live |
| `pixcell[svg]` | SVG (rasterised via `resvg-py`) | Live |
| `pixcell[pdf]` | PDF (`pypdfium2`) | Live |
| *(built-in)* | EPUB (stdlib `zipfile`, XHTML out) | Live |
| `pixcell[docx]` | Word documents (`mammoth`) | Live |
| `pixcell[xlsx]` | Spreadsheets (`openpyxl`) | Live |
| `pixcell[mermaid]` | Mermaid diagrams (Pele via `quickjs-ng`, rasterised via `resvg-py`) | Live |
| `pixcell[pptx]` | PowerPoint decks (`quickjs-ng` + bundled `pptx-glimpse` + `resvg-py`) | Live |
| `pixcell[svg]` | Excalidraw drawings (own JSON-to-SVG converter) | Live |
| `pixcell[all]` | All available extras | Live |

`pixcell[pptx]` renders slides via bundled `pptx-glimpse` in no-fonts mode (patched for autoshape `fontRef` theme colours/tints, proportional/`buSzPct`/`buSzPts` bullets, built-in OOXML table styles, pie/doughnut data labels, and EMF/WMF alternate-image fallbacks). Remaining fidelity limits: embedded fonts are not loaded (slides use built-in font metrics with system fallback fonts), complex EMF+/GDI+ metafiles without an embedded PNG/SVG fallback render as an in-slide `[EMF]`/`[WMF]` box, and 3D effects/animations/SmartArt without cached drawings are simplified or omitted.

Embedding apps that vendor `pixcell` can override the install command shown in `MissingExtra` errors (default `pip install "pixcell[<extra>]"`) with `pixcell.docs.set_install_hint(...)` (accepting a `(extra) -> str` callable, a `"{extra}"` format string, or `None` to reset).

## Browser

`pixcell.browser` (ported from `deftai/console`) drives a headless Chromium-family browser over CDP (`--remote-debugging-pipe`, stdlib-only) for screenshots, JavaScript evaluation, offline `pixcell.docs.HTML` rendering, and an interactive Textual `BrowserView` widget (`pip install pixcell[textual]`). If no Chromium browser is installed, `pixcell.browser.install()` downloads Google's Chrome headless shell into pixcell's user cache directory (`PIXCELL_BROWSER_CACHE_DIR` or `set_cache_dir()`).

Headless use (no Textual needed):

```python
from pixcell.browser import Browser, Policy

with Browser(policy=Policy(allow_hosts=frozenset({"example.com"}))) as browser:
    browser.navigate("https://example.com")
    title = browser.eval("document.title")
    image = browser.screenshot()          # PIL.Image.Image
    png = browser.screenshot(format="png")  # PNG bytes
```

Opening a `pixcell.docs` `HTML` document under the no-network policy (`file://` and `data:` only):

```python
import pixcell.browser
import pixcell.docs

doc = pixcell.docs.open("novel.epub")     # or memo.docx, budget.xlsx
with pixcell.browser.open_html(doc) as browser:
    page = browser.screenshot()           # PIL.Image.Image, rendered offline
```

Live interactive browser in Textual (`pip install pixcell[textual]`):

```python
import pixcell.textual
from textual.app import App
from pixcell.browser import BrowserView

pixcell.textual.configure()   # probe the terminal before the app starts

class BrowserApp(App):
    def compose(self):
        yield BrowserView("https://example.com")

BrowserApp().run()
```

## Notes

- What each terminal really does comes from [terminal-facts](https://github.com/visionik/terminal-facts),
  vendored as `src/pixcell/terminals.toml` by `tools/update_facts.py`. It's shared with sdl-kitty, and
  each fact has its evidence.
- **Local SHM (`t=s`) for `LiveImage`**: on local POSIX terminals known or probed to support Kitty
  shared-memory transmission, `LiveImage` writes uncompressed RGB/RGBA pixels into a pre-faulted,
  reused POSIX shared-memory pool (`pixcell.shm.ShmPool`) and sends a short `t=s` command instead
  of zlib-compressing and base64-encoding pixel data. Detection runs a one-shot `a=q,t=s` query in
  the same startup round trip as `a=q,t=d` (only when local) and combines it with `kitty.shm` from
  `terminals.toml`. Any remote (`SSH_CONNECTION`/`SSH_CLIENT`/`SSH_TTY`), multiplexed (`TMUX`/`STY`
  or `TERM=screen*`/`tmux*`), non-`t=s`, non-POSIX, or iTerm2 session falls back to the direct
  zlib/PNG (`t=d`) path. Opt out with `PIXCELL_SHM=0`, `pixcell.textual.configure(shm=False)`, or
  `LiveImage(..., shm=False)`.

  | Terminal | `kitty.shm` (`terminals.toml`) | `LiveImage` local SHM (`t=s`) | Notes |
  | --- | --- | --- | --- |
  | Kitty | `true` | Yes (local only) | First-class `t=s`; falls back to `t=d` over SSH/tmux/isolated `/dev/shm` |
  | Ghostty (all versions) | `true` | Yes (local only) | `t=s` for both `a=f` edits (1.4+) and double-buffered swaps (<=1.3.1) |
  | iTerm2 (3.7+) | `true` | No (direct PNG) | `kitty.fastest_format = "png"`; falls back to direct PNG (`f=100`) |
  | Rex | `false` | No (direct zlib) | `a=q,t=s` is silent/failing; uses direct `t=d` (`o=z`) |
  | WezTerm | `false` (macOS) / unknown | No (overlay / direct) | No Kitty placeholders; `t=s` fails with `ENXIO` on macOS (wezterm#8063) |
  | Unknown Kitty terminal | unknown | Only if live `a=q,t=s` probe returns `OK` | Fails closed to `t=d` when not probed or probe fails |

- **Output-queue gating for `LiveImage`**: before writing a live frame, `pixcell.ttyqueue.OutputGate`
  checks the tty output queue depth via `TIOCOUTQ` (`fcntl.ioctl`) where available. If queued bytes
  exceed the threshold (default 4096 bytes; override with `PIXCELL_OUTPUT_QUEUE_LIMIT` or
  `LiveImage(..., output_queue_limit=...)`), the frame is held for the next tick (latest frame wins)
  so frames do not pile up in a congested tty. Clean no-op where `TIOCOUTQ` is unavailable (Windows,
  non-tty fds, headless tests). Opt out with `PIXCELL_OUTPUT_GATING=0`,
  `pixcell.textual.configure(output_gating=False)`, or `LiveImage(..., output_gating=False)`.
- Both the pre-faulted SHM pool and `TIOCOUTQ` output-queue gating techniques were inspired by
  [zenbu-labs/terminal-browser](https://github.com/zenbu-labs/terminal-browser) (`pixel`, MIT) and
  re-implemented for pixcell.
- The Kitty side is written from the protocol's documentation
  (<https://sw.kovidgoyal.net/kitty/graphics-protocol/>). The placeholder row/column marks
  are generated from the Unicode Character Database by `tools/gen_diacritics.py`.
- Image ids are carried in 24-bit foreground colours, so the app must draw in true colour.
- Pictures are sent scaled to the size they're shown at. A memory budget (8 MB in Rex,
  whose library keeps at most 10 MB) deletes pictures not drawn lately, and they're sent
  again if they come back into view.
- Inside tmux (with `allow-passthrough on`), Kitty placeholders only; elsewhere half blocks.
- On Windows the probe asks through the console API; terminals that answer with Sixel (Windows Terminal 1.22+) get Sixel.
