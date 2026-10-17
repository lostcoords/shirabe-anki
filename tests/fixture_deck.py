#!/usr/bin/env python3
"""Build a disposable real-Anki collection and APKG for cross-client review."""

from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import wave

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DECK = "Shirabe Phase 0"
PROBE = "_shirabe_phase0_probe_v1.js"
PAYLOAD = "_shirabe_phase0_snapshot_v1.json"
IMAGE = "shirabe_phase0_pixel.png"
AUDIO = "shirabe_phase0_silence.wav"


def require_anki() -> None:
    try:
        import anki.collection
    except ImportError:
        from check_against_anki import anki_python

        python = anki_python()
        if not python or Path(sys.executable).absolute() == python.absolute():
            raise SystemExit("Run with an installed Anki Python; no mock fallback is available.")
        os.execv(str(python), [str(python), str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])


def output_directory(path: Path | None) -> Path:
    if path:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=False)
        return path
    scratch = REPO / "tmp"
    scratch.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="anki-phase0-", dir=scratch))


def save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def probe_block() -> str:
    return """
<details data-phase0-controls data-phase0-snapshot="_shirabe_phase0_snapshot_v1.json" data-phase0-invalid="_shirabe_phase0_invalid_v1.json">
  <summary>Phase 0 client probes</summary>
  <button type="button" onclick="event.stopPropagation();window.ShirabePhase0.run()">Run platform probes</button>
  <button type="button" onclick="event.stopPropagation();window.ShirabePhase0.search()">Native search for fixture notes</button>
  <pre data-phase0-result>Probes have not run.</pre>
</details>
<script>
(function () {
  if (window.ShirabePhase0) { window.ShirabePhase0.attach(); return; }
  var script = document.createElement('script');
  script.src = '_shirabe_phase0_probe_v1.js';
  script.onload = function () { window.ShirabePhase0.attach(); };
  script.onerror = function () {
    document.querySelectorAll('[data-phase0-result]').forEach(function (el) {
      el.textContent = 'Local probe script is missing or could not load.';
    });
  };
  document.head.appendChild(script);
})();
</script>
"""


