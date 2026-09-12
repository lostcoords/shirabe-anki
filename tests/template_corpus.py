"""Compare Shirabe template expansion with rendered output from disposable Anki notes."""

import json
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from fixture_deck import REPO, require_anki, save_json


def template_cases():
    cases = [
        ("raw", "{{Front}}", "{{FrontSide}}<hr>{{Back}}", ["<b>{{Back}}</b>", "answer", ""], False),
        ("sections", "{{#Front}}A{{^Optional}}B{{#Back}}{{Back}}{{/Back}}{{/Optional}}{{/Front}}", "{{FrontSide}}", ["猫", "cat", ""], False),
        ("html-empty", "{{#Optional}}yes{{/Optional}}{{^Optional}}no{{/Optional}}{{Front}}", "{{Back}}", ["猫", "cat", "<br>"], False),
        ("nbsp-empty", "{{#Optional}}yes{{/Optional}}{{^Optional}}no{{/Optional}}{{Front}}", "{{Back}}", ["猫", "cat", "&nbsp;"], False),
        ("text", "{{text:Front}}", "{{text:Back}}", ["<b>猫 &amp; 犬</b>", "&lt;b&gt;literal&lt;/b&gt;<br>next", ""], False),
        ("hint", "{{hint:Front}}|{{hint:Optional}}", "{{hint:furigana:Front}}", ["<b>猫[ねこ]</b>", "cat", ""], False),
        ("hint-text", "{{text:hint:Front}}", "{{text:hint:Back}}", ["<b>猫</b>", "&lt;b&gt;literal&lt;/b&gt;<br>next", ""], False),
        ("hint-empty", "{{Front}}{{hint:Optional}}", "{{hint:Optional}}", ["猫", "cat", ""], False),
        ("latex-png", "{{Front}}", "{{FrontSide}}|{{text:FrontSide}}", ["[latex]猫[/latex] [$]x^2[/$] [$$]x^2[/$$]", "", ""], False),
        ("latex-svg", "[$]{{Front}}[/$]", "{{FrontSide}}", ["x^2", "", ""], False),
        ("latex-cloze", "{{cloze:Front}}", "{{cloze:Front}}", ["[latex]{{c1::x}}+{{c2::y}}[/latex]", "", ""], True),
        ("ruby", "{{kanji:Front}}|{{kana:Front}}|{{furigana:Front}}", "{{text:furigana:Front}}", ["世[よ]の 中[なか]", "", ""], False),
        ("empty-filter", "{{furigana::Front}}|{{:text:Front}}", "{{text::furigana:Front}}|{{::Front}}", ["<b>猫[ねこ]</b>", "", ""], False),
        ("missing-addon-filter", "{{edit:furigana:Front}}|{{text:edit:Front}}", "{{python:Front}}", ["<b>猫[ねこ]</b>", "", ""], False),
        ("ruby-html", "{{furigana:Front}}", "{{kana:Front}}|{{kanji:Front}}", ["<b>日本語[にほんご]</b>を 学[まな]ぶ", "", ""], False),
        ("sound", "{{Front}}", "{{FrontSide}}<hr>{{Back}}", ["[sound:front.mp3]", "[sound:back.mp3]", ""], False),
        ("tts", "{{tts ja_JP voices=Apple_Otoya,Microsoft_Haruka speed=0.8:Front}}[sound:front.mp3]", "{{FrontSide}}|{{tts en_US:Back}}", ["<b>猫 &amp; 犬</b><br>次", "dog", ""], False),
        ("tts-block", "[anki:tts lang=ja_JP speed=1.2]{{Front}}、{{Back}}[/anki:tts]", "{{FrontSide}}", ["猫", "犬", ""], False),
        ("tts-cloze", "{{cloze:Front}}{{tts ja_JP:cloze:Front}}", "{{cloze:Front}}{{tts ja_JP:cloze-only:Front}}", ["{{c1::猫}}と{{c2::犬}}", "", ""], True),
        ("tts-kana", "{{tts ja_JP:kana:Front}}", "{{text:tts ja_JP:Front}}", ["猫[ねこ]", "", ""], False),
        ("typed", "{{Front}}{{type:Back}}", "{{FrontSide}}<hr>{{type:Back}}", ["山", "mountain", ""], False),
        ("typed-nc", "{{Front}}{{type:nc:Back}}", "{{FrontSide}}", ["accent", "élite", ""], False),
        ("special", "{{Front}}|{{Tags}}|{{Type}}|{{Deck}}|{{Subdeck}}|{{Card}}|{{CardFlag}}", "{{Back}}", ["cat", "猫", ""], False),
        ("cloze", "{{cloze:Front}}", "{{cloze:Front}}", ["{{c1::猫}}と{{c4::犬::動物}}、また{{c1::猫}}", "", ""], True),
        ("nested", "{{cloze:Front}}", "{{cloze:Front}}", ["{{c1::外{{c2::内}}}}", "", ""], True),
        ("cloze-context", "{{cloze:Front}}{{#c4}}four{{/c4}}{{^c4}}other{{/c4}}", "{{cloze:Front}}|{{cloze-only:Front}}", ["{{c1::猫}}と{{c4::犬}}", "", ""], True),
        ("cloze-typed", "{{cloze:Front}}{{type:cloze:Front}}", "{{cloze:Front}}{{type:cloze:Front}}", ["{{c1::猫}}と{{c1::犬}}", "", ""], True),
        ("cloze-text", "{{text:cloze:Front}}", "{{text:cloze:Front}}", ["<b>{{c1::猫}}</b>と{{c4::犬}}", "", ""], True),
        ("cloze-spaces", "{{ cloze:Front }}", "{{ cloze:Front }}", ["{{c1::猫}}と{{c4::犬}}", "", ""], True),
        ("script", '<div id="word">{{Front}}</div><script>document.getElementById("word").dataset.ready="yes"</script>', "{{FrontSide}}", ["猫", "cat", ""], False),
    ]
    for label, body in (
        ("html", "<b>a</b>&amp;b<br>c<div>d</div><p>e</p>"),
        ("spaces", " a\n b "), ("entities", "&nbsp;&#160;&lt;x&gt;"),
        ("breaks", "x<br />y<BR>z"), ("divs", "<div>x</div><div>y</div>"),
        ("comment", "<span>x</span><!-- a>b -->y"), ("less-than", "x<y"),
        ("literal-space", "x\u00a0y"), ("commands", r"\input{private}"),
    ):
        cases.append(("latex-" + label, "{{Front}}", "{{FrontSide}}", [f"[LaTeX]{body}[/LaTeX]", "", ""], False))
    for label, body in (
        ("blocks", "a<br>b<div>c</div><p>d</p>"), ("breaks", "a<br />b<BR>c"),
        ("scripts", "<script>x</script><style>y</style>z"), ("comment", "a<!-- hi -->b"),
        ("spaces", "a\n b\t c"), ("entities", "a&nbsp;&#160;\u00a0b"),
        ("ruby", "<ruby>猫<rt>ねこ</rt></ruby>"), ("empty", ""),
    ):
        cases.append(("tts-" + label, "{{Back}}{{tts ja_JP:Front}}", "{{FrontSide}}", [body, "Listen", ""], False))
    for label, value in (
        ("div", "<div></div>"), ("image", '<img src="cat.png">'), ("comment", "<!-- cat -->"),
        ("script", "<script>cat</script>"), ("style", "<style>cat</style>"),
        ("audio", '<audio src="cat.mp3"></audio>'), ("unicode-space", "\u00a0"), ("whitespace", " \t\n"),
        ("paragraph", "<p></p>"), ("span", "<span></span>"), ("rule", "<hr>"), ("img-empty", "<img>"),
        ("audio-empty", "<audio></audio>"), ("comment-empty", "<!-- -->"),
        ("attributes", '<div title="cat"></div>'), ("div-space", "<div> </div>"),
        ("div-nbsp", "<div>&nbsp;</div>"), ("svg-empty", "<svg></svg>"),
        ("br-uppercase", "<BR>"), ("div-uppercase", "<DIV></DIV>"),
        ("br-slash", "<br />"), ("br-attribute", '<br class="empty">'),
    ):
        cases.append(("condition-" + label, "{{#Optional}}yes{{/Optional}}{{^Optional}}no{{/Optional}}{{Front}}",
            "{{Back}}", ["猫", "cat", value], False))
    generation_cases = [
        ("inverted-present", "{{^Optional}}{{Back}}{{/Optional}}", ["猫", "cat", "present"]),
        ("inverted-absent", "{{^Optional}}{{Back}}{{/Optional}}", ["猫", "cat", ""]),
        ("nested-inverted", "{{#Back}}{{^Optional}}{{Back}}{{/Optional}}{{/Back}}", ["猫", "cat", "present"]),
        ("conditional-literal", "{{#Optional}}fixed{{/Optional}}", ["猫", "cat", "present"]),
        ("and-absent", "{{#Back}}{{#Optional}}{{Back}}{{/Optional}}{{/Back}}", ["猫", "cat", ""]),
        ("and-present", "{{#Back}}{{#Optional}}{{Back}}{{/Optional}}{{/Back}}", ["猫", "cat", "present"]),
        ("alternative-absent", "{{#Optional}}{{Back}}{{/Optional}}{{^Optional}}{{Front}}{{/Optional}}", ["猫", "cat", ""]),
        ("alternative-present", "{{#Optional}}{{Back}}{{/Optional}}{{^Optional}}{{Front}}{{/Optional}}", ["猫", "cat", "present"]),
    ]
    for label, question, fields in generation_cases:
        cases.append(("generation-" + label, question, "{{Back}}", fields, False))
    return cases


