import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "install_skill_links.py"
SPEC = importlib.util.spec_from_file_location("install_skill_links", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class InstallSkillLinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.install_root = Path(self.temp_dir.name) / "skills"
        self.install_root.mkdir()
        catalog = json.loads(
            (INSTALLER.ROOT / "catalog/skills.json").read_text(encoding="utf-8")
        )
        self.entries = catalog["skills"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def old_managed_link(self, legacy_name: str) -> Path:
        target = self.install_root / legacy_name
        target.symlink_to(INSTALLER.ROOT / "skills" / legacy_name)
        return target

    def test_links_current_names_then_removes_old_managed_links(self) -> None:
        legacy_targets = [
            self.old_managed_link(legacy_name)
            for entry in self.entries
            for legacy_name in entry.get("renamed_from", [])
        ]

        INSTALLER.link_current_skills(self.install_root, self.entries)
        INSTALLER.remove_managed_legacy_links(self.install_root, self.entries)

        for entry in self.entries:
            current = self.install_root / entry["name"]
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve(), (INSTALLER.ROOT / entry["path"]).resolve())
        for legacy_target in legacy_targets:
            self.assertFalse(os.path.lexists(legacy_target))

    def test_preserves_a_foreign_legacy_link(self) -> None:
        entry = next(item for item in self.entries if item.get("renamed_from"))
        legacy_name = entry["renamed_from"][0]
        foreign_source = Path(self.temp_dir.name) / "foreign-skill"
        foreign_source.mkdir()
        legacy_target = self.install_root / legacy_name
        legacy_target.symlink_to(foreign_source)

        INSTALLER.link_current_skills(self.install_root, self.entries)
        INSTALLER.remove_managed_legacy_links(self.install_root, self.entries)

        self.assertTrue(legacy_target.is_symlink())
        self.assertEqual(legacy_target.resolve(), foreign_source.resolve())

    def test_does_not_remove_legacy_links_when_current_linking_fails(self) -> None:
        legacy_name = next(
            legacy_name
            for entry in self.entries
            for legacy_name in entry.get("renamed_from", [])
        )
        legacy_target = self.old_managed_link(legacy_name)
        collision = self.install_root / self.entries[0]["name"]
        collision.write_text("user-owned path\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            INSTALLER.link_current_skills(self.install_root, self.entries)

        self.assertTrue(legacy_target.is_symlink())


if __name__ == "__main__":
    unittest.main()
