# Vendored JavaScript Bundles

`pptx-glimpse.global.min.js` is a browser-targeted IIFE bundle of
[pptx-glimpse](https://github.com/hirokisakabe/pptx-glimpse) built for execution
inside QuickJS (`quickjs-ng`) by `pixcell.docs.pptx`.

## Rebuilding the bundle

The build script, QuickJS polyfills, and module stubs live in
[`tools/pptx-glimpse/`](../../../../tools/pptx-glimpse/):

```bash
cd tools/pptx-glimpse
npm install
node build.mjs --minify
```

This installs `pptx-glimpse` and `esbuild`, bundles `src/entry.js` with browser
conditions and stubs for `@resvg/resvg-wasm` and Node built-ins, and updates
`src/pixcell/docs/vendor/pptx-glimpse.global.min.js`.

After rebuilding, inspect `dist-bundle/meta.json` and update `NOTICE` in this
directory if any bundled dependencies or versions changed.
