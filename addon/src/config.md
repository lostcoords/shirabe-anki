## Shirabe settings

Use **Tools → Shirabe → Settings** instead of editing login data here.

- `enabled`: show the Desktop popup on cards. Default: `true`.
- `trigger`: `click` by default; also `hover`, `shift`, `alt`, `ctrl`, or `meta`.
  Touch uses a tap.
- `answerOnly`: enable lookup after showing the answer. Default: `true`.
- `onlineParsing`: send visible Japanese text for word boundaries and study
  colours. Default: `false`. With this off, an explicit lookup sends the chosen
  word and nearby text unless a prepared offline result answers it.
- `useCollectionStates`: use mapped Anki expression fields for study colours.
  Default: `true`. Unmapped notes do not imply that a word is being studied.
- `definitionSize`: `small`, `medium`, or `large`. Default: `medium`.
- `theme`: `system`, `light`, or `dark`. Default: `system`.
- `apiBase`: the dictionary server, under **Advanced → Server**.
- `reportUsage`: send software events and Anki, Qt, and operating-system
  versions, without lookup text. Default: `true`.
- `offlineStorageMiB`: the collection's prepared snapshot limit. Default:
  `25`; adjustable in the offline preparation dialog.

Note-type side and parsing choices override the corresponding Desktop defaults.
Use **Add to a note type (for phones)…** to change those choices or exclude fields.

Use **Note fields, deck and tags…** to maintain reusable authoring profiles.
Profiles map fields; they do not grant card scripts permission to write notes.
The native editor or a field-change confirmation remains required.

`cardToken` contains the device's read-only login and pending authorization.
Sign-in writes it; sign-out clears it. It must not be copied into a note,
template, support report, or shared add-on package. Other scripts in a trusted
card's WebView can read it, including access to private uploaded dictionaries.

Prepared excerpts are separate collection media, not login data. They may travel
with deck exports and remain after sign-out. Review their licenses before sharing.
