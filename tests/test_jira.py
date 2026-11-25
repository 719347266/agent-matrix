"""Jira source: open assigned issues become items once; Done in Jira closes them; reassigned / deleted ones are
closed with the reason; a new title or due date is followed."""
from datetime import datetime
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent_matrix.models import Config
from agent_matrix.sources import jira
from agent_matrix.state import State


class FakeGateway:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        return list(self._rows)

    def create(self, fields, key, source):
        rid = f"rec{len(self._rows)}"
        self._rows.append({**fields, "record_id": rid, "去重键": key, "编号": len(self._rows)})
        return {"record_id_list": [rid]}

    def update(self, row, patch, key, source):
        row.update(patch)


class FakeJira:
    """issues: key → {summary, duedate, done, assignee}; a key missing from it is deleted (404)."""

    def __init__(self):
        self.issues = {"LL-1": {"summary": "概要设计", "duedate": "2026-12-01", "done": False, "assignee": "me"},
                       "LL-2": {"summary": "安全合规", "duedate": None, "done": False, "assignee": "me"}}
        self.broken = set()   # keys whose lookup fails for another reason (500)
        self.me = "me"        # None: Jira does not say who the owner is

    def __call__(self, cfg, method, path, body=None):
        if path == "/rest/api/3/myself":
            return {"accountId": self.me, "displayName": "我"} if self.me else {}
        if path.startswith("/rest/api/3/user/search"):
            return [{"accountId": "a-amy", "displayName": "Amy Chen", "emailAddress": "amy@x.com"},
                     {"accountId": "a-amos", "displayName": "Amos Li"},
                     {"accountId": "me", "displayName": "我自己"}]
        if path == "/rest/api/3/search/jql":
            assert "key in" not in body["jql"]   # one stale key must not break the whole query
            self.jql = body["jql"]
            jql, quoted = body["jql"], set(re.findall(r'"([^"]+)"', body["jql"]))
            by_owner = "cf[9]" in jql
            def wanted(k, v):
                taken = v.get("owner") if by_owner else v["assignee"]
                return not v["done"] and (v["assignee"] in {"me"} | quoted or by_owner and v.get("owner") in {"me"} | quoted
                                          or not taken and k.split("-")[0] in quoted)
            return {"isLast": True, "issues": [{"key": k, "fields": self.fields(k, v) | {"summary": v["summary"], "duedate": v["duedate"]}}
                                               for k, v in self.issues.items() if wanted(k, v)]}
        if path == "/rest/api/3/field":
            return [{"id": "summary", "name": "Summary"}, {"id": "customfield_9", "name": "Issue Owner"},
                    {"id": "customfield_7", "name": "App Plan"}, {"id": "customfield_8", "name": "App Plan"}]
        if path.startswith("/rest/api/3/project/"):
            if path.endswith("/LGS"):
                return {"key": "LGS"}
            raise jira.JiraHTTPError("Jira 失败：HTTP 404", 404)
        key = path.split("/issue/")[1].split("?")[0]
        if key in self.broken:
            raise jira.JiraHTTPError("Jira 失败：HTTP 500", 500)
        if key not in self.issues:
            raise jira.JiraHTTPError("Jira 失败：HTTP 404", 404)
        v = self.issues[key]
        return {"key": key, "fields": self.fields(key, v)}

    def fields(self, key, v):
        user = lambda a: a and {"accountId": a, "displayName": a}
        return {"status": {"statusCategory": {"key": "done" if v["done"] else "indeterminate"}},
                "project": {"key": key.split("-")[0]}, "assignee": user(v["assignee"]), "customfield_9": user(v.get("owner")),
                "customfield_7": v.get("plan"), "customfield_8": v.get("plan_option") and {"value": v["plan_option"]}}


class JiraSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(owner_open_id="ou_me", item_store="device", jira_site="acme.atlassian.net",
                             jira_email="me@x.com", jira_token="t")
        self.rows = [{"record_id": "rec0", "标题": "写周报", "状态": ["未开始"], "执行方": ["我"], "去重键": "manual:1"}]
        self.gw = FakeGateway(self.rows)
        patcher = unittest.mock.patch("agent_matrix.workflow.Gateway", lambda c, s: self.gw)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = State(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.state.close)
        self.client = SimpleNamespace(root=Path(self.tmp.name), config=self.config)
        self.now = datetime(2026, 11, 23, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.jira = FakeJira()

    def sync(self):
        return jira.sync(self.config, self.client, self.state, self.now, api=self.jira)

    def made(self, key):
        return next(r for r in self.rows if r.get("去重键") == "jira:" + key)

    def test_creates_once_and_follows_done(self):
        self.assertEqual(self.sync()["created"], 2)
        self.assertEqual(self.made("LL-1")["标题"], "LL-1 概要设计")
        self.assertEqual(self.made("LL-1")["来源链接"], "https://acme.atlassian.net/browse/LL-1")
        self.assertEqual(self.made("LL-1")["截止日"], "2026-12-01 00:00")
        self.assertEqual(self.sync()["created"], 0)   # no duplicates
        self.jira.issues["LL-2"]["done"] = True
        self.assertEqual(self.sync()["completed"], 1)
        self.assertEqual(self.made("LL-2")["状态"], ["完成"])
        self.assertEqual(self.made("LL-1")["状态"], ["未开始"])

    def test_reassigned_or_deleted_is_closed_and_one_failure_skips_only_itself(self):
        self.sync()
        self.jira.issues["LL-1"]["assignee"] = "carl"   # handed to someone else
        del self.jira.issues["LL-2"]                    # deleted (404)
        out = self.sync()
        self.assertEqual((out["closed"], out["completed"]), (2, 0))
        self.assertEqual((self.made("LL-1")["状态"], self.made("LL-2")["状态"]), (["取消"], ["取消"]))

        self.jira.issues["LL-3"] = {"summary": "日志", "duedate": None, "done": False, "assignee": "me"}
        self.jira.issues["LL-4"] = {"summary": "监控", "duedate": None, "done": False, "assignee": "me"}
        self.sync()
        self.jira.broken.add("LL-3")                    # Jira hiccups on one issue
        self.jira.issues["LL-3"]["done"] = self.jira.issues["LL-4"]["done"] = True
        self.assertEqual(self.sync()["completed"], 1)  # LL-4 still closes
        self.assertEqual((self.made("LL-3")["状态"], self.made("LL-4")["状态"]), (["未开始"], ["完成"]))

    def test_new_issues_send_one_new_item_notice(self):
        sent = []
        self.client.send_self = lambda text, key: sent.append(text) or {"message_id": f"m{len(sent)}"}
        self.sync()
        self.sync()   # nothing new: no second notice
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("Jira · 新增 2 件：你 #1 #2"), sent[0])   # 「新增」+「你 #」: a 新事项 that alerts
        from agent_matrix import inbox
        self.assertEqual(inbox.kind_of(sent[0].splitlines()[0], sent[0]), "new")

    def test_follows_a_new_title_and_due_date(self):
        self.sync()
        self.jira.issues["LL-1"].update(summary="概要设计 v2", duedate="2026-12-05")
        self.assertEqual(self.sync()["edited"], 1)
        self.assertEqual((self.made("LL-1")["标题"], self.made("LL-1")["截止日"]), ("LL-1 概要设计 v2", "2026-12-05 00:00"))
        self.assertEqual(self.sync()["edited"], 0)

    def test_without_the_owners_id_nothing_is_closed(self):
        self.sync()
        self.jira.issues["LL-1"]["assignee"] = "carl"
        self.jira.me = None
        self.assertEqual(self.sync()["closed"], 0)
        self.assertEqual(self.made("LL-1")["状态"], ["未开始"])

    def test_followed_people_issues_come_in_as_someone_elses(self):
        self.config.jira_others = [{"id": "a-amy", "name": "Amy"}]
        self.jira.issues["LL-5"] = {"summary": "接口联调", "duedate": None, "done": False, "assignee": "a-amy"}
        self.jira.issues["LL-6"] = {"summary": "不相干", "duedate": None, "done": False, "assignee": "carl"}
        self.assertEqual(self.sync()["created"], 3)
        self.assertIn('assignee in (currentUser(), "a-amy")', self.jira.jql)
        row = self.made("LL-5")
        self.assertEqual((row["标题"], row["执行方"]), ("Amy · LL-5 接口联调", ["别人"]))
        self.assertEqual(self.made("LL-1")["执行方"], ["我"])
        self.assertFalse(any(r.get("去重键") == "jira:LL-6" for r in self.rows))
        self.jira.issues["LL-5"]["assignee"] = "me"          # Amy hands it to the owner
        self.assertEqual(self.sync()["edited"], 1)
        self.assertEqual((row["标题"], row["执行方"]), ("LL-5 接口联调", ["我"]))
        self.jira.issues["LL-1"]["assignee"] = "a-amy"       # and the other way round: still followed, not closed
        out = self.sync()
        self.assertEqual((out["edited"], out["closed"]), (1, 0))
        self.assertEqual(self.made("LL-1")["执行方"], ["别人"])
        self.config.jira_others = []                          # stop following Amy: her issue closes
        self.assertEqual(self.sync()["closed"], 1)
        self.assertEqual(self.made("LL-1")["状态"], ["取消"])

    def test_unassigned_issues_in_watched_projects_wait_for_the_owner(self):
        self.config.jira_unassigned_projects = ["LGS"]
        self.jira.issues["LGS-7"] = {"summary": "商家反馈积分没到账", "duedate": None, "done": False, "assignee": None}
        self.jira.issues["SEC-1"] = {"summary": "别的项目", "duedate": None, "done": False, "assignee": None}
        self.assertEqual(self.sync()["created"], 3)
        row = self.made("LGS-7")
        self.assertEqual((row["标题"], row["状态"]), ("没人领 · LGS-7 商家反馈积分没到账", ["待确认"]))
        self.assertFalse(any(r.get("去重键") == "jira:SEC-1" for r in self.rows))
        self.assertEqual(self.sync()["closed"], 0)            # still nobody's: stays
        self.jira.issues["LGS-7"]["assignee"] = "carl"         # someone else picked it up
        self.assertEqual(self.sync()["closed"], 1)
        self.assertEqual(row["状态"], ["取消"])

    def test_owner_field_decides_who_took_it(self):
        """LPB-style: 经办人 is whoever filed it; Issue Owner is who handles it, empty until someone takes it."""
        self.config.jira_unassigned_projects = ["LPB"]
        self.config.jira_owner_field = {"id": "customfield_9", "name": "Issue Owner"}
        self.jira.issues["LPB-268"] = {"summary": "积分邮件", "duedate": None, "done": False, "assignee": "kathy", "owner": None}
        self.jira.issues["LPB-270"] = {"summary": "我领的", "duedate": None, "done": False, "assignee": "kathy", "owner": "me"}
        self.assertEqual(self.sync()["created"], 4)
        self.assertIn('cf[9] is EMPTY AND statusCategory = "To Do"', self.jira.jql)
        self.assertEqual((self.made("LPB-268")["标题"], self.made("LPB-268")["状态"]), ("没人领 · LPB-268 积分邮件", ["待确认"]))
        self.assertEqual((self.made("LPB-270")["标题"], self.made("LPB-270")["执行方"]), ("LPB-270 我领的", ["我"]))
        self.jira.issues["LPB-268"]["owner"] = "me"            # the owner takes it: same row, no longer 没人领
        out = self.sync()
        self.assertEqual((out["edited"], out["closed"]), (1, 0))
        self.assertEqual(self.made("LPB-268")["标题"], "LPB-268 积分邮件")
        self.jira.issues["LPB-270"]["owner"] = "carl"          # handed to someone not followed: closed
        self.assertEqual(self.sync()["closed"], 1)

    def test_title_fields_go_in_front_of_the_title(self):
        from agent_matrix.config import save
        root = Path(self.tmp.name)
        save(root / "config.yaml", self.config)
        self.assertEqual(jira.title_fields(root, ["app plan", ""], api=self.jira), ["App Plan"])
        from agent_matrix.config import load
        self.config.jira_title_fields = load(root / "config.yaml").jira_title_fields   # both same-named fields
        self.jira.issues["LL-1"]["plan"] = "Pro"
        self.jira.issues["LL-2"].update(plan="TBD", plan_option="Advanced")       # TBD skipped, the other one used
        self.sync()
        self.assertEqual(self.made("LL-1")["标题"], "[Pro] LL-1 概要设计")
        self.assertEqual(self.made("LL-2")["标题"], "[Advanced] LL-2 安全合规")
        self.jira.issues["LL-1"]["plan"] = "N/A"
        self.sync()
        self.assertEqual(self.made("LL-1")["标题"], "LL-1 概要设计")

    def test_watch_projects_checks_each_key(self):
        from agent_matrix.config import load, save
        from agent_matrix.models import MatrixError
        root = Path(self.tmp.name)
        save(root / "config.yaml", self.config)
        self.assertEqual(jira.watch_projects(root, [" lgs", "LGS", ""], api=self.jira), {"projects": ["LGS"], "field": ""})
        self.assertEqual(jira.watch_projects(root, ["LGS"], "issue owner", api=self.jira)["field"], "Issue Owner")
        self.assertEqual(load(root / "config.yaml").jira_owner_field, {"id": "customfield_9", "name": "Issue Owner"})
        with self.assertRaisesRegex(MatrixError, "没有叫「Owner X」"):
            jira.watch_projects(root, ["LGS"], "Owner X", api=self.jira)
        with self.assertRaisesRegex(MatrixError, "没有项目 NOPE"):
            jira.watch_projects(root, ["LGS", "NOPE"], api=self.jira)
        self.assertEqual(load(root / "config.yaml").jira_unassigned_projects, ["LGS"])

    def test_sync_now_runs_one_round_under_the_lock(self):
        from agent_matrix.config import save
        root = Path(self.tmp.name)
        save(root / "config.yaml", self.config)
        fake = SimpleNamespace(root=root, config=self.config, auth=lambda: None, verify_owner=lambda a: None)
        with unittest.mock.patch("agent_matrix.feishu.Feishu", lambda c, r: fake):
            self.assertEqual(jira.sync_now(root, api=self.jira)["created"], 2)
            self.assertEqual(jira.sync_now(root, api=self.jira)["created"], 0)

    def test_follow_finds_one_person_by_name_or_email(self):
        from agent_matrix.config import load, save
        from agent_matrix.models import MatrixError
        root = Path(self.tmp.name)
        save(root / "config.yaml", self.config)
        (root / "state").mkdir()
        self.assertEqual(jira.follow(root, "amy@x.com", api=self.jira), [{"id": "a-amy", "name": "Amy Chen"}])
        jira.follow(root, "Amy Chen", api=self.jira)          # already there: no duplicate
        self.assertEqual(len(load(root / "config.yaml").jira_others), 1)
        with self.assertRaisesRegex(MatrixError, "好几个人"):
            jira.follow(root, "Am", api=self.jira)
        with self.assertRaisesRegex(MatrixError, "你自己"):
            jira.follow(root, "我自己", api=self.jira)
        self.assertEqual(jira.follow(root, remove="a-amy", api=self.jira), [])

    def test_connect_checks_with_jira_then_saves(self):
        from agent_matrix.config import load, save
        root = Path(self.tmp.name)
        save(root / "config.yaml", Config(item_store="device"))
        (root / "state").mkdir()
        who = jira.connect(root, "acme.atlassian.net/", " me@x.com ", "tok-1234",
                           api=lambda c, m, p, b=None: {"accountId": "a1", "displayName": "林"})
        self.assertEqual((who["site"], who["user"], who["tail"]), ("https://acme.atlassian.net", "林", "1234"))
        self.assertEqual(load(root / "config.yaml").jira_email, "me@x.com")
        with self.assertRaises(jira.MatrixError):   # the site is the owner's to fill in
            jira.connect(root, "", "me@x.com", "t", api=lambda *a, **k: {})

    def test_off_without_token(self):
        self.assertEqual(jira.sync(Config(), None, None, None), "no_token")


if __name__ == "__main__":
    unittest.main()
