import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

test("the README and the example point at the URL the add-on actually loads", () => {
  const addon = readFileSync("../../addon/src/template.py", "utf8");
  const host = addon.match(/CDN = "([^"]+)" \+ BUNDLE_PATH/)?.[1];
  const path = addon.match(/BUNDLE_PATH = "([^"]+)"/)?.[1];
  assert.ok(host && path, "found the add-on's bundle URL");
  const shipped = host + path;

  for (const path of ["../../node_modules/@shirabe-org/card/README.md", "../../addon/examples/anki-card.html"]) {
    const cited = readFileSync(path, "utf8").match(/https:\/\/cdn\.shirabe\.org\/\S+?\.js/)?.[0];
    assert.equal(cited, shipped, `${path} cites a bundle URL the add-on does not load`);
  }
});
