import { copyFile } from "node:fs/promises";

await copyFile(new URL("../node_modules/@shirabe-org/card/dist/shirabe-card.js", import.meta.url), new URL("../addon/src/web/shirabe-card.js", import.meta.url));
console.log("Copied the released card SDK into addon/src/web/shirabe-card.js");
