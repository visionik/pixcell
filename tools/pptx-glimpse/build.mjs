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

function parseUnifiedDiff(patchText, patchName) {
  const lines = patchText.replace(/\r\n/g, "\n").split("\n");
  const files = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].startsWith("--- ")) {
      i++;
      continue;
    }
    const oldHeader = lines[i++].slice(4).trim().split("\t")[0];
    if (i >= lines.length || !lines[i].startsWith("+++ ")) {
      throw new Error(`${patchName}: expected '+++ ' header after '--- ${oldHeader}'`);
    }
    const newHeader = lines[i++].slice(4).trim().split("\t")[0];
    const relPath = (newHeader !== "/dev/null" ? newHeader : oldHeader).replace(/^[ab]\//, "");
    const hunks = [];
    while (i < lines.length && !lines[i].startsWith("--- ")) {
      if (!lines[i].startsWith("@@ ")) {
        i++;
        continue;
      }
      const match = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(lines[i++]);
      if (!match) {
        throw new Error(`${patchName}: malformed hunk header near line ${i}`);
      }
      const oldStart = Number(match[1]);
      const oldCount = match[2] === undefined ? 1 : Number(match[2]);
      const hunkLines = [];
      let seenOld = 0;
      const newCount = match[4] === undefined ? 1 : Number(match[4]);
      let seenNew = 0;
      while (i < lines.length && (seenOld < oldCount || seenNew < newCount)) {
        const line = lines[i];
        if (line.startsWith("\\ No newline at end of file")) {
          i++;
          continue;
        }
        const prefix = line[0];
        const content = line.slice(1);
        if (prefix === " ") {
          hunkLines.push({ op: " ", content });
          seenOld++;
          seenNew++;
        } else if (prefix === "-") {
          hunkLines.push({ op: "-", content });
          seenOld++;
        } else if (prefix === "+") {
          hunkLines.push({ op: "+", content });
          seenNew++;
        } else {
          throw new Error(
            `${patchName}: unexpected diff prefix '${prefix ?? ""}' in ${relPath} at line ${i + 1}`
          );
        }
        i++;
      }
      hunks.push({ oldStart, hunkLines });
    }
    files.push({ relPath, hunks });
  }
  return files;
}

function hunkMatchesAt(fileLines, pos, hunkLines) {
  let cursor = pos;
  for (const entry of hunkLines) {
    if (entry.op === "+") continue;
    if (cursor < 0 || cursor >= fileLines.length || fileLines[cursor] !== entry.content) {
      return false;
    }
    cursor++;
  }
  return true;
}

function findHunkPosition(fileLines, expectedPos, hunkLines) {
  if (hunkMatchesAt(fileLines, expectedPos, hunkLines)) return expectedPos;
  for (let delta = 1; delta <= 200; delta++) {
    if (hunkMatchesAt(fileLines, expectedPos + delta, hunkLines)) return expectedPos + delta;
    if (hunkMatchesAt(fileLines, expectedPos - delta, hunkLines)) return expectedPos - delta;
  }
  return -1;
}

function applyPatches() {
  const patchesDir = path.join(here, "patches");
  if (!fs.existsSync(patchesDir)) return;
  const patchFiles = fs
    .readdirSync(patchesDir)
    .filter((name) => name.endsWith(".patch"))
    .sort();
  if (patchFiles.length === 0) return;

  const parsedPatches = patchFiles.map((name) => ({
    name,
    files: parseUnifiedDiff(fs.readFileSync(path.join(patchesDir, name), "utf8"), name),
  }));

  const nodeModulesDir = path.join(here, "node_modules");
  const touchedPaths = new Set();
  for (const patch of parsedPatches) {
    for (const file of patch.files) {
      touchedPaths.add(file.relPath);
    }
  }

  for (const relPath of touchedPaths) {
    const targetPath = path.join(nodeModulesDir, relPath);
    const origPath = `${targetPath}.orig`;
    if (!fs.existsSync(targetPath)) {
      throw new Error(`Patch target missing: ${targetPath} (run 'npm install' first)`);
    }
    if (!fs.existsSync(origPath)) {
      fs.copyFileSync(targetPath, origPath);
    } else {
      fs.copyFileSync(origPath, targetPath);
    }
  }

  for (const patch of parsedPatches) {
    for (const file of patch.files) {
      const targetPath = path.join(nodeModulesDir, file.relPath);
      const fileLines = fs.readFileSync(targetPath, "utf8").replace(/\r\n/g, "\n").split("\n");
      let offset = 0;
      for (let hIdx = 0; hIdx < file.hunks.length; hIdx++) {
        const hunk = file.hunks[hIdx];
        const expectedPos = hunk.oldStart - 1 + offset;
        const matchPos = findHunkPosition(fileLines, expectedPos, hunk.hunkLines);
        if (matchPos < 0) {
          throw new Error(
            `Failed to apply ${patch.name} hunk #${hIdx + 1} to ${file.relPath} near line ${hunk.oldStart}`
          );
        }
        const replacement = [];
        let removeCount = 0;
        for (const entry of hunk.hunkLines) {
          if (entry.op === " " || entry.op === "+") replacement.push(entry.content);
          if (entry.op === " " || entry.op === "-") removeCount++;
        }
        fileLines.splice(matchPos, removeCount, ...replacement);
        offset = matchPos - (hunk.oldStart - 1) + (replacement.length - removeCount);
      }
      fs.writeFileSync(targetPath, fileLines.join("\n"), "utf8");
    }
    console.log(`Applied patch: ${patch.name}`);
  }
}

applyPatches();

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
