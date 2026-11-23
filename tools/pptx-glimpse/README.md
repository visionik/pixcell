# Rebuilding the vendored `pptx-glimpse` QuickJS bundle

`src/pixcell/docs/vendor/pptx-glimpse.global.min.js` is built with `esbuild` from
this directory:

```bash
cd tools/pptx-glimpse
npm ci          # or: npm install
npm run build   # runs: node build.mjs --minify
```

What this does:
- Applies ordered fidelity patches from `patches/*.patch` onto `node_modules/`
  (restoring pristine `.orig` backups first so repeated builds are idempotent; see
  `patches/README.md` for per-patch details).
- Bundles `pptx-glimpse`'s browser entry (`conditions: ["browser"]`) together with
  minimal QuickJS Web API polyfills (`src/polyfills.js`) into an `es2020` IIFE.
- Stubs `@resvg/resvg-wasm` (`stubs/resvg-wasm.js`, unused on the SVG path) and
  Node built-ins (`stubs/empty.js`).
- Writes the minified bundle directly to
  `src/pixcell/docs/vendor/pptx-glimpse.global.min.js` and an esbuild metafile to
  `dist-bundle/meta.json` (ignored by git).

Visual verification across the five fidelity test slides:

```bash
uv run --all-extras python tools/pptx-glimpse/fidelity_check.py --prefix after --out-dir /tmp/pptx-fidelity-png
```

After updating `pptx-glimpse`, verify all bundled dependency licences in
`dist-bundle/meta.json` remain permissive, re-verify or upstream patches in
`patches/`, and update `src/pixcell/docs/vendor/NOTICE`.
