"""Frontend half of conversation identity: behaviour under Node, plus static guards.

The behaviour test runs the helpers straight out of ``index.html``; the static assertions
below hold even on a machine with no Node, and they pin the contracts that a future edit is
most likely to break silently.
"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"


def _index_html():
    return INDEX_HTML.read_text(encoding="utf-8")


class ConversationIdentityFrontendBehaviourTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend behavior tests")
    def test_resolver_list_fold_collapse_keys_and_search_join(self):
        result = subprocess.run(
            [shutil.which("node"),
             str(Path(__file__).with_name("conversation_identity_frontend_test.js"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConversationIdentityFrontendStaticTests(unittest.TestCase):
    def test_one_resolver_instead_of_linear_id_lookups(self):
        """Every id lookup goes through findItem / currentItemFor.

        The twelve `state.items.find(x => x.id === ...)` scans this replaced were each free to
        answer a different question; one resolver cannot drift from itself.
        """
        html = _index_html()
        self.assertNotIn("state.items.find(", html)
        self.assertIn("function findItem(id)", html)
        self.assertIn("function currentItemFor(id)", html)

    def test_record_id_is_resolved_before_any_conversation_id(self):
        """A record id addresses exactly one transcript and must never be overridden."""
        html = _index_html()
        body = html[html.index("function findItem(id)"):]
        body = body[:body.index("}", body.index("return"))]
        self.assertIn("_byRecord.get(id) || _byConversation.get(id)", body)

    def test_state_items_is_only_replaced_through_the_indexing_setter(self):
        """Assigning the array directly would leave the two lookup indexes stale."""
        html = _index_html()
        assignments = re.findall(r"state\.items = [^;]+;", html)
        self.assertEqual(
            assignments, ["state.items = Array.isArray(items) ? items : [];"],
            "state.items must only be replaced inside setItems()",
        )
        # The definition plus the three places a fresh /api/sessions payload arrives:
        # first load, the refresh a search does before it runs, and the live poll.
        self.assertEqual(html.count("setItems("), 4)
        self.assertIn("setItems(await res.json());", html)

    def test_the_list_shows_one_entry_per_conversation(self):
        html = _index_html()
        self.assertIn("const entries = conversationItems();", html)
        self.assertIn("function conversationItems() { return state.items.filter(isCurrentRecord); }", html)

    def test_the_cosmetic_rewind_filter_is_gone(self):
        """It hid ancestor cards behind a global checkbox; conversations replace it."""
        html = _index_html()
        for stale in ("show-rewound", "showRewound", "rewind_current_session_id",
                      "rewind_history", "hiddenRewound", "rewind-filter"):
            self.assertNotIn(stale, html, stale)

    def test_collapse_overrides_are_keyed_by_conversation_with_a_record_fallback(self):
        html = _index_html()
        self.assertIn("function cardKey(m) { return conversationId(m) || (m ? m.id : ''); }", html)
        self.assertIn("state.cardCollapsed.has(key) || state.cardCollapsed.has(m.id)", html)
        self.assertIn("state.cardExpanded.has(key) || state.cardExpanded.has(m.id)", html)

    def test_an_earlier_record_reader_offers_the_current_one_instead_of_retargeting(self):
        html = _index_html()
        self.assertIn("This is an earlier record of a conversation that continued elsewhere.", html)
        self.assertIn("Open the current one", html)
        # The link the reader hands out stays the record's own id while an earlier record is open.
        self.assertIn("const linkId = (convRecords.length > 1 && !isEarlierRecord && !historicalView)", html)

    def test_reader_reads_conversation_facts_from_the_response_before_the_card(self):
        """The standalone reader has no card, so `data` has to answer first in both modes."""
        html = _index_html()
        self.assertIn(
            "const convRecords = conversationRecords(data).length "
            "? conversationRecords(data) : conversationRecords(meta);", html)
        self.assertIn(
            "const convCurrentId = data.conversation_current_id "
            "|| meta?.conversation_current_id || data.id;", html)

    def test_live_follow_offers_the_new_current_record(self):
        html = _index_html()
        self.assertIn("function noticeConversationMoved(data)", html)
        self.assertIn("This conversation continued in a new record", html)
        self.assertIn("noticeConversationMoved(data);", html)

    def test_rows_abandoned_by_an_in_file_rewind_are_counted_out_loud(self):
        """The backend drops those rows from the turns, so the page has to say how many.

        The field arrives with a separate change; this build renders the notice the moment a
        payload carries a non-zero count, and nothing at all when it is absent or zero.
        """
        html = _index_html()
        self.assertIn("const abandonedRows = Number(data.rewind_abandoned_rows) || 0;", html)
        self.assertIn("abandoned by a rewind and", html)
        self.assertIn("${abandonedHtml}", html)
        block = html[html.index("const abandonedHtml"):]
        self.assertIn("abandonedRows > 0", block[:200])

    def test_older_notes_are_listed_separately_and_never_concatenated(self):
        html = _index_html()
        self.assertIn("olderNotesBlock", html)
        self.assertIn("card-older-note-id", html)
        self.assertIn("EARLIER NOTES · ", html)

    def test_ui_adds_no_new_colour_variables(self):
        """design-system.md §1: prove the existing tokens cannot cover it before adding one."""
        html = _index_html()
        root = html[html.index(":root {"):html.index("}", html.index(":root {"))]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root))
        expected_new = {"--card-records", "--conv-rewind-relation"}
        self.assertFalse(declared & expected_new)
        for block in ("card-records", "card-lineage", "card-older-notes", "search-match-record"):
            rules = re.findall(r"\.%s[^{]*\{([^}]*)\}" % block, html)
            self.assertTrue(rules, block)
            for rule in rules:
                self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b",
                                    f"{block} must use tokens, not a hex colour")


if __name__ == "__main__":
    unittest.main()