def populate(col, deck, prefix="template"):
    from study_corpus import add_type

    notes = []
    for name, question, answer, fields, cloze in template_cases():
        templates = [("Recognition", question, answer, None)]
        if name.startswith("generation-"):
            templates = [("Recognition", "{{Front}}", "{{Back}}", None),
                ("Conditional", question, answer, None)]
        if name.startswith("condition-") or name in ("html-empty", "nbsp-empty"):
            templates.append(("Conditional", "{{#Optional}}{{Optional}}{{/Optional}}", "{{Back}}", None))
        model = add_type(col, prefix + " " + name, ["Front", "Back", "Optional"],
            templates, cloze=cloze, css=".card { color: #123456; }")
        if name == "latex-svg":
            model["latexsvg"] = True
            col.models.save(model)
        note = col.new_note(model)
        note.fields = fields
        note.tags = ["template_corpus", "case::" + name]
        col.add_note(note, deck)
        notes.append(note.id)
        for card in note.cards():
            card.flags = 2
            col.update_card(card)
            rendered = col._backend.render_existing_card(card_id=card.id, browser=False, partial_render=False)
            for nodes in (rendered.question_nodes, rendered.answer_nodes):
                extracted = col._backend.extract_latex(text="".join(node.text for node in nodes),
                    svg=rendered.latex_svg, expand_clozes=False)
                for latex in extracted.latex:
                    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>' if rendered.latex_svg else (REPO / "test/fixtures/files/avatar.png").read_bytes()
                    col.media.write_data(latex.filename, data)
    return notes


