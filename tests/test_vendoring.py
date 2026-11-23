"""Tests that pixcell (and pixcell.docs lazy renderer loaders) survive vendoring."""

from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell
import pixcell.docs as docs
from pixcell.docs import HTML, MissingExtra, Registry

HAS_RESVG = importlib.util.find_spec("resvg_py") is not None
HAS_PYPDFIUM2 = importlib.util.find_spec("pypdfium2") is not None


def _rewrite_vendored_imports(text: str, prefix: str, name: str = "pixcell") -> str:
    """Rewrite import statements the same way deftai/console's scripts/vendor.py does."""
    text = re.sub(rf"^(\s*)from ({name})(\.|\s)", rf"\1from {prefix}.\2\3", text, flags=re.M)

    def aliased(match: re.Match[str]) -> str:
        indent, dotted, alias = match.groups()
        parent, _, last = dotted.rpartition(".")
        if parent:
            return f"{indent}from {prefix}.{parent} import {last} as {alias}"
        return f"{indent}from {prefix} import {last} as {alias}"

    text = re.sub(rf"^(\s*)import ((?:{name})(?:\.\w+)*) as (\w+)", aliased, text, flags=re.M)
    return re.sub(rf"^(\s*)import ({name})\b", rf"\1import {prefix}.\2", text, flags=re.M)


def _vendor_pixcell(dest_root: Path, prefix: str = "vendored_x") -> Path:
    """Copy src/pixcell into ``dest_root/<prefix>/pixcell`` with rewritten imports."""
    src_pkg = Path(pixcell.__file__).resolve().parent
    prefix_dir = dest_root
    for part in prefix.split("."):
        prefix_dir = prefix_dir / part
        prefix_dir.mkdir(parents=True, exist_ok=True)
        init = prefix_dir / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")

    target = prefix_dir / "pixcell"
    shutil.copytree(src_pkg, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in target.rglob("*.py"):
        original = path.read_text(encoding="utf-8")
        rewritten = _rewrite_vendored_imports(original, prefix)
        if rewritten != original:
            path.write_text(rewritten, encoding="utf-8")
    return target


def _write_sample_files(directory: Path) -> tuple[Path, Path, Path]:
    png_path = directory / "sample.png"
    PILImage.new("RGB", (8, 6), (10, 120, 220)).save(png_path, "PNG")

    svg_path = directory / "sample.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="16">'
        '<rect width="24" height="16" fill="#336699"/></svg>',
        encoding="utf-8",
    )

    pdf_path = directory / "sample.pdf"
    PILImage.new("RGB", (60, 40), (200, 80, 40)).save(pdf_path, "PDF", resolution=72.0)

    return png_path, svg_path, pdf_path


