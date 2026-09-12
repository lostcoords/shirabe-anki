from copy import deepcopy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support


@unittest.skipUnless(importlib.util.find_spec('anki'), "Run with Anki's bundled Python")
class NativeTemplateInstallation(unittest.TestCase):
    def setUp(self):
        from anki.collection import Collection
        from shirabe_addon import template_ui
        self.ui = template_ui
        self.temp = tempfile.TemporaryDirectory(prefix='shirabe-template-')
        self.col = Collection(str(Path(self.temp.name) / 'collection.anki2'))
        self.model = self.col.models.by_name('Basic (and reversed card)')
        self.model['tmpls'][0]['qfmt'] = '<b>{{Front}}</b>\n\n'
        self.model['tmpls'][0]['afmt'] = '{{FrontSide}}<hr id=answer><div>{{Back}}</div>\n\n'
        self.col.models.update_dict(self.model)
        self.model = self.col.models.get(self.model['id'])
        note = self.col.new_note(self.model)
        note['Front'], note['Back'] = '猫', '<b>ねこ</b>'
        deck = self.col.decks.id('My existing Japanese deck')
        self.col.add_note(note, deck)
        self.note_id = note.id

    def tearDown(self):
        self.col.close()
        self.temp.cleanup()

    def target(self, model=None):
        return self.ui.TemplateTarget.capture(self.col, model or self.col.models.get(self.model['id']))

    def test_install_is_local_answer_only_and_one_undo_restores_model_and_preferences(self):
        from shirabe_addon import template
        before = deepcopy(self.model)
        cards = self.col.db.all('select * from cards where nid = ? order by id', self.note_id)
        self.ui.apply_install(self.col, self.target(), self.ui.TemplateOptions(excluded_fields=('Back',)), {})
        after = self.col.models.get(self.model['id'])
        self.assertEqual(self.model, before, 'Installation must not mutate a cached model reference')
        self.assertEqual(self.col.db.all('select * from cards where nid = ? order by id', self.note_id), cards)
        self.assertEqual(self.col.get_note(self.note_id)['Back'], '<b>ねこ</b>')
        for card in after['tmpls']:
            self.assertFalse(template.installed(card['qfmt']))
            self.assertIn(template.QUESTION_OPEN, card['qfmt'])
            self.assertTrue(template.installed(card['afmt']))
            self.assertIn('_shirabe_', card['afmt'])
            self.assertNotIn('{{text:ShirabeLookup}}', card['afmt'])
        record = self.col.get_config(self.ui.settings_key(self.model['id']))
        self.assertEqual(record['excludedFields'], ['Back'])
        self.assertFalse(record['onlineParsing'])
        self.assertEqual(self.col.undo_status().undo, 'Shirabe: install phone lookup')
        self.col.undo()
        self.assertEqual(self.col.models.get(self.model['id'])['tmpls'], before['tmpls'])
        self.assertIsNone(self.col.get_config(self.ui.settings_key(self.model['id'])))
        self.col.redo()
        self.assertEqual(self.col.models.get(self.model['id'])['tmpls'], after['tmpls'])
        self.assertEqual(self.col.get_config(self.ui.settings_key(self.model['id'])), record)

    def test_stale_template_and_permission_changes_refuse_all_writes(self):
        for changed in ('template', 'preferences'):
            with self.subTest(changed=changed):
                target = self.target()
                if changed == 'template':
                    edited = deepcopy(self.col.models.get(self.model['id']))
                    edited['tmpls'][0]['afmt'] += '<p>my new design</p>'
                    self.col.models.update_dict(edited)
                else:
                    self.col.set_config(self.ui.settings_key(self.model['id']), {'onlineParsing': True})
                before = deepcopy(self.col.models.get(self.model['id']))
                undo = self.col.undo_status()
                with self.assertRaisesRegex(self.ui.TemplateError, 'changed'):
                    self.ui.apply_install(self.col, target, self.ui.TemplateOptions(), {})
                self.assertEqual(self.col.models.get(self.model['id']), before)
                self.assertEqual(self.col.undo_status(), undo)

    def test_remove_only_selected_note_type_restores_exact_source_and_can_be_undone(self):
        originals = {model['id']: deepcopy(model) for model in (self.model, self.col.models.by_name('Basic'))}
        for model in originals.values():
            self.ui.apply_install(self.col, self.target(model), self.ui.TemplateOptions(excluded_fields=('Back',)), {})
        first = self.target()
        other_id = next(mid for mid in originals if mid != first.model['id'])
        other = deepcopy(self.col.models.get(other_id))
        self.ui.apply_remove(self.col, (first,))
        self.assertEqual(self.col.models.get(first.model['id'])['tmpls'], originals[first.model['id']]['tmpls'])
        self.assertEqual(self.col.models.get(other_id), other)
        self.assertEqual(self.col.undo_status().undo, 'Shirabe: remove phone lookup')
        self.col.undo()
        self.assertEqual(self.col.models.get(first.model['id'])['tmpls'], first.model['tmpls'])
        self.assertEqual(self.col.get_config(self.ui.settings_key(first.model['id'])), first.settings)

    def test_stale_removal_target_does_not_partially_remove_other_targets(self):
        models = (self.model, self.col.models.by_name('Basic'))
        for model in models:
            self.ui.apply_install(self.col, self.target(model), self.ui.TemplateOptions(), {})
        targets = tuple(self.target(self.col.models.get(m['id'])) for m in models)
        edited = deepcopy(targets[1].model)
        edited['tmpls'][0]['qfmt'] += 'edited after confirmation'
        self.col.models.update_dict(edited)
        with self.assertRaisesRegex(self.ui.TemplateError, 'changed'):
            self.ui.apply_remove(self.col, targets)
        self.assertEqual(self.col.models.get(targets[0].model['id']), targets[0].model)

    def test_removal_preserves_template_edits_made_since_installation(self):
        self.ui.apply_install(self.col, self.target(), self.ui.TemplateOptions(excluded_fields=('Back',)), {})
        edited = deepcopy(self.col.models.get(self.model['id']))
        edited['tmpls'][0]['afmt'] = edited['tmpls'][0]['afmt'].replace('<hr id=answer>', '<hr class="my-answer">')
        self.col.models.update_dict(edited)
        self.ui.apply_remove(self.col, (self.target(),))
        source = self.col.models.get(self.model['id'])['tmpls'][0]['afmt']
        self.assertIn('<hr class="my-answer">', source)
        self.assertNotIn('data-shirabe-ignore', source)
        self.assertNotIn('shirabe:start', source)

    def test_reinstall_changes_sides_and_exclusions_and_remove_returns_original(self):
        from shirabe_addon import template
        before = deepcopy(self.model)
        self.ui.apply_install(self.col, self.target(), self.ui.TemplateOptions(answer_only=False, online_parsing=True, excluded_fields=('Front',)), {})
        both = self.col.models.get(self.model['id'])
        self.assertTrue(template.installed(both['tmpls'][0]['qfmt']))
        self.assertIn('"onlineParsing": true', both['tmpls'][0]['afmt'])
        self.ui.apply_install(self.col, self.target(), self.ui.TemplateOptions(excluded_fields=('Back',)), {})
        final = self.col.models.get(self.model['id'])
        self.assertNotIn('data-shirabe-ignore', final['tmpls'][0]['qfmt'])
        self.assertFalse(template.installed(final['tmpls'][0]['qfmt']))
        self.assertEqual(final['tmpls'][0]['afmt'].count('data-shirabe-ignore'), 1)
        self.ui.apply_remove(self.col, (self.target(),))
        self.assertEqual(self.col.models.get(self.model['id'])['tmpls'], before['tmpls'])

    def test_unsafe_substitution_refuses_before_media_or_model_write(self):
        edited = deepcopy(self.model)
        edited['tmpls'][0]['afmt'] += '<script>var hidden = "{{Back}}";</script>'
        self.col.models.update_dict(edited)
        target = self.target()
        undo = self.col.undo_status()
        media = set(Path(self.col.media.dir()).iterdir())
        with self.assertRaisesRegex(self.ui.TemplateError, 'Back'):
            self.ui.apply_install(self.col, target, self.ui.TemplateOptions(excluded_fields=('Back',)), {})
        self.assertEqual(self.col.models.get(self.model['id']), target.model)
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(set(Path(self.col.media.dir()).iterdir()), media)

    def test_failed_preferences_write_rolls_back_install_without_losing_prior_undo(self):
        target = self.target()
        undo_before = self.col.undo_status().undo
        with patch.object(self.col, 'set_config', side_effect=RuntimeError('disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                self.ui.apply_install(self.col, target, self.ui.TemplateOptions(), {})
        self.assertEqual(self.col.models.get(self.model['id'])['tmpls'], target.model['tmpls'])
        self.assertIsNone(self.col.get_config(self.ui.settings_key(self.model['id'])))
        self.assertEqual(self.col.undo_status().undo, undo_before)

    def test_failed_second_removal_restores_first_template_and_both_preferences(self):
        models = (self.model, self.col.models.by_name('Basic'))
        for model in models:
            self.ui.apply_install(self.col, self.target(model), self.ui.TemplateOptions(), {})
        targets = tuple(self.target(self.col.models.get(model['id'])) for model in models)
        update = self.col.models.update_dict

        def fail_second(model):
            if model['id'] == targets[1].model['id']:
                raise RuntimeError('second update failed')
            return update(model)

        with patch.object(self.col.models, 'update_dict', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'second update failed'):
                self.ui.apply_remove(self.col, targets)
        for target in targets:
            self.assertEqual(self.col.models.get(target.model['id'])['tmpls'], target.model['tmpls'])
            self.assertEqual(self.col.get_config(self.ui.settings_key(target.model['id'])), target.settings)

    def test_offline_refresh_preserves_options_and_removal_baseline_in_its_undo_group(self):
        from shirabe_addon import assets, template
        original = deepcopy(self.model)
        options = self.ui.TemplateOptions(False, True, ('Back',))
        self.ui.apply_install(self.col, self.target(), options, {})
        installed = self.target()
        undo = self.col.add_custom_undo_entry('Prepare Shirabe offline lookup')
        model = deepcopy(installed.model)
        self.col.models.add_field(model, self.col.models.new_field('ShirabeLookup'))
        self.ui.refresh_installation(self.col, model, {'answerOnly': True, 'onlineParsing': False}, assets.install(self.col.media))
        self.col.merge_undo_entries(undo)
        refreshed = self.target()
        self.assertEqual(self.ui.TemplateOptions.from_settings(refreshed.settings), options)
        for card in refreshed.model['tmpls']:
            self.assertTrue(template.installed(card['qfmt']))
            self.assertIn('{{text:ShirabeLookup}}', card['afmt'])
        self.assertEqual([row['before'] for row in refreshed.settings['templates']],
                         [row['before'] for row in installed.settings['templates']])
        self.col.undo()
        self.assertEqual(self.col.models.get(model['id'])['tmpls'], installed.model['tmpls'])
        self.assertEqual(self.col.get_config(self.ui.settings_key(model['id'])), installed.settings)
        self.col.redo()
        self.ui.apply_remove(self.col, (self.target(),))
        self.assertEqual(self.col.models.get(model['id'])['tmpls'], original['tmpls'])

    def test_native_cloze_rendering_and_rich_field_content_are_preserved(self):
        from shirabe_addon import template
        model = deepcopy(self.col.models.by_name('Cloze'))
        model['tmpls'][0]['qfmt'] = '{{cloze:Text}}'
        model['tmpls'][0]['afmt'] = '{{cloze:Text}}'
        self.col.models.update_dict(model)
        note = self.col.new_note(model)
        note['Text'] = '<b>{{c1::猫}}</b>と{{c2::犬}}'
        self.col.add_note(note, 1)
        before = [card.render_output() for card in note.cards()]
        self.ui.apply_install(self.col, self.target(self.col.models.get(model['id'])),
                              self.ui.TemplateOptions(excluded_fields=('Text',)), {})
        after = [card.render_output() for card in self.col.get_note(note.id).cards()]
        for original, wrapped in zip(before, after):
            self.assertEqual(template.without_block(wrapped.question_text).strip(),
                             self.ui.IGNORE_OPEN + original.question_text + '</span>')
            self.assertEqual(template.without_block(wrapped.answer_text).strip(),
                             self.ui.IGNORE_OPEN + original.answer_text + '</span>')
        self.assertEqual(self.col.get_note(note.id)['Text'], note['Text'])


if __name__ == '__main__':
    unittest.main()
