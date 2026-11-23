// Minimal Web API polyfills for QuickJS (quickjs-ng has atob/btoa/queueMicrotask/performance only).
var g = globalThis;
if (typeof g.TextEncoder === "undefined") {
  g.TextEncoder = class TextEncoder {
    get encoding() { return "utf-8"; }
    encode(str) {
      str = String(str ?? "");
      const out = [];
      for (let i = 0; i < str.length; i++) {
        let c = str.charCodeAt(i);
        if (c >= 0xd800 && c <= 0xdbff && i + 1 < str.length) {
          const d = str.charCodeAt(i + 1);
          if (d >= 0xdc00 && d <= 0xdfff) { c = 0x10000 + ((c - 0xd800) << 10) + (d - 0xdc00); i++; }
          else c = 0xfffd;
        } else if (c >= 0xd800 && c <= 0xdfff) c = 0xfffd;
        if (c < 0x80) out.push(c);
        else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
        else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
        else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      }
      return new Uint8Array(out);
    }
  };
}
if (typeof g.TextDecoder === "undefined") {
  g.TextDecoder = class TextDecoder {
    constructor(label = "utf-8") {
      const l = String(label).toLowerCase();
      this.encoding = l === "utf8" ? "utf-8" : l;
      if (!["utf-8", "utf-16le", "latin1", "iso-8859-1", "ascii", "windows-1252"].includes(this.encoding))
        throw new RangeError("TextDecoder polyfill: unsupported encoding " + label);
    }
    decode(input) {
      if (input == null) return "";
      const b = input instanceof Uint8Array ? input
        : ArrayBuffer.isView(input) ? new Uint8Array(input.buffer, input.byteOffset, input.byteLength)
        : new Uint8Array(input);
      const parts = []; let chunk = [];
      const flush = () => { parts.push(String.fromCharCode.apply(null, chunk)); chunk = []; };
      const push = (c) => { if (c > 0xffff) { c -= 0x10000; chunk.push(0xd800 + (c >> 10), 0xdc00 + (c & 1023)); } else chunk.push(c); if (chunk.length > 8000) flush(); };
      if (this.encoding === "utf-16le") {
        for (let i = 0; i + 1 < b.length; i += 2) { chunk.push(b[i] | (b[i + 1] << 8)); if (chunk.length > 8000) flush(); }
      } else if (this.encoding !== "utf-8") {
        for (let i = 0; i < b.length; i++) push(b[i]);
      } else {
        let i = 0;
        if (b.length >= 3 && b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) i = 3;
        while (i < b.length) {
          const x = b[i++];
          if (x < 0x80) push(x);
          else if (x >= 0xc0 && x < 0xe0 && i < b.length) push(((x & 31) << 6) | (b[i++] & 63));
          else if (x >= 0xe0 && x < 0xf0 && i + 1 < b.length) { push(((x & 15) << 12) | ((b[i] & 63) << 6) | (b[i + 1] & 63)); i += 2; }
          else if (x >= 0xf0 && i + 2 < b.length) { push(((x & 7) << 18) | ((b[i] & 63) << 12) | ((b[i + 1] & 63) << 6) | (b[i + 2] & 63)); i += 3; }
          else push(0xfffd);
        }
      }
      flush();
      return parts.join("");
    }
  };
}
if (typeof g.crypto === "undefined") g.crypto = {};
if (typeof g.crypto.getRandomValues !== "function") {
  // Not cryptographic: only used for unique SVG element ids.
  g.crypto.getRandomValues = function (arr) { for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256); return arr; };
}
if (typeof g.crypto.randomUUID !== "function") {
  g.crypto.randomUUID = function () {
    const b = g.crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  };
}
if (typeof g.structuredClone !== "function") {
  g.structuredClone = function structuredClone(v, seen = new Map()) {
    if (v === null || typeof v !== "object") return v;
    if (seen.has(v)) return seen.get(v);
    let out;
    if (ArrayBuffer.isView(v)) out = v.slice();
    else if (v instanceof ArrayBuffer) out = v.slice(0);
    else if (v instanceof Date) out = new Date(v.getTime());
    else if (v instanceof RegExp) out = new RegExp(v.source, v.flags);
    else if (v instanceof Map) { out = new Map(); seen.set(v, out); v.forEach((val, k) => out.set(structuredClone(k, seen), structuredClone(val, seen))); return out; }
    else if (v instanceof Set) { out = new Set(); seen.set(v, out); v.forEach((val) => out.add(structuredClone(val, seen))); return out; }
    else if (Array.isArray(v)) { out = []; seen.set(v, out); for (let i = 0; i < v.length; i++) out[i] = structuredClone(v[i], seen); return out; }
    else { out = {}; seen.set(v, out); for (const k of Object.keys(v)) out[k] = structuredClone(v[k], seen); return out; }
    seen.set(v, out); return out;
  };
}
if (typeof g.console === "undefined") {
  const noop = function () {};
  g.console = { log: noop, warn: noop, error: noop, info: noop, debug: noop };
}
if (typeof g.setTimeout === "undefined") {
  // Run callbacks as microtasks; enough for promise-style code, no real delays.
  g.setTimeout = function (fn, _ms, ...args) { queueMicrotask(() => fn(...args)); return 0; };
  g.clearTimeout = function () {};
}
