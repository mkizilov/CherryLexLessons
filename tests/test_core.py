import csv
import fcntl
import hashlib
from pathlib import Path
import tempfile
import unittest

from lesson_factory import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_locale_sources_and_review_roles(self):
        self.assertEqual(len(core.LOCALES), 12)
        for study in core.LOCALES:
            self.assertEqual(len(core.learner_locales(study)), 11)
            self.assertEqual(len(core.translation_locales(study)), 10)
            self.assertNotIn(study, core.learner_locales(study))
            self.assertNotIn(core.source_locale(study), core.translation_locales(study))
            self.assertEqual(len(core.review_roles(study)), 13)
        self.assertEqual(core.source_locale("English"), "es")
        self.assertEqual(core.language_code("German"), "de")
        self.assertEqual(core.language_code("Mandarin Chinese"), "zh")
        for value in (None, 123, "unknown"):
            with self.assertRaises(core.AuthoringError):
                core.language_code(value)

    def test_stage_identity_and_word_ranges(self):
        for base, stage in ((True, 0), (20, True), (20, False), (20, -1), (20, 5), (20, 1.0), (20, "1"), (0, 0), (21, 0), (20.0, 0), (1000000, 0)):
            with self.subTest(base=base, stage=stage), self.assertRaises(core.AuthoringError):
                core.stage_identity(base, stage)
        self.assertEqual([core.word_range(core.stage_identity(20, stage)) for stage in range(5)], [[1, 20], [21, 45], [46, 95], [96, 145], [146, 245]])
        self.assertEqual(core.word_range(core.stage_identity(30)), [21, 30])
        self.assertEqual(core.RETURN_OFFSETS, (25, 75, 125, 225))

    def test_event_ties_use_base_then_stage(self):
        events = [core.stage_identity(base, stage) for base, stage in ((120, 1), (20, 3), (70, 2))]
        self.assertEqual([core.event_key(item) for item in sorted(events, key=core.event_key)], [(145, 20, 3), (145, 70, 2), (145, 120, 1)])

    def test_safe_paths_reject_traversal_and_symlink(self):
        for relative in ("../outside", "/absolute", "family_runs/de/../../outside"):
            with self.assertRaises(core.AuthoringError):
                core.safe_path(self.root, relative)
        (self.root / "link").symlink_to(self.root)
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            core.safe_path(self.root, "link/file")
        self.assertEqual(core.safe_path(self.root, "new/file"), self.root.resolve() / "new/file")

    def test_json_and_sha256(self):
        path = self.root / "nested/value.json"
        payload = {"z": "é", "a": [1, 2]}
        core.write_json(path, payload)
        self.assertEqual(core.read_json(path), payload)
        self.assertEqual(core.digest(path), hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(core.canonical(payload), core.canonical({"a": [1, 2], "z": "é"}))
        path.write_text("invalid")
        with self.assertRaises(core.AuthoringError):
            core.read_json(path)

    def test_vocabulary_requires_exact_contiguous_order(self):
        folder = self.root / "reference/vocabulary/German"
        folder.mkdir(parents=True)
        path = folder / "german_vocabulary.csv"
        for ranks in ([1, 2], [2, 1], [1, 1], [1, 3], ["bad"]):
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Rank", "Word"])
                writer.writerows((rank, f"wort{rank}") for rank in ranks)
            if ranks == [1, 2]:
                self.assertEqual(len(core.vocabulary(self.root, "de")), 2)
            else:
                with self.assertRaises(core.AuthoringError):
                    core.vocabulary(self.root, "de")
        path.write_text("Word\nwort1\n")
        with self.assertRaisesRegex(core.AuthoringError, "rank column"):
            core.vocabulary(self.root, "de")

    def test_mutation_lock_excludes_concurrent_owner_and_symlink(self):
        path = self.root / ".authoring.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.AuthoringError, "locked"):
                with core.mutation_lock(self.root):
                    self.fail("Concurrent mutation admitted")
        with core.mutation_lock(self.root):
            self.assertTrue(path.read_text().isdigit())
        path.unlink()
        target = self.root / "evidence"
        target.write_text("untouched")
        path.symlink_to(target)
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            with core.mutation_lock(self.root):
                self.fail("Symlink lock admitted")
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