def build(root):
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    from anki.latex import Config
    from study_corpus import snapshot

    with tempfile.TemporaryDirectory(prefix="collection-", dir=root) as scratch:
        col = Collection(str(Path(scratch) / "collection.anki2"))
        try:
            col.set_config_bool(Config.Bool.RENDER_LATEX, False)
            deck = col.decks.id("Template corpus::Selected")
            populate(col, deck)
            expected = snapshot(col)
            for source in expected["cards"]:
                card = col.get_card(source["id"])
                source["sounds"] = {side: [tag.filename if hasattr(tag, "filename") else asdict(tag) for tag in tags]
                    for side, tags in (("q", card.question_av_tags()), ("a", card.answer_av_tags()))}
            save_json(root / "expected.json", {"anki_version": version,
                "unsupported_filters": {"case::missing-addon-filter": ["edit", "text", "python"]}, **expected})
            col.export_anki_package(out_path=str(root / "template-corpus.apkg"),
                options=ExportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True, legacy=True), limit=None)
        finally:
            col.close()


if __name__ == "__main__":
    require_anki()
    root = Path(tempfile.mkdtemp(prefix="anki-templates-", dir=REPO / "tmp"))
    print(f"Artifacts: {root}", flush=True)
    build(root)
    probe = subprocess.run(["bin/test", "unit", "test/models/study_rendering_corpus_test.rb"], cwd=REPO,
        env={**os.environ, "ANKI_TEMPLATE_CORPUS": str(root)}, capture_output=True, text=True)
    (root / "rails.log").write_text(probe.stdout + probe.stderr)
    print(probe.stdout + probe.stderr)
    sys.exit(probe.returncode)
