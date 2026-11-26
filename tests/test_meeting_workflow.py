from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent_matrix.meeting_watch import MeetingQueue, check, prepare, month_ranges, defer, notify_missing, TZ
from agent_matrix.meeting_review import Review, Action, validate_review, task_fields, publish
from agent_matrix.agents.pre_worker import assess
from agent_matrix.models import Config, MatrixError
from agent_matrix.state import State


MID="1234567890123456789"


class MeetingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.state=State(self.root/"state/state.db"); self.queue=MeetingQueue(self.state)
        self.now=datetime(2026,11,16,11,30,tzinfo=TZ)
        self.config=Config(owner_name="示例用户",owner_open_id="ou_fixture",base_token="base_owned",
                           tables={"事项表":"tbl_tasks","会议处理记录表":"tbl_meetings"},
                           meeting_watch_enabled=True,meeting_watch_started_at="2026-11-16T00:00:00+08:00")
        self.meta={"meeting_id":MID,"topic":"模拟周会","start_time":"2026-11-16 10:00","end_time":"2026-11-16 11:00",
                   "note_id":"1234","source_url":"https://example.invalid/meeting"}
        self.source=SimpleNamespace(agenda=Mock(return_value=[]),search=Mock(return_value=[{"id":MID,"meta_data":{"app_link":self.meta["source_url"]}}]),detail=Mock(return_value=self.meta))

    def tearDown(self):
        self.state.close(); self.tmp.cleanup()

    def test_detects_without_fetching_transcript_and_waits_for_permission(self):
        result=check(self.config,self.source,self.queue,self.now)
        self.assertFalse(result["run_model"])
        self.assertEqual(result["reason"],"awaiting_processing_permission")
        client=SimpleNamespace(root=self.root,_exec=Mock())
        with self.assertRaises(MatrixError): prepare(self.config,client,self.queue,MID)
        client._exec.assert_not_called()

    def test_duplicate_detection_does_not_duplicate_or_acknowledge(self):
        check(self.config,self.source,self.queue,self.now)
        check(self.config,self.source,self.queue,self.now)
        self.assertEqual(len(self.queue.list()),1)
        self.assertEqual(self.queue.get(MID)["status"],"ready")
        with self.assertRaises(MatrixError): self.queue.done(MID,{"ok":True})

    def test_source_failure_is_not_no_change(self):
        self.source.search.side_effect=MatrixError("source failed")
        with self.assertRaises(MatrixError): check(self.config,self.source,self.queue,self.now)

    def test_waiting_transcript_backoff_and_eventual_ready(self):
        self.source.detail.return_value={**self.meta,"note_id":None}
        result=check(self.config,self.source,self.queue,self.now)
        self.assertEqual(result["ready"],[])
        self.assertEqual(self.queue.get(MID)["status"],"waiting_transcript")
        self.source.detail.return_value=self.meta
        self.config.transcripts_to_llm_allowed=True; self.config.llm_backend="cindy_current"
        result=check(self.config,self.source,self.queue,self.now+timedelta(minutes=2))
        self.assertTrue(result["run_model"])

    def test_no_record_after_24h_is_attention_not_fabricated_summary(self):
        self.source.detail.return_value={**self.meta,"note_id":None}
        check(self.config,self.source,self.queue,self.now+timedelta(days=1))
        self.assertEqual(self.queue.get(MID)["status"],"needs_attention")

    def test_future_meeting_rejected(self):
        with self.assertRaises(MatrixError): self.queue.put({**self.meta,"end_time":"2026-11-17 11:00"},self.now)

    def test_scheduled_calendar_event_alone_does_not_trigger_post_meeting_review(self):
        self.source.search.return_value=[]
        self.source.agenda.return_value=[{"event_id":"fixture_calendar_event","summary":"已预约的会议"}]
        self.config.transcripts_to_llm_allowed=True; self.config.llm_backend="cindy_current"
        result=check(self.config,self.source,self.queue,self.now)
        self.assertEqual(result["scheduled_events_count"],1)
        self.assertFalse(result["run_model"])
        self.assertEqual(self.queue.list(),[])
        self.assertEqual(len(self.state.get("meeting_agenda_snapshot")["events"]),1)

    def test_unready_transcript_is_deferred_and_missing_notice_is_sent_once(self):
        self.queue.put(self.meta,self.now)
        self.assertEqual(defer(self.queue,MID,self.now)["status"],"waiting_transcript")
        self.assertEqual(defer(self.queue,MID,self.now+timedelta(days=1))["status"],"needs_attention")
        client=SimpleNamespace(send_self=Mock(return_value={"message_id":"om_missing"}))
        notify_missing(self.config,client,self.queue,MID)
        notify_missing(self.config,client,self.queue,MID)
        client.send_self.assert_called_once()
        self.assertEqual(self.queue.get(MID)["status"],"attention_sent")

    def test_prepared_meeting_lease_prevents_minutely_model_wakeups(self):
        self.queue.put(self.meta,self.now)
        self.queue.update(MID,status="prepared",next_check=(self.now+timedelta(minutes=30)).isoformat())
        self.config.transcripts_to_llm_allowed=True; self.config.llm_backend="cindy_current"
        self.assertFalse(check(self.config,self.source,self.queue,self.now)["run_model"])
        self.assertTrue(check(self.config,self.source,self.queue,self.now+timedelta(minutes=31))["run_model"])

    def test_month_boundary_does_not_drop_pending_dates(self):
        low=datetime(2026,11,16,tzinfo=TZ); high=datetime(2027,1,2,tzinfo=TZ)
        ranges=list(month_ranges(low,high))
        self.assertEqual(len(ranges),3)
        for a,b in zip(ranges,ranges[1:]): self.assertEqual(a[1]+timedelta(seconds=1),b[0])

    def make_review(self):
        transcript="00:10 示例用户：我明天提交评审材料。\n00:30 同事：这件事还需要确认验收范围。"
        action=Action(title="提交评审材料",text="整理后提交材料",quote="我明天提交评审材料。",timestamp="00:10",
                      reference=transcript.splitlines()[0],owner_name="示例用户",due_phrase="明天",due_date="2026-11-17",direction="mine",
                      commitment_phrase="我明天提交",confidence=0.95)
        return transcript,Review(meeting_id=MID,transcript_sha256=sha256(transcript.encode()).hexdigest(),actions=[action])

    def test_quotes_owner_and_due_must_have_evidence(self):
        text,review=self.make_review()
        validate_review(review,text,self.meta)
        for changes in ({"quote":"没有说过的话"},{"timestamp":"12:35"},{"owner_name":"其他人"},{"due_date":"2026-11-18"}):
            altered=review.model_copy(deep=True)
            altered.actions[0]=altered.actions[0].model_copy(update=changes)
            with self.assertRaises(MatrixError): validate_review(altered,text,self.meta)

    def test_unknown_owner_or_date_is_pending_not_guessed(self):
        _,review=self.make_review()
        action=review.actions[0]
        self.assertEqual(task_fields(action,self.config,self.meta)["状态"],["未开始"])
        # What the AI would do from a meeting waits for the owner's 「AI做」, even with an owner and a date.
        self.assertEqual(task_fields(action.model_copy(update={"executor":"AI"}),self.config,self.meta)["状态"],["待确认"])
        for changes in ({"owner_name":None},{"due_date":None}):
            fields=task_fields(action.model_copy(update=changes),self.config,self.meta)
            self.assertEqual(fields["状态"],["待确认"])
        # Somebody else's dated work is theirs, not the owner's decision; it stays out of the brief.
        other=task_fields(action.model_copy(update={"owner_name":"同事","direction":"others"}),self.config,self.meta)
        self.assertEqual((other["执行方"],other["状态"],other.get("排除本人提醒")),(["别人"],["未开始"],True))
        self.assertNotIn("负责人",other)

    def test_owned_action_without_deadline_waits_for_owner_before_ai_starts(self):
        _,review=self.make_review()
        self.config.prework_enabled=True
        action=review.actions[0].model_copy(update={"due_date":None,"due_phrase":None,"executor":"AI"})
        fields=task_fields(action,self.config,self.meta)
        self.assertEqual((fields["状态"],fields["执行方"]),(["待确认"],["AI"]))
        self.assertEqual(fields["负责人"],[{"id":"ou_fixture"}])
        self.assertNotIn("预做策略",fields)
        self.assertNotIn("截止日",fields)
        result=assess(self.config,[{"record_id":"rec_draft",**fields}],today=self.now.date())
        self.assertEqual(result["candidate_count"],0)

    def test_unknown_owner_or_disabled_prework_never_gets_draft_exception(self):
        _,review=self.make_review()
        action=review.actions[0].model_copy(update={"due_date":None,"due_phrase":None})
        self.assertNotIn("预做策略",task_fields(action,self.config,self.meta))
        self.config.prework_enabled=True
        for owner in [None,"同事",""]:
            with self.subTest(owner=owner):
                fields=task_fields(action.model_copy(update={"owner_name":owner,"direction":"unknown"}),self.config,self.meta)
                self.assertEqual(fields["状态"],["待确认"])
                self.assertNotIn("预做策略",fields)
                self.assertNotIn("负责人",fields)

    def test_missing_configured_owner_identity_cannot_enable_draft_exception(self):
        _,review=self.make_review()
        action=review.actions[0].model_copy(update={"due_date":None,"due_phrase":None})
        self.config.prework_enabled=True
        self.config.owner_open_id=""
        fields=task_fields(action,self.config,self.meta)
        self.assertNotIn("负责人",fields)
        self.assertNotIn("预做策略",fields)
        self.assertEqual(fields["状态"],["待确认"])

    def test_confirmed_deadline_keeps_normal_meeting_action_policy(self):
        _,review=self.make_review()
        self.config.prework_enabled=True
        fields=task_fields(review.actions[0],self.config,self.meta)
        self.assertEqual(fields["状态"],["未开始"])
        self.assertEqual(fields["截止日"],"2026-11-17")
        self.assertNotIn("预做策略",fields)

    def test_normal_doc_nested_content_is_prepared_only_after_consent(self):
        self.queue.put(self.meta,self.now); self.queue.update(MID,status="ready")
        self.config.transcripts_to_llm_allowed=True; self.config.llm_backend="cindy_current"
        text,_=self.make_review()
        client=SimpleNamespace(root=self.root,_exec=Mock(side_effect=[{"note":{"note_display_type":"normal","verbatim_doc_token":"doc_fixture"}},
                                                                    {"document":{"content":text,"reference_map":{}}}]))
        prepared=prepare(self.config,client,self.queue,MID)
        self.assertEqual(Path(prepared["transcript_path"]).read_text(),text)
        self.assertEqual(self.queue.get(MID)["status"],"prepared")
        self.assertEqual(prepare(self.config,client,self.queue,MID)["transcript_sha256"],prepared["transcript_sha256"])
        self.assertEqual(client._exec.call_count,2)

    def test_publish_retry_does_not_duplicate_tasks_or_notifications(self):
        text,review=self.make_review()
        self.queue.put(self.meta,self.now)
        path=self.root/"state/transcript.md"; path.write_text(text)
        self.queue.update(MID,status="prepared",transcript_path="state/transcript.md",transcript_sha256=review.transcript_sha256)
        self.config.transcripts_to_llm_allowed=True; self.config.llm_backend="cindy_current"
        self.state.set("owned_base","base_owned")
        writes=[]
        def call(command,*args,**kwargs):
            if command=="+field-list":
                return {"fields":[{"name":n} for n in ["标题","状态","原话引用","去重键","会议ID","提取状态","提取待办数"]]}
            writes.append(command)
            return {"record_id_list":["rec_fixture"]}
        client=SimpleNamespace(root=self.root,call=call,send_self=Mock(return_value={"message_id":"om_fixture"}))
        first=publish(self.config,client,self.state,review)
        second=publish(self.config,client,self.state,review)
        self.assertEqual(first,second); self.assertEqual(len(writes),2); client.send_self.assert_called_once()
        self.assertEqual(self.queue.get(MID)["status"],"done")


