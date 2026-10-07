# Use Shirabe in Anki

Click Japanese on an answer to open the shared Shirabe popup. Use the Desktop
add-on to create notes, review field changes, and prepare definitions for offline
review on AnkiDroid.

## Connect Desktop

1. In Anki, open **Tools → Add-ons → Install from file** and select the
   `.ankiaddon` package. Restart Anki.
2. Open **Tools → Shirabe → Settings → Sign in**.
3. Approve the connection in your browser. Keep Anki's connection window open
   until it says **Connected**.
4. Review a card, reveal its answer, and click a Japanese word.

If approval was interrupted, choose **Continue sign-in**. If dictionary access
needs renewing, choose **Reconnect…** in Settings.

Use card templates you trust. Card scripts can read the device's login, including
its access to uploaded dictionaries. The dictionary-popup connection cannot write to Shirabe, but
read-only access still includes private information.

## Try two-way study sync

Use a disposable Anki profile for this unreleased feature. Keep your original
collection and media backed up. Study sync currently requires Anki **26.08.1**,
FSRS enabled, and a Shirabe account with Study enabled and permission to save
reviews. Other Anki versions can use dictionary lookup but cannot start study
sync.

1. For a local trial, set **Tools → Shirabe → Settings → Advanced → Server**
   to your development server before connecting.
2. Open **Tools → Shirabe → Study sync…** and choose **Sign in for study sync**.
   This is a separate login from the dictionary popup and grants study writes.
3. Choose **Open approval page**, approve the connection, and return to Anki.
4. Select a small deck and choose **Upload selected decks**. Selection includes
   subdecks and every field of the selected cards' notes. Review the saved copy's
   counts, missing files and shared-note warning, then choose **Upload this copy**.
   Cancel pauses without starting an upload and keeps the local copy for later.
   Keep the window open through the initial upload and reconciliation.
5. Open the copied deck in Shirabe. Edit a note field and review one card.
   Return to the add-on and choose **Sync now**. Inspect both the field and
   the individual card's progress in Anki.
6. Edit or review a different card in Anki, leave its reviewer, and choose
   **Sync now** again. Confirm the change appears in Shirabe.
7. Close Anki and review in Shirabe. Reopen Anki and sync to deliver those
   reviews. Anki also remains usable while Shirabe is unavailable; exchange
   waits until both the desktop add-on and the server are reachable.
8. Enable **Sync automatically** after the manual round trip works. Automatic
   sync waits until you leave Anki's reviewer. Resolve reported conflicts in
   this window before continuing.

With automatic sync enabled, closing Anki or switching profiles first saves open
editors and finishes a Shirabe sync. If it fails, retry or choose **Stay in Anki**
to resolve the problem. **Close without finishing sync** keeps pending progress
for the next sync and waits for the current worker to stop before closing.
Anki's normal shutdown sync runs afterward; anything it downloads reaches
Shirabe on the next sync.

If a legacy note type pauses sync with `ambiguous_mapping`, select that record
and choose **Match fields and templates…**. Match each renamed item to its
previous name. Review the items marked **not retained** before confirming.
Choose **New item** only for an addition, not a rename. Unmatched templates
retire their cards in Shirabe; their history remains.
For a rejected record that needs mapping, choose **Map current Anki fields and
templates…** in its retry window. Choose **Leave pending** to cancel either
confirmation without applying a mapping.

Editing a shared note changes its sibling cards, including siblings outside
the selected decks. Only selected cards' schedules join the link. Deck selection
is fixed after the initial capture in this trial; signing out retains the copied
decks and checkpoints rather than unlinking or deleting them.
Unlinking is currently API-only and stops exchange without deleting copied data.

Before the initial upload, Shirabe checks whether the saved media fits within
your remaining account storage. A rejected upload keeps its local snapshot and
sends no notes, cards or media. After freeing space, resume the same saved copy.
The preflight does not reserve storage; each media upload checks the limit again.

Choose **Template checks…** in the study-sync window to inspect reports for the
last accepted note-type revisions. These source checks do not run JavaScript,
inspect note values or verify media. They do not block synchronization.
Sync template corrections to refresh the reports, then preview actual cards.

