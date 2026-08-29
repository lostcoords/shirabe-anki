## Shirabe

**enabled**: whether the popup goes on cards at all. Also on the toolbar, at
**Tools → Shirabe → Popup on cards**, because it is the one switch worth
reaching without opening a window. Off leaves your cards exactly as their author
wrote them: no scripts, no lookups, no spans.

**trigger**: what opens the card. `hover`, `click`, or `shift` / `alt` / `ctrl` /
`meta` for hover with that key held, which is what the browser extension does and
what keeps a card from appearing under a pointer that is only passing through. A
tap always works on a phone, whichever of these is set.

Sign in from a card, not from here: look up a word and use the **Sign in** link
on it. Add Shirabe to a note type (**Tools → Shirabe → Add to a note type**) and
sync, and you can sign in the same way on your phone.

Sign out in the settings window. That covers this computer; to sign out
everywhere, including phones, remove the device on your shirabe.org account
page.

Looking words up needs a free account. Saving to Shirabe is not offered on a
card: an Anki card keeps its sign-in where the page can read it, because on a
phone there is nowhere else, so it signs in as a client that can only read.

**useCollectionStates**: colour a word by whether YOUR collection already has a
note for it, rather than by your Shirabe account. On by default, because in Anki
your own deck is the better answer to "do I know this word".

**definitionSize**: `small`, `medium`, or `large`. Large is worth it if you read
monolingual definitions.

**theme**: `system`, `light`, or `dark`, for the popup itself.

**apiBase**: under **Advanced** in the window. The Shirabe server to use, for
testing against your own.

**cardToken**: written by signing in and cleared by signing out; never typed.
It is here rather than in the browser's storage because Anki serves its webviews
from a port it picks fresh on every launch, and browser storage belongs to an
origin: kept there, your sign-in would not survive closing Anki. It is a
read-only credential: the client it belongs to is refused a write by the server
however it asks, which is what makes it safe to keep where a card can read it.