if __name__=="__main__":
    unittest.main()


class SearchIntervalTests(unittest.TestCase):
    """How often Feishu is searched for finished meetings: 30 min with live meeting pushes, 5 min without."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.state.close)
        self.queue = MeetingQueue(self.state)
        self.now = datetime(2026, 11, 19, 17, 0, tzinfo=TZ)
        self.state.set("listen:heartbeat", (self.now - timedelta(seconds=30)).isoformat())

    def due(self, minutes_since_search, status):
        from agent_matrix.meeting_watch import _search_due
        self.state.set("meeting_watch:last_search", (self.now - timedelta(minutes=minutes_since_search)).isoformat())
        self.state.set("listen:status", status)
        return _search_due(self.queue, self.now)

    def test_without_meeting_pushes_search_every_five_minutes(self):
        status = {"vc.meeting.participant_meeting_ended_v1": "not_subscribed", "vc.note.generated_v1": "not_subscribed",
                  "im.message.receive_v1": "live"}
        self.assertFalse(self.due(4, status))
        self.assertTrue(self.due(5, status))

    def test_one_meeting_push_missing_still_means_five_minutes(self):
        status = {"vc.meeting.participant_meeting_ended_v1": "live", "vc.note.generated_v1": "error 1"}
        self.assertTrue(self.due(6, status))

    def test_with_both_meeting_pushes_live_search_every_thirty_minutes(self):
        status = {"vc.meeting.participant_meeting_ended_v1": "live", "vc.note.generated_v1": "live"}
        self.assertFalse(self.due(29, status))
        self.assertTrue(self.due(30, status))

    def test_listener_down_or_first_run_searches_every_time(self):
        from agent_matrix.meeting_watch import _search_due
        self.assertTrue(_search_due(self.queue, self.now))  # never searched yet
        self.state.set("meeting_watch:last_search", self.now.isoformat())
        self.state.set("listen:heartbeat", (self.now - timedelta(minutes=10)).isoformat())
        self.assertTrue(_search_due(self.queue, self.now))


class ListenerMeetingCheckTests(unittest.TestCase):
    """The listener's minute job looks for finished meetings itself instead of waiting for the AI employee."""

    def setUp(self):
        from unittest.mock import patch
        from agent_matrix import listen
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "state").mkdir()
        self.state = State(root / "state/state.db")
        self.addCleanup(self.state.close)
        self.listener = listen.Listener(root / "config.yaml")
        self.config = Config(meeting_watch_enabled=True, meeting_watch_started_at="2026-11-19T00:00:00+08:00")
        self.now = datetime(2026, 11, 19, 17, 0, tzinfo=TZ)
        self.kicks = []
        self.listener.wake_worker = lambda config, force=False: self.kicks.append(force)
        self.patch = patch

    def run_check(self, effect):
        def fake_check(config, source, queue, now):
            effect(queue)
            return {"ok": True}
        with self.patch("agent_matrix.meeting_watch.check", fake_check):
            return self.listener.meeting_check(self.config, None, self.state, self.now)

    def meta(self, mid):
        return {"meeting_id": mid, "topic": "周会", "start_time": "2026-11-19T16:00:00+08:00", "end_time": "2026-11-19T16:50:00+08:00"}

    def test_nothing_new_stays_quiet_and_does_not_wake_the_ai(self):
        self.assertEqual(self.run_check(lambda q: None), [])
        self.assertEqual(self.kicks, [])

    def test_a_meeting_that_just_became_ready_wakes_the_ai_once(self):
        mid = "7000000000000000001"
        self.assertEqual(self.run_check(lambda q: (q.put(self.meta(mid), self.now), q.update(mid, status="ready"))), [mid])
        self.assertEqual(self.kicks, [True])
        self.assertEqual(self.run_check(lambda q: None), [])  # still ready next minute: no second wake-up
        self.assertEqual(self.kicks, [True])

    def test_a_meeting_the_owner_cannot_read_wakes_nobody_says_so_once_and_is_tried_again_in_minutes(self):
        from agent_matrix.feishu import FeishuError
        mid = "7000000000000000003"
        said = []
        class Client:
            root = Path(self.tmp.name)
            def send_self(self, text, key):
                said.append(text)
                return {"message_id": "local_1"}
        def refuse(*_args, **_kwargs):
            raise FeishuError("no_permission", detail="No read permission for minute x")
        config = self.config.model_copy(update={"transcripts_to_llm_allowed": True, "llm_backend": "cindy_current"})
        meta = {**self.meta(mid), "end_time": "2026-11-19 16:50"}
        def fake_check(config, source, queue, now):
            if not queue.get(mid):
                queue.put(meta, now)
                queue.update(mid, status="ready", next_check=now.isoformat())
        with self.patch("agent_matrix.meeting_watch.check", fake_check), self.patch("agent_matrix.meeting_watch.prepare", refuse):
            self.assertEqual(self.listener.meeting_check(config, Client(), self.state, self.now), [])
            self.assertEqual(self.kicks, [])                                   # no transcript: no AI woken
            row = MeetingQueue(self.state).get(mid)
            self.assertEqual(row["status"], "waiting_transcript")
            self.assertEqual(row["next_check"], (self.now + timedelta(minutes=5)).isoformat())
            MeetingQueue(self.state).update(mid, status="ready", next_check=self.now.isoformat())
            self.listener.meeting_check(config, Client(), self.state, self.now)
        self.assertEqual(len(said), 1)                                         # told once, not every 5 minutes
        self.assertIn("没有查看权限", said[0])

    def test_a_meeting_still_waiting_for_its_transcript_does_not_wake_the_ai(self):
        mid = "7000000000000000002"
        self.assertEqual(self.run_check(lambda q: (q.put(self.meta(mid), self.now), q.update(mid, status="waiting_transcript"))), [])
        self.assertEqual(self.kicks, [])

    def test_a_failing_check_is_recorded_and_does_not_raise(self):
        def broken(queue):
            raise MatrixError("飞书暂时不可用")
        self.assertIsNone(self.run_check(broken))

    def test_switched_off_does_nothing(self):
        self.config.meeting_watch_enabled = False
        self.assertIsNone(self.run_check(lambda q: self.fail("should not run")))
