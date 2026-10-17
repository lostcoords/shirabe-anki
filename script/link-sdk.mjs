import { execFileSync } from "node:child_process";
import { lstat, mkdir, readFile, realpath, rm, symlink } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const sdk = await realpath(resolve(process.argv[2] ?? join(root, "../shirabe-sdk")));
const names = ["api", "card"];
for (const name of names) {
  const manifest = JSON.parse(await readFile(join(sdk, "packages", name, "package.json"), "utf8"));
  if (manifest.name !== `@shirabe-org/${name}`) throw new Error(`${sdk} is not the Shirabe SDK checkout`);
}
execFileSync("npm", ["run", "build", "--workspace", "@shirabe-org/api", "--workspace", "@shirabe-org/card"], { cwd: sdk, stdio: "inherit" });
const scope = join(root, "node_modules/@shirabe-org");
await mkdir(scope, { recursive: true });
if (await realpath(scope) !== scope) throw new Error("Refusing to replace packages outside this checkout");
for (const name of names) {
  const destination = join(scope, name);
  const installed = await lstat(destination).catch((error) => {
    if (error.code !== "ENOENT") throw error;
    return null;
  });
  if (installed) await rm(destination, { recursive: true, force: true });
  await symlink(join(sdk, "packages", name), destination, "dir");
}
console.log(`Local SDK linked: ${sdk}. Run npm ci to restore the published packages.`);
