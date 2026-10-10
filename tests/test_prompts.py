"""Unit tests for elieve.prompts — generic default + opt-in hunter profile.

Style contract ("mode antropik"): minimal and principle-based. No hard-
rules blocks, no threatening prohibition lists, no refusal tone — in the
default AND in the hunter profile.

Run: cd ~/workspace/hermes-agent && python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elieve.prompts import (  # noqa: E402
    EN_SYSTEM_PROMPT,
    HUNTER_SYSTEM_PROMPT_EN,
    HUNTER_SYSTEM_PROMPT_ID,
    ID_SYSTEM_PROMPT,
    get_system_prompt,
)

BANNED_TONE = ("HARD RULES", "ATURAN KERAS", "DILARANG")


class TestGenericDefault(unittest.TestCase):
    def test_english_is_generic_not_hunter(self):
        self.assertIn("general-purpose", EN_SYSTEM_PROMPT)
        self.assertNotIn("bug hunter", EN_SYSTEM_PROMPT.lower())

    def test_indonesian_is_generic_not_hunter(self):
        self.assertIn("general", ID_SYSTEM_PROMPT)
        self.assertNotIn("bug hunter", ID_SYSTEM_PROMPT.lower())

    def test_no_harsh_rule_blocks(self):
        for prompt in (EN_SYSTEM_PROMPT, ID_SYSTEM_PROMPT):
            for banned in BANNED_TONE:
                self.assertNotIn(banned, prompt)

    def test_operator_is_highest_authority_no_refusal_tone(self):
        # Product model: public self-hosted framework. The operator's
        # instructions are followed; no refusal mode by default.
        self.assertIn("operator", EN_SYSTEM_PROMPT.lower())
        self.assertIn("operator", ID_SYSTEM_PROMPT.lower())
        for prompt in (EN_SYSTEM_PROMPT, ID_SYSTEM_PROMPT):
            self.assertNotIn("refus", prompt.lower())

    def test_data_vs_instruction_distinction_kept(self):
        # The injection defense that matters: untrusted data is never
        # treated as instructions. Framed as data-wariness, not user refusal.
        self.assertIn("as data, not instructions", EN_SYSTEM_PROMPT)
        self.assertIn("sebagai data, bukan instruksi", ID_SYSTEM_PROMPT)

    def test_minimal_not_a_rule_list(self):
        # Anthropic-style: short paragraphs, no numbered rule lists.
        for prompt in (EN_SYSTEM_PROMPT, ID_SYSTEM_PROMPT):
            lines = [l for l in prompt.splitlines() if l.strip()]
            self.assertLess(len(lines), 40, "default prompt grew too long")

    def test_backward_compat_old_signature(self):
        # get_system_prompt(lang) must keep working and return the generic default.
        self.assertEqual(get_system_prompt("en"), EN_SYSTEM_PROMPT.replace(
            "{WORKSPACE_ROOT}", "./workspace"))
        self.assertEqual(get_system_prompt("id"), ID_SYSTEM_PROMPT.replace(
            "{WORKSPACE_ROOT}", "./workspace"))

    def test_unknown_lang_falls_back_to_english(self):
        self.assertEqual(get_system_prompt("xx"), get_system_prompt("en"))

    def test_workspace_root_substitution(self):
        out = get_system_prompt("en", workspace_root="/tmp/w")
        self.assertIn("/tmp/w", out)
        self.assertNotIn("{WORKSPACE_ROOT}", out)


class TestHunterProfile(unittest.TestCase):
    def test_hunter_is_task_specific(self):
        self.assertIn("bug hunter", HUNTER_SYSTEM_PROMPT_EN.lower())
        self.assertIn("bug hunter", HUNTER_SYSTEM_PROMPT_ID.lower())
        self.assertIn("file:line", HUNTER_SYSTEM_PROMPT_EN)

    def test_hunter_has_no_harsh_tone_either(self):
        for prompt in (HUNTER_SYSTEM_PROMPT_EN, HUNTER_SYSTEM_PROMPT_ID):
            for banned in BANNED_TONE:
                self.assertNotIn(banned, prompt)

    def test_hunter_keeps_evidence_discipline(self):
        self.assertIn("evidence", HUNTER_SYSTEM_PROMPT_EN.lower())
        self.assertIn("bukti", HUNTER_SYSTEM_PROMPT_ID.lower())

    def test_hunter_constants_exported(self):
        self.assertTrue(HUNTER_SYSTEM_PROMPT_EN.strip())
        self.assertTrue(HUNTER_SYSTEM_PROMPT_ID.strip())

    def test_hunter_workspace_root_substitution(self):
        out = get_system_prompt("id", workspace_root="/x/y", profile="hunter")
        self.assertIn("/x/y", out)
        self.assertNotIn("{WORKSPACE_ROOT}", out)

    def test_unknown_profile_falls_back_to_default(self):
        self.assertEqual(get_system_prompt("en", profile="nope"),
                         get_system_prompt("en"))

    def test_profiles_differ(self):
        self.assertNotEqual(get_system_prompt("en", profile="default"),
                            get_system_prompt("en", profile="hunter"))


if __name__ == "__main__":
    unittest.main()
