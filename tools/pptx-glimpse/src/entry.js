import "./polyfills.js";
import { convertPptxToSvg } from "pptx-glimpse";

// base64 -> Uint8Array using QuickJS-ng's native atob.
function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

globalThis.pptxGlimpse = {
  convertPptxToSvg,
  b64ToBytes,
  // Kicks off a conversion; the host drains pending jobs and then reads __result.
  start(pptxB64, optionsJson, fontsB64) {
    globalThis.__result = null;
    const options = optionsJson ? JSON.parse(optionsJson) : {};
    if (fontsB64 && fontsB64.length) options.fonts = fontsB64.map((f) => ({ name: f.name, data: b64ToBytes(f.data) }));
    convertPptxToSvg(b64ToBytes(pptxB64), options).then(
      (r) => { globalThis.__result = JSON.stringify({ ok: true, slides: r.slides, diagnostics: r.diagnostics, supportCoverage: r.supportCoverage }); },
      (e) => { globalThis.__result = JSON.stringify({ ok: false, error: String(e && e.stack ? e.message + "\n" + e.stack : e) }); }
    );
  },
};
