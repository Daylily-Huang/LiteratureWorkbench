import { cp, mkdir, readFile, readdir, rm, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appPackagePath = path.join(root, "package.json");
const pdfPackagePath = path.join(root, "node_modules", "pdfjs-dist", "package.json");
const appPackage = JSON.parse(await readFile(appPackagePath, "utf8"));
const pdfPackage = JSON.parse(await readFile(pdfPackagePath, "utf8"));
const pinnedVersion = appPackage.dependencies?.["pdfjs-dist"];

if (typeof pinnedVersion !== "string" || !/^\d+\.\d+\.\d+$/.test(pinnedVersion)) {
  throw new Error("pdfjs_dependency_must_be_exactly_pinned");
}
if (pdfPackage.version !== pinnedVersion) {
  throw new Error(
    `pdfjs_version_mismatch: expected ${pinnedVersion}, installed ${pdfPackage.version}`,
  );
}

const outputRoot = path.join(root, "dist", "pdfjs");
await mkdir(outputRoot, { recursive: true });

for (const directory of ["cmaps", "standard_fonts", "wasm", "iccs"]) {
  const source = path.join(root, "node_modules", "pdfjs-dist", directory);
  const sourceStat = await stat(source);
  const entries = await readdir(source);
  if (!sourceStat.isDirectory() || entries.length === 0) {
    throw new Error(`pdfjs_asset_directory_missing_or_empty:${directory}`);
  }
  const destination = path.join(outputRoot, directory);
  await rm(destination, { recursive: true, force: true });
  await cp(source, destination, { recursive: true, errorOnExist: true });
}
