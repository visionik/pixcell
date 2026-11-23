"""Textual key events as Chrome DevTools key presses (``Input.dispatchKeyEvent``).

Ported from ``deftai/console`` (MIT).
"""

from __future__ import annotations

from dataclasses import dataclass

# Textual's name -> (DOM key, DOM code, Windows virtual key code, text it types)
SPECIAL = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "tab": ("Tab", "Tab", 9, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "up": ("ArrowUp", "ArrowUp", 38, ""),
    "down": ("ArrowDown", "ArrowDown", 40, ""),
    "left": ("ArrowLeft", "ArrowLeft", 37, ""),
    "right": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
    "space": (" ", "Space", 32, " "),
}
MODIFIERS = {"alt": 1, "ctrl": 2, "meta": 4, "super": 4, "shift": 8}


@dataclass(frozen=True)
class Press:
    key: str
    text: str = ""
    code: str = ""
    key_code: int = 0
    modifiers: int = 0


def press_for(key: str, character: str | None) -> Press | None:
    """The key press for a Textual key (``"enter"``, ``"shift+tab"``, ``"ctrl+a"``, ``"a"``)
    and its character, or None for keys that don't go to the page."""
    *mods, name = key.split("+") if key != "+" else ["+"]
    modifiers = sum(MODIFIERS.get(mod, 0) for mod in mods)
    if name in SPECIAL:
        dom_key, code, key_code, text = SPECIAL[name]
        return Press(dom_key, text if not (modifiers & 2 or modifiers & 4) else "", code, key_code, modifiers)
    if character and character.isprintable() and not (modifiers & (2 | 4)):
        # Typed text (shift is already in the character).
        return Press(character, character, modifiers=modifiers & ~8)
    if len(name) == 1 and modifiers:  # ctrl+a, cmd+c...
        letter = name.lower()
        code = f"Key{letter.upper()}" if letter.isalpha() else ""
        return Press(letter, code=code, key_code=ord(letter.upper()), modifiers=modifiers)
    return None
