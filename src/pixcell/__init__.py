"""pixcell: images in terminal cells.

- :mod:`pixcell.kitty` — Kitty graphics protocol escape codes and Unicode placeholders.
- :mod:`pixcell.iterm2`, :mod:`pixcell.sixel` — iTerm2 inline images and Sixel.
- :mod:`pixcell.halfblock` — two pixels per cell for any 24-bit-colour terminal.
- :mod:`pixcell.frames` — the frames of animated pictures (GIF, WebP, APNG).
- :mod:`pixcell.probe` — find out which the terminal supports, and its cell size.
- :mod:`pixcell.sheet` — several pictures on one cell grid, in the terminal's best protocol.
- :mod:`pixcell.textual` — an ``Image`` widget for Textual (``pip install pixcell[textual]``).
- :mod:`pixcell.live` — ``LiveImage``, a Textual widget you keep pushing frames into.
- :mod:`pixcell.docs` — open images, SVGs, and documents into displayable outputs.
- :mod:`pixcell.browser` — headless Chromium over CDP and a live Textual ``BrowserView`` widget.
"""

__version__ = "0.7.0"
