import type { Definition, StudyDetails } from "@shirabe-org/card/word-card/types";
import type { LookupResult, Outcome, WordCardHost } from "@shirabe-org/card/word-card/host";

/** Every call the card made, so a test can assert on what it ASKED for and not
    only on what it drew. */
export type Journal = { lookups: string[]; saves: string[]; notified: string[] };

/**
 * A host with no extension, no pack, no account and no network behind it.
 *
 * This is the point of the contract rather than a convenience for tests: if the
 * card can be driven by 40 lines here, a Vue app and an Anki template can drive
 * it too. A change that makes this file hard to write has broken the seam, and
 * that is worth hearing about here rather than from the second consumer.
 */
export function fakeHost(options: {
  definitions?: Record<string, Definition>;
  details?: Record<string, StudyDetails>;
  study?: boolean;
  pending?: boolean;
} = {}): WordCardHost & { journal: Journal } {
  const journal: Journal = { lookups: [], saves: [], notified: [] };
  const definitions = options.definitions ?? {};
  const outcome = async (word: HTMLElement, message: string): Promise<Outcome> => {
    journal.saves.push(word.dataset.lemma ?? "");
    return { ok: true, message };
  };

  const host: WordCardHost & { journal: Journal } = {
    journal,
    async lookup(query): Promise<LookupResult> {
      journal.lookups.push(query);
      if (options.pending) return { status: "pending", message: "Getting ready…" };
      return { status: "found", definition: definitions[query] ?? null };
    },
    async lookupEntry(entry): Promise<LookupResult> {
      journal.lookups.push(entry);
      const found = Object.values(definitions).find((d) => d.entry === entry);
      return { status: "found", definition: found ?? null };
    },
    notify: (message) => void journal.notified.push(message),
  };

  if (options.study !== false) {
    host.study = {
      async states(lemmas) {
        return Object.fromEntries(lemmas.map((l) => [l, options.details?.[l] ?? { state: "none", decks: [] }]));
      },
      async urlForWord() { return "https://shirabe.org/en/study/cards"; },
      apply(word, details) { word.dataset.state = details.state; },
      save: (word) => outcome(word, "Saved"),
      markKnown: (word) => outcome(word, "Marked known"),
    };
  }
  return host;
}