def create_fixture(root: Path, template_block: str = "", extra_media: tuple[Path, ...] = (), *, portable: bool = False, api_base: str = "https://shirabe.org", online_parsing: bool = False, web: Path | None = None, prepared: bool = False) -> dict:
    from anki.buildinfo import version
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions

    source = root / "source"
    source.mkdir()
    collection_path = source / "collection.anki2"
    col = Collection(str(collection_path))
    try:
        deck_id = col.decks.id(DECK)
        assets = {
            PROBE: (HERE / "phase0_probe.js").read_bytes(),
            PAYLOAD: json.dumps({"format": 1, "fixture": "日本語", "transport": "JSON data, not executable code"}, ensure_ascii=False).encode(),
            "_shirabe_phase0_invalid_v1.json": b'{"format":',
            IMAGE: base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="),
        }
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 800)
        assets[AUDIO] = audio.getvalue()
        for path in extra_media:
            if path.name in assets:
                raise ValueError(f"Extra media conflicts with fixture asset: {path.name}")
            assets[path.name] = path.read_bytes()
        for name, data in assets.items():
            actual = col.media.write_data(name, data)
            if actual != name:
                raise ValueError(f"Anki changed fixture filename {name!r} to {actual!r}")

        portable_assets = {}
        snapshot_name = None
        snapshot_reference = None
        if portable:
            import support
            from shirabe_addon import assets as addon_assets, template as addon_template

            portable_assets = addon_assets.install(col.media, web or addon_assets.WEB)
        if prepared:
            from offline_fixture import prepared as prepare_fixture
            from shirabe_addon.offline import persist_snapshot, snapshot_field

            snapshot_name = persist_snapshot(col.media, prepare_fixture()).filename
            snapshot_reference = snapshot_field(snapshot_name)

        styles = """.card { font-family: sans-serif; font-size: 24px; text-align: left; }
rt { font-size: 0.6em; } .phase0-label { font-size: 14px; color: #666; }
[data-phase0-controls] { margin-top: 24px; font-size: 12px; }
[data-phase0-result] { white-space: pre-wrap; overflow-wrap: anywhere; }
[data-phase0-controls] button, [data-phase0-controls] summary { cursor: pointer; }
[data-phase0-controls] button:hover, [data-phase0-controls] summary:hover { background: #ddd; }
"""
        models = {}
        kinds = [("basic", "Basic"), ("reverse", "Basic (and reversed card)"), ("cloze", "Cloze"), ("custom", "Basic")]
        if prepared:
            kinds.append(("prepared", "Basic"))
        for key, stock in kinds:
            model = copy.deepcopy(col.models.by_name(stock))
            model["id"] = 0
            model["name"] = f"Shirabe Phase 0 {key}"
            model["css"] = styles
            col.models.add_field(model, col.models.new_field("FixtureKey"))
            if key == "custom":
                model["tmpls"][0]["qfmt"] = '<article class="imported-layout"><header>独自のカード</header><main><blockquote>{{Front}}</blockquote></main></article>'
                model["tmpls"][0]["afmt"] = '{{FrontSide}}<hr id="answer"><aside data-user-layout="preserve">{{Back}}</aside>'
            if key == "prepared":
                col.models.add_field(model, col.models.new_field("ShirabeLookup"))
                model["tmpls"][0]["qfmt"] = '<div>{{Front}}</div>'
                model["tmpls"][0]["afmt"] = '{{FrontSide}}<hr id="answer"><div>{{Back}}</div>'
            for template in model["tmpls"]:
                for side in ("qfmt", "afmt"):
                    template[side] = (
                        '<div class="phase0-label" data-phase0-case="{{FixtureKey}}">{{FixtureKey}}</div>\n'
                        + template[side] + probe_block() + template_block
                    )
                if portable:
                    template["qfmt"] = addon_template.with_question_guard(template["qfmt"])
                    template["afmt"] = addon_template.with_block(template["afmt"], api_base, addon_template.CLIENT_ID, "click", online_parsing=online_parsing, assets=portable_assets, snapshot_field=key == "prepared")
            added = col.models.add_dict(model)
            models[key] = col.models.get(added.id)

        cases = [
            ("basic", "basic", "猫がいます。", "A cat is here. ANSWER_ONLY_BASIC_7F3A"),
            ("ruby-inline", "basic", '<ruby>日本語<rt>にほんご</rt></ruby>を<strong>勉<span>強</span></strong>します。<span hidden>HIDDEN_INLINE_7F3A</span>', "I study Japanese. ANSWER_ONLY_RUBY_7F3A"),
            ("reverse", "reverse", "食べる", "to eat"),
            ("cloze", "cloze", "私は{{c1::寿司::食べ物}}を{{c2::食べました}}。", "CLOZE_EXTRA_ONLY_7F3A"),
            ("custom-layout", "custom", '<span lang="ja">𠮷田さんは<em>学校</em>へ行きます。</span><details><summary>補足</summary>COLLAPSED_DETAIL_7F3A</details>', '<div><ruby>学校<rt>がっこう</rt></ruby> means school.</div>'),
            ("long-sentence", "basic", "昨日、図書館で日本語の本を読みました。" * 64, "Long fixture answer. ANSWER_ONLY_LONG_7F3A"),
            ("media", "basic", f'<img src="{IMAGE}" alt="Fixture pixel">音を聞いてください。[sound:{AUDIO}]', "Local image and a 100 ms silent WAV. ANSWER_ONLY_MEDIA_7F3A"),
        ]
        if prepared:
            cases.insert(0, ("prepared", "prepared", "😀猫は猫", "犬"))
        notes = []
        rendered = root / "rendered"
        rendered.mkdir()
        for key, kind, front, back in cases:
            note = col.new_note(models[kind])
            note.fields = [front, back, key, *([snapshot_reference] if kind == "prepared" else [])]
            note.tags = ["shirabe_phase0", f"phase0::{key}", "user_tag_preserve"]
            note_deck = col.decks.id(DECK + "::Prepared") if key == "prepared" else deck_id
            col.add_note(note, note_deck)
            cards = []
            for card in note.cards():
                for side, html in (("question", card.question()), ("answer", card.answer())):
                    (rendered / f"{key}-{card.ord}-{side}.html").write_text(html)
                cards.append({"id": card.id, "ord": card.ord})
            notes.append({"case": key, "id": note.id, "guid": note.guid, "notetype": note.mid, "deck": note_deck, "fields": note.fields, "tags": note.tags, "cards": cards})
        apkg = root / "shirabe-phase0.apkg"
        exported = col.export_anki_package(
            out_path=str(apkg),
            options=ExportAnkiPackageOptions(with_scheduling=False, with_deck_configs=False, with_media=True, legacy=True),
            limit=DeckIdLimit(deck_id),
        )
        manifest = {
            "anki": version,
            "collection": str(collection_path),
            "apkg": str(apkg),
            "deck": {"id": deck_id, "name": DECK},
            "exported_count": exported,
            "notes": notes,
            "media": sorted([*assets, *portable_assets.values(), *([snapshot_name] if snapshot_name else [])]),
            "snapshot": snapshot_name,
            "portable_assets": portable_assets,
            "portable_settings": {"api_base": api_base, "trigger": "click", "online_parsing": online_parsing} if portable else None,
            "ui_validation": "not run by this builder",
        }
        save_json(root / "manifest.json", manifest)
        return manifest
    finally:
        col.close()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New directory only; defaults to ignored repo tmp.")
    parser.add_argument("--template-block", type=Path, help="Append this HTML to each template side.")
    parser.add_argument("--media", action="append", type=Path, default=[], help="Extra collection media; repeat as needed.")
    parser.add_argument("--portable", action="store_true", help="Install the real add-on assets, question guard and answer-side local loader.")
    parser.add_argument("--api-base", default="https://shirabe.org")
    parser.add_argument("--online-parsing", action="store_true", help="Explicitly opt the fixture into whole-card online parsing.")
    parser.add_argument("--web", type=Path, help="Directory with shirabe-card.js and attach.js for assets.install.")
    parser.add_argument("--prepared", action="store_true", help="Add the shared Python offline fixture using the real snapshot writer; requires --portable.")
    parser.add_argument("--study-sizes", type=int, nargs="*", default=[10_000, 100_000], help="Study corpus reconciliation sizes; pass without values to skip benchmarks.")
    args = parser.parse_args()
    if args.prepared and not args.portable:
        parser.error("--prepared requires --portable")
    return args


def build_from_arguments(root: Path, args: argparse.Namespace) -> dict:
    return create_fixture(root, args.template_block.read_text() if args.template_block else "", tuple(args.media), portable=args.portable, api_base=args.api_base, online_parsing=args.online_parsing, web=args.web, prepared=args.prepared)


if __name__ == "__main__":
    require_anki()
    args = arguments()
    root = output_directory(args.output)
    manifest = build_from_arguments(root, args)
    print(json.dumps({"root": str(root), "apkg": manifest["apkg"], "notes": len(manifest["notes"]), "cards": sum(len(n["cards"]) for n in manifest["notes"])}, indent=2))
