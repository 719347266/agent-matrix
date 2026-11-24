"""The AI employee: background pack, self-check, one conversation per item, cards updated in place."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from zoneinfo import ZoneInfo

from agent_matrix import engines
from agent_matrix.models import Config

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 17, 15, 0, tzinfo=TZ)


def row(n=44, **extra):
    return {"record_id": f"rec{n}", "编号": n, "标题": "投流方案", "执行方": ["AI"], "状态": ["进行中"], **extra}


GOOD = textwrap.dedent("""\
    # 投流方案

    - 先投 25-35 岁一线城市女性，预算 60% 给信息流
    - 第一周日预算 800 元，看点击成本再加
    - 素材用三套：痛点、口碑、限时福利

    ## 推荐
    先做人群包 A，再测 B。人群包 A 是过去三个月咨询过的用户相似人群，转化最高，成本也最稳。

    ## 选项对比
    | 方案 | 成本 | 风险 |
    |---|---|---|
    | A | 低 | 低 |
    | B | 中 | 中 |

    ## 需要你拍板
    第一周预算是否按 800 元/天。

    ## 用到的资料
    - 会议原话 11-12 周会

    ## 我做的假设
    - 预算上限按上个月实际花费估算
    """)


class BackgroundTests(unittest.TestCase):
    def test_task_sheet_puts_the_owners_words_first(self):
        from agent_matrix.background import task_sheet
        parent = {"record_id": "rec30", "编号": 30, "标题": "投放方案撰写"}
        text = task_sheet(row(补充材料="11-17 用上周的点击数据", 退回意见="11-17 少了预算", 父事项=[{"id": "rec30"}]), [parent])
        self.assertIn("本人补充", text)
        self.assertIn("用上周的点击数据", text)
        self.assertIn("本人退回意见", text)
        self.assertIn("#30", text)
        self.assertIn("没写", text)  # no done criterion: the employee sets a sensible one and says so

    def test_transcript_excerpt_centers_on_the_promise(self):
        from agent_matrix.background import transcript_excerpt
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "state/meeting-transcripts/m1"
            d.mkdir(parents=True)
            lines = [f"[00:{i:02d}:00] 说话人1：第 {i} 句" for i in range(200)]
            lines[150] = "[00:150:00] 说话人1：投流方案我来写"
            (d / "transcript.md").write_text("\n".join(lines))
            r = row(去重键="meeting:m1:abc", 原话引用="[00:150:00] 投流方案我来写")
            text, path = transcript_excerpt(tmp, r)
            self.assertIn("投流方案我来写", text)
            self.assertIn("第 100 句", text)
            self.assertNotIn("第 10 句\n", text)
            self.assertTrue(str(path).endswith("transcript.md"))
            self.assertEqual(transcript_excerpt(tmp, row()), (None, None))

    def test_pack_without_sources_tells_the_model_to_dig(self):
        from agent_matrix.background import build
        with tempfile.TemporaryDirectory() as tmp:
            text = build(tmp, None, [row(), row(45, 标题="投流方案复盘", 交付物链接="https://x.feishu.cn/docx/a")], row(), NOW)
        self.assertIn("#45", text)          # similar item with its deliverable
        self.assertIn("lark-cli docs +fetch", text)


class CheckResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc, result):
        (self.dir / "交付.md").write_text(doc)
        (self.dir / "结果.json").write_text(json.dumps(result, ensure_ascii=False))

    def test_good_delivery_passes(self):
        from agent_matrix.worker import check_result
        self.write(GOOD, {"outcome": "done", "template": "方案", "summary": "先投人群包A，日预算800", "sources": ["会议"]})
        self.assertIsNone(check_result(self.dir, row())[1])

    def test_missing_assumptions_section_is_sent_back(self):
        from agent_matrix.worker import check_result
        self.write(GOOD.split("## 我做的假设")[0], {"outcome": "done", "template": "方案", "summary": "x", "sources": ["a"]})
        self.assertIn("我做的假设", check_result(self.dir, row())[1])

    def test_placeholders_and_missing_template_sections_are_sent_back(self):
        from agent_matrix.worker import check_result
        self.write(GOOD.replace("## 需要你拍板", "## 待定").replace("800 元/天", "XX 元/天"),
                   {"outcome": "done", "template": "方案", "summary": "x", "sources": ["a"]})
        self.assertIn("拍板", check_result(self.dir, row())[1])
        self.write(GOOD.replace("800 元/天", "TODO"), {"outcome": "done", "template": "方案", "summary": "x", "sources": ["a"]})
        self.assertIn("TODO", check_result(self.dir, row())[1])

    def test_blocked_must_say_what_was_tried(self):
        from agent_matrix.worker import check_result
        self.write("", {"outcome": "blocked", "question": "上周点击数据在哪？"})
        self.assertIn("tried", check_result(self.dir, row())[1])
        self.write("", {"outcome": "blocked", "question": "上周点击数据在哪？", "tried": "搜了文档和群聊"})
        self.assertIsNone(check_result(self.dir, row())[1])


class ConversationTests(unittest.TestCase):
    def test_only_new_owner_lines_are_passed_on(self):
        from agent_matrix.worker import _changes, _remember, resume_prompt
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            first = row(补充材料="11-16 用上周数据")
            self.assertEqual(_changes(d, first), ["补充材料：11-16 用上周数据"])
            _remember(d, first)
            later = row(补充材料="11-16 用上周数据\n11-17 加上竞品", 退回意见="11-17 少了预算")
            changes = _changes(d, later)
            self.assertEqual(sorted(changes), ["补充材料：11-17 加上竞品", "退回意见：11-17 少了预算"])
            self.assertIn("加上竞品", resume_prompt(changes, False))
            self.assertIn("时间到了", resume_prompt([], True))

    def test_first_prompt_renders(self):
        from agent_matrix.worker import first_prompt
        root = Path(__file__).resolve().parents[1]
        text = first_prompt(root, row())
        self.assertIn("#44 投流方案", text)
        self.assertIn('"outcome": "done"', text)
        self.assertIn("方案：推荐", text)

    def test_runner_keeps_the_thread_and_mirrors_steps(self):
        """A stand-in for codex: prints the JSON events and writes step.txt, like the real one."""
        from agent_matrix.worker import CodexRun
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_codex.py"
            fake.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, sys, pathlib
                prompt = sys.stdin.read()
                args = sys.argv[1:]
                thread = args[args.index("resume") + 1] if "resume" in args else "thread-1"
                print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
                pathlib.Path("step.txt").write_text("2/4 查文档")
                pathlib.Path("seen_prompt.txt").write_text(prompt)
                print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}}), flush=True)
                """))
            fake.chmod(0o755)
            work = Path(tmp) / "work"
            work.mkdir()
            config = Config(worker_codex=str(fake))
            from unittest.mock import patch
            original_argv = CodexRun.argv
            def script_argv(run):
                return [sys.executable, str(fake), *original_argv(run)[1:]]
            runner_patch = patch.object(CodexRun, "argv", script_argv)
            runner_patch.start()
            self.addCleanup(runner_patch.stop)
            steps = []
            run = CodexRun(config, work).run("开始", on_tick=lambda: steps.append((work / "step.txt").read_text()
                                                                                if (work / "step.txt").exists() else None))
            self.assertEqual(run.thread, "thread-1")
            self.assertEqual((work / ".thread").read_text(), "thread-1")
            self.assertEqual(run.usage["input_tokens"], 100)
            self.assertIn("2/4 查文档", steps)
            again = CodexRun(config, work, thread="thread-1")
            self.assertIn("resume", again.argv())
            again.run("本人补充：加上竞品")
            self.assertEqual((work / "seen_prompt.txt").read_text(), "本人补充：加上竞品")


    def test_claude_runner_keeps_its_own_session_and_last_reply(self):
        """A stand-in for `claude -p`: the event shapes are copied from a real stream-json run."""
        from agent_matrix.worker import ClaudeRun, engine
        from agent_matrix import thread as conversation
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_claude.py"
            fake.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, sys, pathlib
                prompt = sys.stdin.read()
                args = sys.argv[1:]
                sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-1"
                print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}), flush=True)
                pathlib.Path("seen_prompt.txt").write_text(prompt)
                pathlib.Path("seen_args.txt").write_text(json.dumps(args))
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "好了"}]}}), flush=True)
                print(json.dumps({"type": "result", "subtype": "success", "result": "交付写好了", "session_id": sid,
                                  "usage": {"input_tokens": 6, "output_tokens": 188}}), flush=True)
                """))
            fake.chmod(0o755)
            work = Path(tmp) / "work"; work.mkdir()
            (work / ".thread").write_text("gpt-thread")          # GPT's conversation is left alone
            config = Config(worker_claude=str(fake), worker_claude_model="sonnet")
            from unittest.mock import patch
            original_argv = ClaudeRun.argv
            with patch.object(ClaudeRun, "argv", lambda run: [sys.executable, str(fake), *original_argv(run)[1:]]):
                run = ClaudeRun(config, work).run("开始")
                self.assertEqual((run.thread, run.commands, run.messages), ("sess-1", 1, 1))
                self.assertEqual((work / ".claude-session").read_text(), "sess-1")
                self.assertEqual((work / ".thread").read_text(), "gpt-thread")
                self.assertEqual((work / ".last.txt").read_text(), "交付写好了")
                self.assertEqual(run.usage["output_tokens"], 188)
                args = json.loads((work / "seen_args.txt").read_text())
                for flag in ("--safe-mode", "--permission-mode", "dontAsk", "--model", "sonnet", "--effort", "high"):
                    self.assertIn(flag, args)
                ClaudeRun(config, work, thread="sess-1").run("本人补充：加上竞品")
                self.assertEqual((work / "seen_prompt.txt").read_text(), "本人补充：加上竞品")
                self.assertIn("--resume", json.loads((work / "seen_args.txt").read_text()))
            with_mcp = ClaudeRun(Config(worker_claude=str(fake), worker_mcp_tools=["mcp__claude_ai_Notion__notion-fetch", "rm -rf"]), work).argv()
            self.assertNotIn("--safe-mode", with_mcp)               # safe mode would switch every MCP server off
            self.assertEqual(with_mcp[with_mcp.index("--setting-sources") + 1], "user,project")  # where MCP servers live
            self.assertTrue(json.loads(with_mcp[with_mcp.index("--settings") + 1])["disableAllHooks"])  # the owner's hooks stay off
            allowed = with_mcp[with_mcp.index("--allowedTools") + 1]
            self.assertIn("mcp__claude_ai_Notion__notion-fetch", allowed)
            self.assertNotIn("rm -rf", allowed)                      # only mcp__ names get through
            from agent_matrix import worker
            listed = ("claude.ai Notion: https://mcp.notion.com/mcp - ✔ Connected\n"
                      "claude.ai Apollo.io: https://mcp.apollo.io/mcp - ! Needs authentication\n"
                      "plugin:chrome-devtools-mcp:chrome-devtools: npx chrome-devtools-mcp@latest - ✔ Connected\n"
                      "pencil: /x/mcp-server --app cursor - ✔ Connected\n")
            fake_run = lambda *a, **k: type("R", (), {"stdout": listed})()
            servers = worker.mcp_servers("claude", run=fake_run, cache=Path(tmp) / "mcp.json")
            self.assertEqual(servers, ["claude_ai_Notion", "pencil", "plugin_chrome-devtools-mcp_chrome-devtools"])  # connected only
            with patch.object(engines, "mcp_servers", lambda binary: servers):
                every = ClaudeRun(Config(worker_claude=str(fake), worker_mcp_tools=["*"]), work).argv()
            self.assertIn("mcp__pencil__* mcp__plugin_chrome-devtools-mcp_chrome-devtools__*", every[every.index("--allowedTools") + 1])
            self.assertIn("mcp__*send*", every[every.index("--disallowedTools") + 1])  # never sends on its own
            self.assertIn("Bash(lark-cli im +messages-send:*)", every[every.index("--disallowedTools") + 1])
            plain = ClaudeRun(Config(worker_claude=str(fake)), work).argv()          # without MCP too
            self.assertIn("Bash(lark-cli im +messages-reply:*)", plain[plain.index("--disallowedTools") + 1])
            self.assertNotIn("claude_ai_Notion", every[every.index("--allowedTools") + 1])  # plugins + local MCP only
            self.assertEqual(ClaudeRun(Config(worker_mcp_tools=["*"]), work).env()["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
            root = Path(tmp) / "root"
            self.assertEqual(engine(root).NAME, "本机 GPT")           # default stays GPT
            conversation.set_setting(root, "engine", "claude")
            self.assertIs(engine(root), ClaudeRun)


    def test_dev_mode_waits_for_the_owner_before_each_asked_step(self):
        """开发模式: the fake speaks claude's stdio permission protocol (shapes from a real run); the owner allows one
        step and refuses the other in 心响, and each answer goes back to the same request."""
        import threading, time
        from unittest.mock import patch
        from agent_matrix import approvals
        from agent_matrix.worker import ClaudeRun
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_claude.py"
            fake.write_text(textwrap.dedent("""\
                import json, sys, pathlib
                prompt = json.loads(sys.stdin.readline())["message"]["content"]
                print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
                seen = []
                for rid, cmd in (("r1", "git push"), ("r0", "cd ~/x && ls | head -3"), ("r2", "npm publish")):
                    print(json.dumps({"type": "control_request", "request_id": rid, "request": {"subtype": "can_use_tool",
                          "tool_name": "Bash", "input": {"command": cmd}, "description": "收尾"}}), flush=True)
                    seen.append(json.loads(sys.stdin.readline())["response"])
                pathlib.Path("seen.json").write_text(json.dumps({"prompt": prompt, "answers": seen, "args": sys.argv[1:]}))
                print(json.dumps({"type": "result", "result": "完", "usage": {}}), flush=True)
                sys.stdin.read()   # exits only once the driver closes stdin
                """))
            root, work = Path(tmp) / "root", Path(tmp) / "work"
            work.mkdir()
            guard = {"root": root, "mode": "risky", "record_id": "rec1"}
            from agent_matrix import thread
            thread.set_setting(root, "dev_mode", "risky")   # read again per request, not only at start

            def owner():
                answered = set()
                for _ in range(200):
                    for a in approvals.pending(root):
                        approvals.decide(root, a["id"], a["what"] == "git push", "别删")
                        answered.add(a["id"])
                    if len(answered) == 2:
                        return
                    time.sleep(0.05)
            threading.Thread(target=owner, daemon=True).start()
            original_argv = ClaudeRun.argv
            with patch.object(ClaudeRun, "argv", lambda run: [sys.executable, str(fake), *original_argv(run)[1:]]):
                run = ClaudeRun(Config(worker_claude=str(fake)), work, guard=guard, timeout_minutes=1).run("开始")
            self.assertFalse(run.timed_out)
            seen = json.loads((work / "seen.json").read_text())
            self.assertEqual(seen["prompt"], "开始")
            allow, plain, deny = seen["answers"]
            self.assertEqual((plain["request_id"], plain["response"]["behavior"]), ("r0", "allow"))   # not risky: no ask
            self.assertEqual((allow["request_id"], allow["response"]["behavior"], allow["response"]["updatedInput"]),
                             ("r1", "allow", {"command": "git push"}))
            self.assertEqual((deny["request_id"], deny["response"]["behavior"]), ("r2", "deny"))
            self.assertIn("别删", deny["response"]["message"])
            args = seen["args"]
            self.assertEqual(args[args.index("--permission-mode") + 1], "default")
            self.assertIn("--permission-prompt-tool", args)
            settings = json.loads(args[args.index("--settings") + 1])
            self.assertIn("Bash(git push:*)", settings["permissions"]["ask"])
            self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"])   # sandboxed Bash would skip the ask
            every = ClaudeRun(Config(worker_claude=str(fake)), work, guard={**guard, "mode": "every"}).argv()
            self.assertNotIn("--allowedTools", every)                            # nothing pre-allowed
            self.assertEqual(approvals.pending(root), [])
            self.assertTrue(approvals.needs_owner(root, "Bash", {"command": "ls && FOO=1 git push -f"}))
            self.assertFalse(approvals.needs_owner(root, "Bash", {"command": "git status; echo rmdir-ish"}))
            self.assertFalse(approvals.needs_owner(root, "Edit", {"file_path": "a.py"}))
            ask = lambda cmd: approvals.needs_owner(root, "Bash", {"command": cmd})
            # only what reaches beyond this computer asks; local work (even rm -rf, git reset) runs
            for cmd in ("d=$TMPDIR/tk; rm -rf $d; mkdir -p $d && cd $d && npm pack x@1 | tail -2", "rm -rf .",
                        "git reset --hard; git clean -fd; git branch -D old", "chmod +x a.sh; kill 123"):
                self.assertFalse(ask(cmd), cmd)
            # hidden in loops, $(…), `…`, bash -c, xargs: still seen
            for cmd in ("for f in a b; do git push origin $f; done", "x=$(aws s3 rm s3://b/k)", "echo $(gh pr merge 3)",
                        "echo `git push`", "bash -c 'npm publish'", "if true; then kubectl delete pod x; fi",
                        "ls | xargs gh pr merge", "nohup git push &"):
                self.assertTrue(ask(cmd), cmd)
            self.assertFalse(ask("for i in 1 2; do aws codebuild batch-get-builds --ids a; sleep 30; done"))   # polling
            self.assertFalse(ask("ids=$(aws codebuild list-builds-for-project --project-name p --query ids --output text)"))
            self.assertFalse(ask("aws configure list 2>&1"))
            self.assertTrue(ask("aws configure get aws_secret_access_key"))
            self.assertTrue(ask("git push origin dev"))
            # aws: reading is fine, changing or reading secrets asks
            self.assertFalse(ask("cat out.txt; aws codebuild batch-get-builds --ids x:1 --query 'builds[0].{s:buildStatus}' --output json"))
            self.assertFalse(ask("aws --region us-east-1 logs tail /aws/x; aws s3 ls s3://b/; aws sts get-caller-identity"))
            self.assertTrue(ask("aws codebuild start-build --project-name x"))
            self.assertTrue(ask("aws --debug s3 rm list-thing"))             # a global flag does not hide the operation
            self.assertTrue(ask("aws secretsmanager get-secret-value --secret-id prod/db"))
            self.assertTrue(ask("aws ssm get-parameter --name p --with-decryption"))
            # 只读都放行: kubectl / docker / helm / gcloud / terraform / SQL / redis
            for cmd in ("kubectl -n prod get pods -o wide | grep api", "kubectl logs deploy/api --tail 50",
                        "kubectl config current-context", "docker ps -a; docker compose logs web", "docker image ls",
                        "helm -n x list; helm status r", "gcloud compute instances list --project p",
                        "terraform plan -out p; terraform state list", "terraform fmt -check",
                        'mysql -h db -e "select id from t limit 3"', "psql -c 'SHOW search_path'", "redis-cli -h r GET k"):
                self.assertFalse(ask(cmd), cmd)
            for cmd in ("kubectl delete pod x", "kubectl -n get delete pod x", "kubectl exec -it x -- sh",
                        "kubectl get secret db -o yaml", "docker rm -f x", "docker compose up -d", "helm upgrade r c",
                        "gcloud compute instances delete x", "gcloud auth print-access-token", "terraform apply",
                        'mysql -e "select 1; drop table t"', "mysql -e 'update t set a=1'", "psql -f migrate.sql",
                        "mysql db < dump.sql", "mysql -h db", "redis-cli DEL k", "redis-cli -h r FLUSHALL",
                        "ssh host ls", "mongosh --eval 'db.x.find()'"):
                self.assertTrue(ask(cmd), cmd)
            # gh: reading is fine, writing outside asks
            self.assertFalse(ask("gh auth token >/dev/null 2>&1 && echo ok; gh pr view 3 --json state; gh pr checks 3"))
            self.assertTrue(ask("gh pr create --base dev --fill"))
            self.assertTrue(ask("gh api -X POST repos/o/r/issues"))
            # an MCP send the owner let through still waits for the owner in 开发模式; reads do not
            self.assertTrue(approvals.needs_owner(root, "mcp__plugin_slack_slack__slack_send_message", {"channel_id": "C1"}))
            self.assertFalse(approvals.needs_owner(root, "mcp__plugin_slack_slack__slack_read_channel", {}))
            thread.set_setting(root, "dev_mode", "every")   # switched mid-run: everything asks again
            self.assertTrue(approvals.needs_owner(root, "Bash", {"command": "ls"}))


class CardTests(unittest.TestCase):
    def test_tapped_button_becomes_a_result_line(self):
        from agent_matrix.brief_v1 import _block, _row_item, card
        from agent_matrix.cards import mark, short_result
        c = card("AI 做完了", "", "green", [_block("做完了", "green", [
            _row_item(("#44 投放方案", "", "grey", None), "https://x.feishu.cn/docx/a", cmd="44 过", cmd_label="通过"),
            _row_item(("#45 数据看板", "", "grey", None), "https://x.feishu.cn/docx/b", cmd="45 过", cmd_label="通过")])],
                 "hint", None)
        text, ok = short_result("#44 投放方案 → 完成 ✓", NOW)
        self.assertEqual((text, ok), ("✓ 已完成 15:00", True))
        new, replaced = mark(c, "44 过", text, ok)
        self.assertTrue(replaced)
        dumped = json.dumps(new, ensure_ascii=False)
        self.assertIn("✓ 已完成 15:00", dumped)
        self.assertIn('"cmd": "45 过"', dumped)       # the other item keeps its button
        self.assertNotIn('"cmd": "44 过"', dumped)
        self.assertIn("docx/a", dumped)               # and the「打开」link stays
        self.assertEqual(short_result("#44 现在是进行中，还没交付，不能验收。", NOW)[1], False)

    def test_cards_are_remembered_by_message_id(self):
        from agent_matrix.cards import load, remember
        with tempfile.TemporaryDirectory() as tmp:
            remember(tmp, "om_abc123", {"body": {"elements": []}})
            self.assertEqual(load(tmp, "om_abc123"), {"body": {"elements": []}})
            self.assertIsNone(load(tmp, "../x"))
            self.assertEqual(load(tmp, "om_missing", json.dumps({"body": {}, "schema": "2.0"}))["schema"], "2.0")


class ReplyVerbTests(unittest.TestCase):
    def test_first_and_resume(self):
        from agent_matrix.reply import parse
        from tests.test_v1_flow import TODAY, applier
        ops, _ = parse("44 先做\n45 继续", TODAY)
        self.assertEqual([o.verb for o in ops], ["first", "resume"])

        class Kv(dict):
            def get(self, k, d=None):
                return super().get(k, d)

            def set(self, k, v):
                self[k] = v
        a = applier([row(44), row(45, 状态=["卡住"], 卡点="缺数据")])
        a.state = Kv()
        out = [x for op in ops for x in a.apply(op)]
        self.assertIn("下一件就做", out[0])
        self.assertEqual(a.state["work:first"], ["rec44"])  # 44 jumps the queue; 45 继续 keeps its place
        r45 = a.gateway.data["rec45"]
        self.assertEqual(r45["状态"], ["进行中"])
        self.assertIn("继续做", r45["补充材料"])
        a.apply(parse("45 先做", TODAY)[0][0])
        self.assertEqual(a.state["work:first"], ["rec45", "rec44"])
        b = applier([row(46, 执行方=["我"])])
        self.assertIn("不是 AI", b.apply(parse("46 先做", TODAY)[0][0])[0])


class PanelTests(unittest.TestCase):
    def test_one_item_in_progress_and_the_rest_queued(self):
        import os
        from agent_matrix.monitor import worker_view
        from agent_matrix.worker import write_status
        with tempfile.TemporaryDirectory() as tmp:
            write_status(tmp, running=True, pid=os.getpid(), kind="item", record_id="rec44", step="3/4 写方案",
                         item_started=NOW.replace(minute=0).isoformat(), queue=[{"record_id": "rec45"}, {"record_id": "rec46"}])
            moving = [{"n": n, "label": "AI 在做", "tone": "blue", "note": ""} for n in (46, 45, 44)]
            rows = [{"record_id": f"rec{n}", "编号": n} for n in (44, 45, 46)]
            info = worker_view(tmp, rows, moving, NOW.replace(minute=6))
            self.assertEqual(info["line"], "AI 员工：正在做 #44 · 3/4 写方案 · 6 分钟")
            self.assertEqual([q["n"] for q in info["queue"]], [45, 46])
            self.assertEqual([(x["n"], x["label"]) for x in moving], [(44, "AI 正在做"), (45, "排队第 1"), (46, "排队第 2")])
            write_status(tmp, running=False)
            self.assertEqual(worker_view(tmp, rows, [], NOW)["state"], "idle")


if __name__ == "__main__":
    unittest.main()


class EnvironmentTests(unittest.TestCase):
    """GPT's command tool crashes on non-ASCII paths, so its folder and environment stay ASCII."""

    def test_environment_has_no_non_ascii_paths(self):
        import os
        from unittest.mock import patch
        from agent_matrix.worker import _env
        fake = {"PATH": "/usr/bin:/Users/me/01 项目/x/.venv/bin", "PWD": "/Users/me/01 项目", "HOME": "/Users/me",
                "CODEX_HOME": "/elsewhere", "VIRTUAL_ENV": "/v"}
        with patch.dict(os.environ, fake, clear=True):
            env = _env(Config())
        self.assertNotIn("项目", env["PATH"])
        self.assertIn("/usr/bin", env["PATH"])
        self.assertNotIn("PWD", env)
        self.assertNotIn("CODEX_HOME", env)
        self.assertEqual(env["HOME"], "/Users/me")

    def test_work_folder_moves_out_of_a_non_ascii_project(self):
        from agent_matrix.worker import work_base
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            self.assertEqual(work_base(plain), plain.resolve() / "state/work")
            fancy = Path(tmp) / "01 项目"
            fancy.mkdir()
            self.assertTrue(str(work_base(fancy)).isascii())


