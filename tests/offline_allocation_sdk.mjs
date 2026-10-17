import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const require = createRequire(new URL("../package.json", import.meta.url));
const { Window } = await import(pathToFileURL(require.resolve("happy-dom")).href);
const window = new Window({ url: "https://fixture.invalid/" });
window.TextDecoder = TextDecoder;
window.TextEncoder = TextEncoder;
let networkAttempts = 0;
const forbidden = async () => { networkAttempts++; throw new Error("Network forbidden"); };
window.fetch = forbidden;
globalThis.fetch = forbidden;
const bundle = readFileSync(new URL("../addon/src/web/shirabe-card.js", import.meta.url));
const bundleHash = createHash("sha256").update(bundle).digest("hex");
assert.equal(bundleHash, process.argv[2], "SDK bundle differs from frozen input");
window.eval(bundle.toString());
const plain = (value) => JSON.parse(JSON.stringify(value));
const same = (left, right) => {
  try { assert.deepEqual(plain(left), plain(right)); return true; }
  catch { return false; }
};
const found = (result) => result.status === "found" && result.definition !== null;
const matches = (token, other) => (token.lemma ?? token.surface) === (other.lemma ?? other.surface)
  && token.surface === other.surface && (!token.reading || token.reading === other.reading)
  && (!token.pos || token.pos === other.pos);

try {
  for await (const line of createInterface({ input: process.stdin })) {
    const { index, before, after, checks } = JSON.parse(line);
    const options = { apiBase: "https://fixture.invalid", contentVersion: after.contentVersion,
      texts: after.segments.map((segment) => segment.text) };
    const hosts = Object.fromEntries(Object.entries({ before, after }).map(([key, snapshot]) =>
      [key, window.ShirabeCard.snapshotHost(undefined, snapshot, options)]));
    for (const host of Object.values(hosts)) {
      for (const segment of after.segments) assert.deepEqual(plain(await host.tokenize(segment.text)), segment.tokens);
    }
    const rows = [];
    for (const check of checks) {
      const segment = after.segments[0], token = segment.tokens[check.token];
      const reference = { ...after, complete: true, segments: [{ ...segment, tokens: [token],
        results: [{ tokenIndex: 0, candidates: check.candidates }] }] };
      const expected = window.ShirabeCard.snapshotHost(undefined, reference, options);
      const context = { surface: token.surface, reading: token.reading, pos: token.pos };
      const lookup = (host) => host.lookup(token.lemma ?? token.surface, "word", context);
      const primary = (host) => token.entry ? host.lookupEntry(token.entry, token.surface) : lookup(host);
      const referencePrimary = await primary(expected), referenceContext = await lookup(expected);
      const row = { token: check.token, surface: token.surface, query: token.lemma ?? token.surface,
        pinned: !!token.entry, entry: token.entry ?? null,
        repeatedContext: segment.tokens.filter((other) => other.content !== false && matches(token, other)).length > 1,
        expectedDefinitionAvailable: found(referencePrimary), expectedCandidates: referencePrimary.candidates?.map((c) => c.entry) ?? [] };
      for (const [side, host] of Object.entries(hosts)) {
        const value = await primary(host), contextual = await lookup(host);
        row[side] = {
          primaryFound: found(value),
          primaryExpectedDefinition: found(value) && same(value.definition, referencePrimary.definition),
          primaryExpectedAlternatives: found(value) && same(value.candidates, referencePrimary.candidates),
          primaryExactResult: same(value, referencePrimary),
          contextualFound: found(contextual),
          contextualExpectedDefinition: found(contextual) && same(contextual.definition, referenceContext.definition),
          contextualExpectedAlternatives: found(contextual) && same(contextual.candidates, referenceContext.candidates),
          contextualExactResult: same(contextual, referenceContext),
        };
        if ((found(value) && !row[side].primaryExpectedDefinition) || (found(contextual) && !row[side].contextualExpectedDefinition)) {
          row[`${side}DefinitionDifference`] = { primaryEntry: value.definition?.entry ?? null, contextualEntry: contextual.definition?.entry ?? null,
            primaryInflection: value.definition?.inflection ?? null, contextualInflection: contextual.definition?.inflection ?? null };
        }
      }
      if ((row.before.primaryExpectedDefinition && !row.after.primaryExpectedDefinition)
        || (row.before.contextualExpectedDefinition && !row.after.contextualExpectedDefinition)) {
        row.matchingResults = Object.fromEntries(Object.entries({ before, after }).map(([side, snapshot]) => [side,
          snapshot.segments[0].results.filter((result) => matches(token, snapshot.segments[0].tokens[result.tokenIndex]))
            .map((result) => ({ token: result.tokenIndex, entries: result.candidates.map((candidate) => `${candidate.dictionary}:${candidate.sourceId}`),
              payloadSha256: createHash("sha256").update(JSON.stringify(result.candidates)).digest("hex") }))]));
      }
      rows.push(row);
    }
    assert.equal(networkAttempts, 0);
    process.stdout.write(JSON.stringify({ index, rows, bundleHash, networkAttempts, tokenSpansEqual: true }) + "\n");
  }
} finally {
  await window.happyDOM.close();
}