def test_builtin_loaders_are_relative_and_third_party_absolute_loaders_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin = docs.renderers()
    assert builtin
    for spec in builtin:
        assert isinstance(spec.loader, str)
        assert spec.loader.startswith("."), f"{spec.name!r} loader should be relative: {spec.loader!r}"

    # Absolute "pkg.mod:fn" loader strings must keep working for third-party renderers.
    plugin_pkg = tmp_path / "third_party_plugin"
    plugin_pkg.mkdir()
    (plugin_pkg / "__init__.py").write_text("", encoding="utf-8")
    (plugin_pkg / "custom.py").write_text(
        "from pathlib import Path\n"
        "from pixcell.docs import HTML\n"
        "def render(path: Path) -> HTML:\n"
        "    return HTML(html=f'<p>{path.read_text()}</p>', source=path)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        reg = Registry()
        reg.register(
            "custom",
            loader="third_party_plugin.custom:render",
            suffixes=(".custom",),
        )
        sample = tmp_path / "hello.custom"
        sample.write_text("hi there", encoding="utf-8")
        doc = reg.open(sample)
        assert isinstance(doc, HTML)
        assert doc.html == "<p>hi there</p>"
    finally:
        for mod_name in list(sys.modules):
            if mod_name == "third_party_plugin" or mod_name.startswith("third_party_plugin."):
                sys.modules.pop(mod_name, None)


def test_vendored_pixcell_opens_image_svg_and_pdf_from_vendored_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "vendored_x"
    _vendor_pixcell(tmp_path, prefix=prefix)
    png_path, svg_path, pdf_path = _write_sample_files(tmp_path)

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        vdocs = importlib.import_module(f"{prefix}.pixcell.docs")

        # Built-in image renderer
        img_doc = vdocs.open(png_path)
        assert isinstance(img_doc, vdocs.Picture)
        assert img_doc.image.size == (8, 6)
        img_doc.close()
        img_loader = vdocs.resolve(png_path).resolve_loader()
        assert img_loader.__module__ == f"{prefix}.pixcell.docs.image"
        assert f"{prefix}.pixcell.docs.image" in sys.modules

        # Built-in SVG renderer
        svg_doc = vdocs.open(svg_path)
        assert isinstance(svg_doc, vdocs.SVG)
        assert svg_doc.width == 24.0 and svg_doc.height == 16.0
        svg_loader = vdocs.resolve(svg_path).resolve_loader()
        assert svg_loader.__module__ == f"{prefix}.pixcell.docs.svg"
        assert f"{prefix}.pixcell.docs.svg" in sys.modules
        if HAS_RESVG:
            raster = svg_doc.to_picture()
            assert isinstance(raster, vdocs.Picture)
            assert raster.image.size[0] > 0
            raster.close()

        # Built-in PDF renderer (when pypdfium2 extra is installed)
        if HAS_PYPDFIUM2:
            with vdocs.open(pdf_path) as pdf_doc:
                assert isinstance(pdf_doc, vdocs.Picture)
                assert pdf_doc.page_count == 1
                assert pdf_doc.pages[0].size[0] > 0
            pdf_loader = vdocs.resolve(pdf_path).resolve_loader()
            assert pdf_loader.__module__ == f"{prefix}.pixcell.docs.pdf"
            assert f"{prefix}.pixcell.docs.pdf" in sys.modules

        # Non-docs resource lookup (terminals.toml) also survives vendoring
        vfacts = importlib.import_module(f"{prefix}.pixcell.terminal_facts")
        assert len(vfacts.load()) > 0
    finally:
        for mod_name in list(sys.modules):
            if mod_name == prefix or mod_name.startswith(f"{prefix}."):
                sys.modules.pop(mod_name, None)


def test_vendored_pixcell_never_imports_toplevel_pixcell(tmp_path: Path) -> None:
    """Verify vendored loaders work even when top-level ``pixcell`` cannot be imported."""
    prefix = "vendored_isolated"
    _vendor_pixcell(tmp_path, prefix=prefix)
    png_path, svg_path, pdf_path = _write_sample_files(tmp_path)

    script = f"""
import importlib.util
import sys

class BlockTopLevelPixcell:
    def find_spec(self, fullname, path, target=None):
        if fullname == "pixcell" or fullname.startswith("pixcell."):
            raise ModuleNotFoundError(f"blocked top-level import: {{fullname}}", name=fullname)
        return None

sys.meta_path.insert(0, BlockTopLevelPixcell())
sys.path.insert(0, {str(tmp_path)!r})

import {prefix}.pixcell.docs as vdocs

img = vdocs.open({str(png_path)!r})
assert isinstance(img, vdocs.Picture) and img.image.size == (8, 6)
img.close()
assert vdocs.resolve({str(png_path)!r}).resolve_loader().__module__ == "{prefix}.pixcell.docs.image"

svg = vdocs.open({str(svg_path)!r})
assert isinstance(svg, vdocs.SVG) and svg.width == 24.0
assert vdocs.resolve({str(svg_path)!r}).resolve_loader().__module__ == "{prefix}.pixcell.docs.svg"

if importlib.util.find_spec("pypdfium2") is not None:
    with vdocs.open({str(pdf_path)!r}) as pdf:
        assert isinstance(pdf, vdocs.Picture) and pdf.page_count == 1
        assert pdf.pages[0].size[0] > 0
    assert vdocs.resolve({str(pdf_path)!r}).resolve_loader().__module__ == "{prefix}.pixcell.docs.pdf"

assert not any(m == "pixcell" or m.startswith("pixcell.") for m in sys.modules)
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_missing_extra_install_hint_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        # Default hint
        default_err = MissingExtra("pdf", renderer="pdf")
        assert default_err.install_hint == 'pip install "pixcell[pdf]"'
        assert 'Install with: pip install "pixcell[pdf]"' in str(default_err)

        # Callable override (covers both Registry.open and direct renderer MissingExtra)
        docs.set_install_hint(lambda extra: f'uv pip install "deft-console[{extra}]"')
        custom_err = MissingExtra("pdf", renderer="pdf", dependency="pypdfium2")
        assert custom_err.install_hint == 'uv pip install "deft-console[pdf]"'
        assert 'Install with: uv pip install "deft-console[pdf]"' in str(custom_err)

        svg_path = tmp_path / "hint.svg"
        svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
            encoding="utf-8",
        )
        monkeypatch.setitem(sys.modules, "resvg_py", None)
        with pytest.raises(MissingExtra) as svg_exc:
            docs.open(svg_path, rasterize=True)
        assert 'Install with: uv pip install "deft-console[svg]"' in str(svg_exc.value)

        # String template override (both with {extra} and static)
        docs.set_install_hint('pip install "myapp[{extra}]"')
        assert 'Install with: pip install "myapp[docx]"' in str(MissingExtra("docx"))

        docs.set_install_hint('pip install "myapp[docs]"')
        assert 'Install with: pip install "myapp[docs]"' in str(MissingExtra("xlsx"))

        # Invalid argument type
        with pytest.raises(TypeError):
            docs.set_install_hint(123)  # type: ignore[arg-type]

        # Reset to default
        docs.set_install_hint(None)
        reset_err = MissingExtra("pdf", renderer="pdf")
        assert reset_err.install_hint == 'pip install "pixcell[pdf]"'
        assert 'Install with: pip install "pixcell[pdf]"' in str(reset_err)
    finally:
        docs.set_install_hint(None)
