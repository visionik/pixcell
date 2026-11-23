import * as esbuild from "esbuild";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const stub = path.join(here, "stubs", "empty.js");
const minify = process.argv.includes("--minify");
const outfile = minify
  ? path.resolve(here, "../../src/pixcell/docs/vendor/pptx-glimpse.global.min.js")
  : path.join(here, "dist-bundle", "pptx-glimpse.global.js");

const res = await esbuild.build({
  entryPoints: [path.join(here, "src", "entry.js")],
  bundle: true,
  format: "iife",
  platform: "browser",
  conditions: ["browser"],
  target: "es2020",
  outfile,
  minify,
  legalComments: "eof",
  metafile: true,
  alias: {
    "@resvg/resvg-wasm": path.join(here, "stubs", "resvg-wasm.js"),
    fs: stub,
    "node:fs": stub,
    path: stub,
    "node:path": stub,
    crypto: stub,
    "node:crypto": stub,
    os: stub,
    "node:os": stub,
    url: stub,
    module: stub,
    zlib: stub,
  },
  logLevel: "info",
});

fs.mkdirSync(path.join(here, "dist-bundle"), { recursive: true });
fs.writeFileSync(path.join(here, "dist-bundle", "meta.json"), JSON.stringify(res.metafile));
