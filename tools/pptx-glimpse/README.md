# Rebuilding the vendored `pptx-glimpse` QuickJS bundle

`src/pixcell/docs/vendor/pptx-glimpse.global.min.js` is built with `esbuild` from
this directory:

```bash
cd tools/pptx-glimpse
npm install
npm run build   # runs: node build.mjs --minify
```

What this does:
- Bundles `pptx-glimpse`'s browser entry (`conditions: ["browser"]`) together with
  minimal QuickJS Web API polyfills (`src/polyfills.js`) into an `es2020` IIFE.
- Stubs `@resvg/resvg-wasm` (`stubs/resvg-wasm.js`, unused on the SVG path) and
  Node built-ins (`stubs/empty.js`).
- Writes the minified bundle directly to
  `src/pixcell/docs/vendor/pptx-glimpse.global.min.js` and an esbuild metafile to
  `dist-bundle/meta.json` (ignored by git).

After updating `pptx-glimpse`, verify all bundled dependency licences in
`dist-bundle/meta.json` remain permissive and update
`src/pixcell/docs/vendor/NOTICE`.
