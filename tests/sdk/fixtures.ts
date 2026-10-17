import type { Definition, StudyDetails } from "@shirabe-org/card/word-card/types";

// Definitions shaped exactly as a host hands them over. Written out rather than
// fetched, so the card can be tested and demonstrated with no dictionary, no
// account and no network: which is the whole claim the contract makes.

export const taberu: Definition = {
  headword: "食べる",
  reading: "たべる",
  entry: "jmdict:1358280",
  url: "https://shirabe.org/en/words/食べる-たべる",
  common: true,
  jlpt: "N5",
  frequency: 312,
  pitch: [{ downstep: 2, audioUrl: "https://cdn.shirabe.org/audio/abc123.opus" }],
  senses: [
    {
      position: 0,
      glosses: [{ lang: "eng", tag: "EN", text: "to eat" }],
      partsOfSpeech: [{ label: "Ichidan verb", code: "v1", category: "pos" }],
      tags: [],
    },
    {
      position: 1,
      glosses: [{ lang: "eng", tag: "EN", text: "to live on (e.g. a salary)" }],
      partsOfSpeech: [{ label: "Ichidan verb", code: "v1", category: "pos" }],
      tags: [],
    },
  ],
  examples: [],
  forms: [{ text: "喰べる", common: false, rare: true }],
  sourceForm: "食べた",
  inflection: ["past"],
};

export const kimi: Definition = {
  headword: "君",
  reading: "きみ",
  entry: "jmdict:1247250",
  url: "https://shirabe.org/en/words/君-きみ",
  common: true,
  jlpt: "N4",
  frequency: 205,
  pitch: [{ downstep: 0, audioUrl: null }],
  senses: [{
    position: 0,
    glosses: [{ lang: "eng", tag: "EN", text: "you" }],
    partsOfSpeech: [{ label: "Pronoun", code: "pn", category: "pos" }],
    tags: [{ label: "Usually kana", code: "uk", category: "misc" }],
  }],
  examples: [],
};

export const studied: StudyDetails = {
  state: "learning",
  decks: [{ id: 1, name: "Mining" }],
  cardId: 42,
  cardCount: 1,
};