Inspect custom-script cards before relying on them. Check the
[template limitations and copied-profile results](../docs/specs/study-notes.md#template-compatibility-has-a-boundary),
especially add-on filters and JP Mining Note diagnostics. Keep Anki available
for cards whose answers do not render correctly. Successful grading alone does
not prove rendering compatibility.

Do not configure Shirabe as Anki's sync server. This feature uses the desktop
add-on; phone changes reach it through Anki's normal synchronization.

## Choose when lookup runs

Open **Tools → Shirabe → Settings** to choose click, hover, or a held modifier.
Keep **Only after revealing the answer** checked to avoid hints during recall.

Choose **Popup theme** and **Definition size** in the same window. If Anki is
dark while your operating system is light, choose the dark popup theme explicitly.

Leave **Identify visible Japanese online** unchecked for lookup only
when you choose a word. Enable it to send visible Japanese text for token
boundaries and study colours. Hidden text and excluded fields stay out of that
request.

For note-type-specific sides and field exclusions, use
**Tools → Shirabe → Add to a note type (for phones)…**. These choices also apply
when the Desktop add-on displays that note type.

If a word boundary is wrong, select the Japanese expression and choose
**Look up selection**. This looks up the chosen span without splitting it again.
Ordinary selection and copy still work.

## Create or enrich notes

1. Open **Tools → Shirabe → Note fields, deck and tags…**.
2. Choose an existing note type. Map the fields you want Shirabe to fill, then
   save the profile. Use **Create Shirabe Japanese note type** only if you want
   a separate vocabulary layout.
3. Open a word popup. Expand **Copy and choose fields** to choose its surface or
   dictionary form, meanings, and examples.
4. Choose **Add Anki note** to review a prefilled native Add window. Choose
   **Update this note** to review changes to the current note.
5. Inspect **Current**, **Action**, and **After** before saving. Keep the default
   fill-empty action unless you intend to append or replace a field.

Use **Find in Anki** to inspect existing notes before creating another one.
Map expression, reading, and entry fields for more precise duplicate matches.
Do not treat a spelling-only candidate as an identical word.

For a sentence or cloze note, choose the corresponding mode in the native
dialog. Select the cloze span and number explicitly. Keep the native editor open
to adjust the card before its final save.

Use **Edit → Undo** to reverse a saved Shirabe field update. If the note changed
while a lookup was running, reopen its preview instead of overwriting the newer
version.

## Look up from the editor or Browser

In Anki's Add or Edit window, select Japanese and press the **調べる** toolbar
button. Search, choose the dictionary match, and preview its fields in the draft.

In the Browser, select notes and choose
**Shirabe → Fill missing fields in selected notes…**. Resolve ambiguous matches
and missing mappings before reviewing the field changes. Use **Retry failed
lookups** for failures; use **Refresh generated fields** only for content that
Shirabe previously filled. Review learner-edited fields individually.

Use **Find examples** to choose alternatives from the sentence corpus. Check the
translation language and source before inserting a sentence. Listen to available
pronunciation in the popup.

To keep a pronunciation in a note, map **Audio** and **Source** in your Shirabe
field profile. In the material preview, choose **Choose recording…**, select a
reading and accent, then choose **Download MP3** when available. Review both
fields before saving. Existing audio stays intact unless you choose to append
or replace it. The Source field keeps the recording credit and permission with
synced and exported cards.

Downloads cover available Shirabe word recordings, not third-party audio from
uploaded dictionaries. Sync media before listening on another device offline.
If an MP3 copy is not available, the picker explains that AnkiWeb will not show
a replay button for M4A. Check **Use M4A anyway** before downloading that format.
This limitation concerns Anki sound tags, not the popup's own audio player.

## Set up AnkiDroid

1. On Desktop, choose **Tools → Shirabe → Add to a note type (for phones)…**.
2. Select a note type, sides, and any fields to exclude. Leave online full-card
   identification off unless you want it.
3. Install the template changes, then sync the collection **and media**.
4. On AnkiDroid, finish the media sync before opening a card.
5. Before signing in on AnkiDroid, enable **Settings → Advanced → localStorage in
   Study Screen**. Restart AnkiDroid after changing it.
6. Reveal the answer and tap Japanese. Sign in from the popup for online lookup.

If the browser link is blocked, expand **Copy sign-in link and code**. Copy the
address into your browser, approve the connection, and return to AnkiDroid.
You do not need to allow every card to launch other apps.

Sign in separately on each device. Do not copy a Desktop login into a note or
template. On AnkiDroid, use **Find in Anki** and the copy controls; unsupported
Desktop editing actions are not offered.

In **Copy and choose fields**, choose meanings or examples, then copy the
Japanese sentences, translations, or both. **Copy current sentence** keeps the
sentence from the card available alongside corpus examples.

If AnkiDroid asks you to sign in again after a restart, check that storage setting.
Avoid force-stopping the app immediately after approving a new login; leave the
reviewer normally first.

## Prepare cards for offline review

1. In the Desktop Browser, select the notes you plan to review. Use a deck or due
   search to narrow the selection.
2. Choose **Shirabe → Prepare selected notes for offline lookup…**.
3. Review the upload and export notice. Approve only dictionaries whose licenses
   permit the excerpts you intend to sync or export.
4. Check storage use and the collection limit, then prepare the selection.
5. Read the coverage report. Sync the collection and media before going offline.

Prepare again after editing card text. If media is missing, finish syncing and
reopen the card. For script-generated text outside the prepared coverage, use
online lookup or prepare a card with that text in its fields.

Expect offline lookup only for prepared text. No complete dictionary pack is
downloaded. Keep private excerpts out of shared deck exports; signing out does
not remove content deliberately saved into your collection.

To remove the phone popup, use **Tools → Shirabe → Remove from note types**.
Review the selected note types before confirming, then sync again.

To recover space, open **Tools → Shirabe → Review offline storage…**. Review
the exact files before moving them to Anki's media trash. This works while
signed out. Keep a backup if you need older snapshots for an undone change.

## Develop and verify

From the repository root, build the add-on's shared popup:

```sh
npm run sdk:assets -- anki
python3 anki-addon/tests/run.py
```

Ordinary Python runs the portable suite. CI runs the same suite against its
pinned Anki runtime to exercise collection and Qt tests. Use the isolated-profile
[Desktop runner](tests/desktop_check.py) and
[fixture deck generator](tests/fixture_deck.py) for client verification.

The renderer comes from the installed `@shirabe-org/card` release. After upgrading
it, copy the bundle again. Restart Anki after changing Python or loading a new
bundle into an existing reviewer.
Use an isolated test profile, not a real study collection.

For a local API, set **Settings → Advanced → Server** to the Rails server's
loopback address. For phone testing, use an address the phone can reach, then
update the installed note type. A phone's `localhost` is the phone itself.
