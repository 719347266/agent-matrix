"""Quotes that differ from the transcript only in spacing must not sink a meeting; made-up words still do."""
from hashlib import sha256
import unittest

from agent_matrix.meeting_review import Action, Grounded, Review, repair_review, validate_review

# Feishu transcripts put two spaces before each line break; the AI copies the lines without them.
T = ('<cite type="user" uid-ref="u4" user-name="我"></cite> 00:21:39  \n可以。OK。行，我弄一下，然后把图发出来。\n\n\n\n'
     '<cite type="user" uid-ref="u2" user-name="老周"></cite> 00:28:56  \n就每天的例会，但凡进开发的需求都需要同步。\n\n')
META = {"meeting_id": "m1", "start_time": "2026-11-18 14:03", "owner_name": "我"}


def review(**parts):
    return Review(meeting_id="m1", transcript_sha256=sha256(T.encode()).hexdigest(), **parts)


class GroundingTests(unittest.TestCase):
    def test_a_line_copied_without_the_trailing_spaces_is_kept(self):
        action = Action(title="补齐角色流程图", text="补图", quote="行，我弄一下，然后把图发出来",
                        timestamp="00:21:39", reference='<cite type="user" uid-ref="u4" user-name="我"></cite> 00:21:39\n可以。OK。行，我弄一下，然后把图发出来。',
                        owner_name="我", commitment_phrase="我弄一下", confidence=0.9)
        fixed, notes = repair_review(review(actions=[action]), T, META)
        self.assertEqual([a.title for a in fixed.actions], ["补齐角色流程图"])
        self.assertIn(fixed.actions[0].reference, T)       # the exact transcript words are stored
        validate_review(fixed, T, META)

    def test_a_reworded_context_is_rebuilt_from_the_transcript_when_the_quote_is_real(self):
        req = Grounded(text="进开发的需求都要在例会同步", quote="但凡进开发的需求都需要同步", timestamp="00:28:56",
                       reference='<cite type="user" uid-ref="u2" user-name="老周"></cite> 00:28:56\n就是每天的例会，但凡进开发的需求都需要同步。')
        fixed, notes = repair_review(review(requirements=[req]), T, META)
        self.assertEqual(len(fixed.requirements), 1)
        self.assertTrue(fixed.requirements[0].reference.startswith('<cite type="user" uid-ref="u2"'))
        validate_review(fixed, T, META)

    def test_words_nobody_said_are_still_dropped(self):
        made_up = Grounded(text="周五上线", quote="这周五必须上线", timestamp="00:28:56",
                           reference="00:28:56\n这周五必须上线")
        fixed, notes = repair_review(review(decisions=[made_up]), T, META)
        self.assertEqual(fixed.decisions, [])
        self.assertTrue(any(n.startswith("去掉") for n in notes))


if __name__ == "__main__":
    unittest.main()
