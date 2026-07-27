import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const output = fileURLToPath(
  new URL("../../pikvm_agent/harness_ui/app.js", import.meta.url),
);
const javascript = await readFile(output, "utf8");

// Some upstream overlay styles contain space-only template-literal lines.
// Removing only those bytes keeps the compiled asset diff-check clean without
// changing any CSS declarations or runtime behavior.
await writeFile(output, javascript.replace(/^[\t ]+$/gm, ""), "utf8");
