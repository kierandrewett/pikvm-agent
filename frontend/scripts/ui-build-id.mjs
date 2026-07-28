import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { relative, resolve } from "node:path";

const sourceFiles = async (directory) => {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = await Promise.all(
    entries.map(async (entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? sourceFiles(path) : [path];
    }),
  );
  return paths.flat();
};

export const computeUiBuildId = async (frontendRoot) => {
  const inputs = [
    ...(await sourceFiles(resolve(frontendRoot, "src"))),
    resolve(frontendRoot, "index.html"),
    resolve(frontendRoot, "package.json"),
    resolve(frontendRoot, "package-lock.json"),
    resolve(frontendRoot, "vite.config.ts"),
    resolve(frontendRoot, "scripts/ui-build-id.mjs"),
  ].sort();
  const digest = createHash("sha256");
  for (const path of inputs) {
    digest.update(relative(frontendRoot, path));
    digest.update("\0");
    digest.update(await readFile(path));
    digest.update("\0");
  }
  return digest.digest("hex").slice(0, 12);
};