class PublishOnAsciiPathTests(unittest.TestCase):
    """On a plain-ASCII project path the AI's folder is the project's own copy; publishing must not
    try to copy each file onto itself (it raised SameFileError and nothing could ever be delivered)."""

    def test_delivery_publishes_when_work_folder_is_the_kept_copy(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from agent_matrix import itemwork, thread
        with tempfile.TemporaryDirectory(prefix="ascii") as tmp:
            root = Path(tmp)
            item = itemwork.Item(root, Config(), {"record_id": "recA1", "input_version": "v" * 64, "title": "周报"})
            self.assertEqual(item.workdir.resolve(), itemwork.record_copy(root, "recA1").resolve())
            (item.workdir / "交付.md").write_text("# 周报\n\n## 我做的假设\n- 按本周口径\n", encoding="utf-8")
            (item.workdir / "结果.json").write_text("{}", encoding="utf-8")

            @contextmanager
            def project(*a, **k):
                yield None, None

            with patch.object(itemwork, "project", project), \
                    patch("agent_matrix.prework.publish_markdown", return_value={"url": "https://x/docx/a"}), \
                    patch("agent_matrix.prework.notify"), patch("agent_matrix.prework.add_note"):
                out = item.publish({"summary": "本周完成三件", "confirmation": ""}, 0)
            self.assertEqual(out["result"], "published")
            self.assertEqual((item.workdir / "交付.md").read_text(encoding="utf-8").splitlines()[0], "# 周报")
            self.assertEqual(thread.history(root, ["recA1"])["recA1"]["deliverables"][0]["url"], "https://x/docx/a")


class McpLetThroughTests(unittest.TestCase):
    def test_only_the_named_send_tool_escapes_the_ban(self):
        from agent_matrix.engines import MCP_DENY, mcp_deny
        self.assertEqual(mcp_deny(["plugin_slack_slack", "plugin_Notion_notion"]), MCP_DENY)
        deny = mcp_deny(["plugin_slack_slack", "plugin_Notion_notion"], ["mcp__plugin_slack_slack__slack_send_message"]).split()
        self.assertNotIn("mcp__*send*", deny)
        self.assertIn("mcp__plugin_Notion_notion__*send*", deny)          # other servers still cannot send
        self.assertNotIn("mcp__plugin_slack_slack__*send*", deny)
        self.assertIn("mcp__*delete*", deny)                               # every other word stays global
        self.assertIn("mcp__*schedule_message*", deny)
