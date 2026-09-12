"""Compare browser typed answers with Anki in a disposable collection."""

from pathlib import Path
import random
import subprocess
import sys
import tempfile

from fixture_deck import REPO, require_anki, save_json


def build(root):
    from anki.buildinfo import version
    from anki.collection import Collection

    pairs = [
        ("cat", "cat"), ("cat", "cut"), ("cat", ""), ("", ""),
        ("cat", "cats"), ("cat", "ct"), ("猫, 犬", "猫"),
        ("élite", "elite"), ("Cat", "cat"), ("が", "か"),
        ("が", "か\u3099"), ("猫", "犬"), ("abc", "xyz"),
        ("abc", "axbyc"), ("<b>猫 &amp; 犬</b>", "猫 & 犬"),
        ("&lt;b&gt;literal&lt;/b&gt;", "<b>literal</b>"),
        (" cat ", " cat "), ("cat\ndog", "cat dog"),
        ("cat<br>dog", "cat dog"), ("<div>cat</div><div>dog</div>", "cat dog"),
        ("cat[sound:cat.mp3]", "cat"), ("猫&nbsp;犬", "猫 犬"),
        ("<script>bad()</script>cat", "cat"), ("<style>bad</style>cat", "cat"),
        ("👨‍👩‍👧猫", "👨‍👩‍👧犬"), ("قَلْب", "قلب"),
        ("tide", "diet"), ("ababa", "babab"), ("abc", "bcax"),
        ("café", "cafe\u0301"), ("猫\t犬", "猫 犬"),
        ("élite", "elide"), ("がっこう", "かつこう"),
        ("قَلْب", "قلم"), ("\u0301a", "a"), ("a", "\u0301a"),
        ("cat<br class='line'>dog", "cat dog"),
        ("<p>cat</p><p>dog</p>", "cat dog"),
        ("cat\r\ndog", "cat dog"), ("cat  dog", "cat dog"),
        ("cat&#10;dog", "cat dog"), ("cat", "cat\u00a0"),
        ("猫", "<img src=x onerror=alert(1)>"),
        ("ＡＢ", "AB"), ("ﬁle", "file"), ("①", "1"), ("ｶﾞ", "ガ"),
        ("ﬁle", "fide"), ("각", "가"),
    ]
    randomizer = random.Random(20260913)
    for _ in range(100):
        pairs.append(tuple("".join(randomizer.choices("ab c猫犬", k=randomizer.randrange(1, 16))) for _ in range(2)))
    with tempfile.TemporaryDirectory(prefix="collection-", dir=root) as scratch:
        col = Collection(str(Path(scratch) / "collection.anki2"))
        try:
            cases = [{"expected": expected, "provided": provided, "combining": combining,
                "html": col._backend.compare_answer(expected=expected, provided=provided, combining=combining)}
                for expected, provided in pairs for combining in (True, False)]
            save_json(root / "expected.json", {"anki_version": version, "cases": cases})
        finally:
            col.close()


if __name__ == "__main__":
    require_anki()
    root = Path(tempfile.mkdtemp(prefix="anki-typed-", dir=REPO / "tmp"))
    print(f"Artifacts: {root}", flush=True)
    build(root)
    compiled = subprocess.run(["npm", "run", "build", "--workspace", "@shirabe-org/study-runtime"], cwd=REPO)
    if compiled.returncode:
        sys.exit(compiled.returncode)
    result = subprocess.run(["node", "anki-addon/tests/typed_answer_browser.mjs", str(root)], cwd=REPO)
    sys.exit(result.returncode)
