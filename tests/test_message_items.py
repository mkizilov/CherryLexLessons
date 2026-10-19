"""Keep independent provider message items separate; never merge conflicting families."""
import copy
import json
import unittest

from lesson_factory.translation import TranslationError, parse_candidate


def family():
    return {'stages': [{'stage': n, 'lesson_md': 'lesson', 'quiz_md': 'quiz', 'adaptation_notes': []} for n in range(5)]}


def message(value):
    return {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': value}]}


class MessageItemTests(unittest.TestCase):
    def parse(self, *items, **fields):
        return parse_candidate({'status': 'completed', 'output': list(items), **fields})

    def test_empty_skeleton_then_complete_or_identical_duplicate(self):
        skeleton = {'stages': [{'stage': n, 'lesson_md': '', 'quiz_md': '', 'adaptation_notes': []} for n in range(5)]}
        for items in ([message(json.dumps(skeleton)), message(json.dumps(family()))],
                      [message(' '), message(json.dumps(family()))],
                      [message(json.dumps(family())), message(json.dumps(family()))]):
            self.assertEqual(self.parse(*items), family())
        with self.assertRaises(TranslationError):
            self.parse(message(json.dumps(skeleton)))

    def test_distinct_complete_items_require_review(self):
        other = copy.deepcopy(family())
        other['stages'][4]['quiz_md'] = 'other quiz'
        with self.assertRaisesRegex(TranslationError, 'ambiguous'):
            self.parse(message(json.dumps(family())), message(json.dumps(other)))

    def test_only_same_message_content_parts_are_joined(self):
        text = json.dumps(family())
        parts = [{'type': 'output_text', 'text': text[:80]}, {'type': 'output_text', 'text': text[80:]}]
        self.assertEqual(self.parse({'type': 'message', 'content': parts}), family())
        with self.assertRaises(TranslationError):
            self.parse(message(text[:80]), message(text[80:]))

    def test_failed_terminal_and_malformed_other_items_are_not_accepted(self):
        for status in ('failed', 'incomplete'):
            with self.assertRaises(TranslationError):
                self.parse(message(json.dumps(family())), status=status)
        for extra in (message('commentary'), message('{"stages": ['), {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}):
            with self.assertRaises(TranslationError):
                self.parse(extra, message(json.dumps(family())))

    def test_duplicate_json_keys_and_nonempty_skeleton_remain_rejected(self):
        with self.assertRaises(TranslationError):
            self.parse(message('{"stages": [], "stages": ' + json.dumps(family()['stages']) + '}'))
        bad = {'stages': [{'stage': n, 'lesson_md': ' ', 'quiz_md': '', 'adaptation_notes': []} for n in range(5)]}
        with self.assertRaises(TranslationError):
            self.parse(message(json.dumps(bad)), message(json.dumps(family())))
