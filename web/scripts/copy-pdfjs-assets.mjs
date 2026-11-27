import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
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

// An explicit output directory also supports isolated release preparation.
if (process.argv.length > 3) throw new Error("expected_at_most_one_output_directory");
const distRoot = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, "dist");
if (distRoot === path.parse(distRoot).root || distRoot === root || distRoot === path.join(root, "node_modules")) {
  throw new Error("unsafe_asset_output_directory");
}
const outputRoot = path.join(distRoot, "pdfjs");
await mkdir(outputRoot, { recursive: true });

for (const directory of ["cmaps", "standard_fonts", "wasm", "iccs"]) {
  const source = path.join(root, "node_modules", "pdfjs-dist", directory);
  const sourceStat = await stat(source);
  const entries = await readdir(source);
  if (!sourceStat.isDirectory() || entries.length === 0) {
    throw new Error(`pdfjs_asset_directory_missing_or_empty:${directory}`);
  }
  const destination = path.join(outputRoot, directory);
  if (path.dirname(destination) !== outputRoot) throw new Error("unsafe_asset_destination");
  await rm(destination, { recursive: true, force: true });
  await cp(source, destination, { recursive: true, errorOnExist: true });
}

// Keep the upstream license text alongside the bundled browser components.
// This does not replace a review of the backend, models, or distribution policy.
const lock = JSON.parse(await readFile(path.join(root, "package-lock.json"), "utf8"));
const notices = [];
const noticeName = /^(license|licence|copying|notice|copyright)(?:$|[._-])/i;
for (const name of ["react", "react-dom", "scheduler", "pdfjs-dist"]) {
  const packageRoot = path.join(root, "node_modules", name);
  const metadata = JSON.parse(await readFile(path.join(packageRoot, "package.json"), "utf8"));
  if (metadata.version !== lock.packages?.[`node_modules/${name}`]?.version) {
    throw new Error(`notice_package_lock_mismatch:${name}`);
  }
  const files = (await readdir(packageRoot, { withFileTypes: true }))
    .filter(entry => entry.isFile() && noticeName.test(entry.name)).map(entry => entry.name).sort();
  if (!files.some(filename => /^license(?:$|[._-])/i.test(filename))) {
    throw new Error(`package_license_file_missing:${name}`);
  }
  for (const filename of files) {
    const source = path.join(packageRoot, filename);
    const relative = path.posix.join("licenses", name, filename);
    const destination = path.join(distRoot, ...relative.split("/"));
    const contents = await readFile(source);
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, contents);
    notices.push({ package: name, version: metadata.version, declared_license: metadata.license ?? null,
      source: `node_modules/${name}/${filename}`, file: relative, sha256: createHash("sha256").update(contents).digest("hex") });
  }
}
for (const directory of ["cmaps", "standard_fonts", "wasm", "iccs"]) {
  for (const entry of (await readdir(path.join(outputRoot, directory), { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isFile() || !noticeName.test(entry.name)) continue;
    const relative = path.posix.join("pdfjs", directory, entry.name);
    const contents = await readFile(path.join(distRoot, ...relative.split("/")));
    notices.push({ package: "pdfjs-dist", version: pdfPackage.version, declared_license: null,
      source: `node_modules/pdfjs-dist/${directory}/${entry.name}`, file: relative,
      sha256: createHash("sha256").update(contents).digest("hex") });
  }
}
await writeFile(path.join(distRoot, "licenses", "third-party.json"), JSON.stringify({
  schema_version: 1,
  scope: "Browser packages and copied PDF.js asset license files; not a complete product license assessment.",
  files: notices,
}, null, 2) + "\n", "utf8");
