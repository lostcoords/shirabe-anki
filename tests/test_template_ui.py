import unittest
import os
from pathlib import Path
import subprocess

import support
from check_against_anki import anki_python
from shirabe_addon import template_ui


class FieldExclusions(unittest.TestCase):
    def test_exact_filtered_substitutions_and_conditionals_keep_their_source(self):
        source = ('{{#Text}}<div class="sentence">{{cloze:Text}}</div>{{/Text}}\n'
                  '{{^Text}}empty{{/Text}} {{text:furigana:Text}} {{ Text }} {{Text Extra}} {{text}}')
        result = template_ui.with_excluded_fields(source, ("Text",))
        self.assertEqual(result.count('data-shirabe-ignore'), 3)
        self.assertIn('{{#Text}}<div class="sentence">', result)
        self.assertIn('{{cloze:Text}}</span></div>{{/Text}}', result)
        self.assertIn('{{text:furigana:Text}}</span>', result)
        self.assertIn('{{ Text }}</span>', result)
        self.assertIn('{{Text Extra}} {{text}}', result)
        self.assertIn('style="display:contents"', result)
        self.assertEqual(template_ui.without_excluded_fields(result), source)

    def test_reinstall_and_changing_exclusions_do_not_nest_wrappers(self):
        source = '<p>{{Front}}</p>\n<div>{{Back}}</div>\n\n'
        first = template_ui.with_excluded_fields(source, ("Front",))
        self.assertEqual(template_ui.with_excluded_fields(first, ("Front",)), first)
        changed = template_ui.with_excluded_fields(first, ("Back",))
        self.assertEqual(changed.count('data-shirabe-ignore'), 1)
        self.assertIn('<p>{{Front}}</p>', changed)
        self.assertEqual(template_ui.with_excluded_fields(changed, ()), source)

    def test_user_authored_ignore_markup_is_preserved(self):
        source = '<section data-shirabe-ignore><b>{{Back}}</b></section>'
        wrapped = template_ui.with_excluded_fields(source, ("Back",))
        self.assertEqual(template_ui.without_excluded_fields(wrapped), source)

    def test_attributes_scripts_and_structural_contexts_refuse_unsafe_exclusion(self):
        for source in (
            '<img src="{{Text}}">', '<a title="{{text:Text}}">link</a>',
            '<script>document.body.innerHTML = "{{Text}}";</script>',
            '<style>.card::after {content: "{{Text}}"}</style>',
            '<textarea>{{Text}}</textarea>', '<table>{{Text}}</table>',
            '<table><tr>{{Text}}</tr></table>', '<select>{{Text}}</select>',
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(template_ui.TemplateError, 'Text'):
                    template_ui.with_excluded_fields(source, ("Text",))

    def test_unrelated_dynamic_content_and_comment_substitutions_are_untouched(self):
        source = '<script>var text = "{{Front}}";</script><!-- {{Back}} --><td>{{Back}}</td>'
        result = template_ui.with_excluded_fields(source, ("Back",))
        self.assertEqual(result.count('data-shirabe-ignore'), 1)
        self.assertTrue(result.startswith('<script>var text = "{{Front}}";</script><!-- {{Back}} -->'))
        self.assertEqual(template_ui.without_excluded_fields(result), source)

    def test_native_entities_unicode_and_comparisons_survive_exactly(self):
        source = '<div>1 < 2 &amp; {{日本語}} &lt;b&gt; {{Back}}</div>\n'
        result = template_ui.with_excluded_fields(source, ("日本語", "Back"))
        self.assertEqual(result.count('data-shirabe-ignore'), 2)
        self.assertEqual(template_ui.without_excluded_fields(result), source)

    @unittest.skipUnless(anki_python(), 'Installed Anki required for WebEngine')
    def test_excluded_rich_text_stays_visible_but_never_enters_shared_scanner(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name('template_ui_webengine_probe.py'))],
                                capture_output=True, text=True, timeout=20,
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QTWEBENGINE_CHROMIUM_FLAGS': '--disable-gpu'})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
