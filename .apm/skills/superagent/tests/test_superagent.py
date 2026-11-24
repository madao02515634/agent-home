"""Run: uv run python -m unittest discover -s <skill>/tests"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sa.adaptors import ADAPTORS, LaunchRequest, get_adaptor  # noqa: E402
from sa.ade import ensure_watcher, notify  # noqa: E402
from sa.board import cli_cmd, post_message  # noqa: E402
from sa.hooks import stop_decision  # noqa: E402
from sa.store import Repo, parse_remote_key  # noqa: E402
from sa.watch import watch_tick  # noqa: E402
from superagent import main  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.proj = root / "home", root / "proj"
        self.home.mkdir()
        self.proj.mkdir()
        for d in (self.home, self.proj):
            subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.proj), "remote", "add", "origin", "git@github.com:own/demo.git"], check=True)
        self.env = {k: os.environ.pop(k, None) for k in ("SUPERAGENT_HOME", "HC_TERMINAL_ID", "ORCA_TERMINAL_HANDLE")}
        os.environ["SUPERAGENT_HOME"] = str(self.home)
        self.run_cli("init", "--use-defaults")
        self.run_cli("stage-new", "--stage", "s1", "--goal", "g", "--base", "HEAD")
        self.repo = Repo(self.proj)

    def tearDown(self) -> None:
        for k, v in self.env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> int:
        return main([argv[0], "--project", str(self.proj), *argv[1:]])

    def add_worker(self, wid: str, ade: str = "hc", marker: str | None = None) -> None:
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"][wid] = {"id": wid, "stage": "s1", "role": "impl", "ade": ade, "backend_ref": wid,
                                 "report_path": str(self.repo.stage_dir("s1") / f"{wid}.md"), "expected_marker": marker,
                                 "watch_logs": [], "status": "active", "last_progress_at": "2026-01-01T00:00:00Z",
                                 "last_read_seq": 0, "alert_count": 0}
            self.repo.save_members(m)

    def tick(self, now: str, probes: dict) -> dict:
        mock = Path(self.tmp.name) / "probe.json"
        mock.write_text(json.dumps(probes))
        return watch_tick(self.repo, Namespace(now=now, mock_probe=str(mock), auto_nudge=True))


class RemoteKey(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(parse_remote_key("git@github.com:own/demo.git"), "own-demo")
        self.assertEqual(parse_remote_key("https://gitlab.x/grp/sub/demo"), "grp-sub-demo")
        self.assertEqual(parse_remote_key("ssh://git@host:22/own/demo.git"), "own-demo")
        self.assertIsNone(parse_remote_key("not a url"))


class Board(Base):
    def test_store_location_and_ignore(self) -> None:
        self.assertEqual(self.repo.dir, self.home / ".superagent" / "own-demo")
        self.assertIn(".superagent/", (self.home / ".gitignore").read_text())

    def test_post_read_cursor_and_done(self) -> None:
        self.add_worker("w1")
        self.add_worker("w2")
        post_message(self.repo, "w1", ["router"], "done", "ok")
        post_message(self.repo, "router", ["all"], "note", "hi")
        m = self.repo.load_members()
        self.assertEqual(m["workers"]["w1"]["status"], "ready")
        # `all` expands to live workers other than the sender; w1 is ready, so still included.
        self.assertEqual(self.repo.read_board()[-1]["to"], ["w1", "w2"])
        self.assertEqual(self.run_cli("read", "--as", "w2"), 0)
        self.assertEqual(self.repo.load_members()["workers"]["w2"]["last_read_seq"], 2)

    def test_unknown_recipient_rejected(self) -> None:
        with self.assertRaises(ValueError):
            post_message(self.repo, "router", ["ghost"], "note", "x")

    def test_home_override_travels_with_commands(self) -> None:
        self.assertTrue(cli_cmd("read", self.repo, "--as", "w1").startswith(f"env SUPERAGENT_HOME={self.home}"))


    def test_watcher_does_not_need_router_on_hc(self) -> None:
        with self.repo.lock():
            m = self.repo.load_members()
            m["router"].update(ade="orca", ref="term_x")
            self.repo.save_members(m)
        res = ensure_watcher(self.repo, dry_run=True)
        self.assertTrue(res["started"])
        self.assertIn("--loop", res["command"])

class Watch(Base):
    def test_stopped_worker_nudged_once_then_alerts(self) -> None:
        self.add_worker("w1")
        probe = {"w1": {"agent_status": "idle", "screen_text": "same"}}
        self.tick("2026-01-01T00:00:00Z", probe)  # first probe counts as progress
        r = self.tick("2026-01-01T00:04:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "AUTO_NUDGED")
        self.assertTrue(r["events"][0]["nudge_result"]["dry_run"])
        r = self.tick("2026-01-01T00:08:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "STOPPED_NO_POST")
        self.assertTrue(r["events"][0]["should_notify"])
        self.assertEqual(self.repo.read_board()[-1]["kind"], "alert")

    def test_marker_without_post_is_posted_for_worker(self) -> None:
        self.add_worker("w1", marker="IMPL DONE")
        Path(self.repo.load_members()["workers"]["w1"]["report_path"]).write_text("x\nIMPL DONE\n")
        self.tick("2026-01-01T00:00:00Z", {})
        last = self.repo.read_board()[-1]
        self.assertEqual((last["from"], last["kind"], last["by"]), ("w1", "done", "watch"))
        self.assertEqual(self.repo.load_members()["workers"]["w1"]["status"], "ready")

    def test_router_unread_alerts_once_per_seq(self) -> None:
        self.add_worker("w1", ade="native")
        post_message(self.repo, "w1", ["router"], "ask", "?")
        later = "2099-01-01T00:00:00Z"
        events = [e["event"] for e in self.tick(later, {})["events"]]
        self.assertIn("ROUTER_UNREAD", events)
        events = [e["event"] for e in self.tick(later, {})["events"]]
        self.assertNotIn("ROUTER_UNREAD", events)

    def test_silent_hang(self) -> None:
        self.add_worker("w1")
        probe = {"w1": {"agent_status": "working", "screen_text": "same"}}
        self.tick("2026-01-01T00:00:00Z", probe)
        r = self.tick("2026-01-01T00:11:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "SILENT_HANG")
        self.assertTrue(Path(r["events"][0]["incident_path"]).is_file())


class Stage(Base):
    def test_reset_cap_halts_dispatch(self) -> None:
        self.assertEqual(self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "a"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "b"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "c"), 2)
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run"), 1)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--owner-ack", "ok"), 0)
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run"), 0)


class Roles(Base):
    def test_roles_start_empty_and_are_created_per_task(self) -> None:
        self.assertEqual(self.repo.load_config()["roles"], {})
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "migration", "--dry-run"), 1)
        self.assertEqual(self.run_cli("set-role", "--role", "migration"), 1)  # a new role needs --kind
        self.assertEqual(self.run_cli("set-role", "--role", "Bad Name", "--kind", "write", "--confirm"), 1)
        args = ["set-role", "--role", "perf-review", "--kind", "review", "--cli", "agy", "--model", "gemini-3.8-pro",
                "--purpose", "效能審查"]
        self.assertEqual(self.run_cli(*args), 3)  # only a proposal until the Owner confirms
        self.assertNotIn("perf-review", self.repo.load_config()["roles"])
        self.assertEqual(self.run_cli(*args, "--confirm"), 0)
        role = self.repo.load_config()["roles"]["perf-review"]
        self.assertEqual((role["kind"], role["ade"], role["cli"], role["model"], role["effort"], role["purpose"]),
                         ("review", "hc", "agy", "gemini-3.8-pro", "high", "效能審查"))
        self.assertEqual(self.run_cli("set-role", "--role", "perf-review", "--effort", "low"), 0)  # update keeps kind
        self.assertEqual(self.repo.load_config()["roles"]["perf-review"]["cli"], "agy")
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "perf-review", "--dry-run"), 0)

    def test_kind_defaults_follow_config(self) -> None:
        cfg = self.repo.load_config()
        cfg["kinds"]["write"]["model"] = "sonnet"
        self.repo.config.write_text(json.dumps(cfg))
        self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm")
        self.assertEqual(self.repo.load_config()["roles"]["impl"]["model"], "sonnet")


class StopHook(Base):
    def decide(self, wid: str = "w1") -> str | None:
        with self.repo.lock():
            return stop_decision(self.repo, wid)

    def test_blocks_until_worker_posts(self) -> None:
        self.add_worker("w1")
        post_message(self.repo, "router", ["w1"], "brief", "do it", push=False)
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["w1"]["last_read_seq"] = 1  # dispatch starts the cursor past the brief
            self.repo.save_members(m)
        self.assertIn("--kind done", self.decide())
        self.assertIsNone(self.decide())  # same board state is blocked only once
        post_message(self.repo, "w1", ["router"], "ask", "A or B?", push=False)
        self.assertIsNone(self.decide())  # waiting on the Router is a legal stop
        post_message(self.repo, "router", ["w1"], "answer", "A", push=False)
        self.assertIn("read", self.decide())  # unread answer comes first
        self.run_cli("read", "--as", "w1")
        self.assertIn("--kind done", self.decide())  # read but still owes a post
        post_message(self.repo, "w1", ["router"], "done", "ok", push=False)
        self.assertIsNone(self.decide())

    def test_claude_hc_worker_gets_settings(self) -> None:
        self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm")
        out = Path(self.tmp.name) / "d.json"
        self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run", "--output", str(out))
        self.assertIn("--settings", json.loads(out.read_text())["command"])


class Screen(unittest.TestCase):
    def test_add_dir_trust_prompt_is_interactive(self) -> None:
        from sa.screen import detect_interactive_block
        lines = [" Adding a new directory:", " /tmp/x", " This session hasn't worked here before. Is this a directory you created or one you trust?",
                 " ❯ No, exit", "   Yes, add it"]
        self.assertEqual(detect_interactive_block(lines)["pattern"], "trust_or_auth")


class Adaptors(unittest.TestCase):
    def req(self, **kw: object) -> LaunchRequest:
        base = dict(wid="w", role="impl", stage="s1", workdir=Path("/p"), prompt_file=Path("/s/w.prompt.md"),
                    instructions="do it", cli="claude", model="opus", effort="high")
        return LaunchRequest(**{**base, **kw})  # type: ignore[arg-type]

    def test_every_adaptor_launches(self) -> None:
        for name, adaptor in ADAPTORS.items():
            plan = adaptor.launch(self.req())
            self.assertTrue(plan.command or plan.native_spawn, name)

    def test_hc_label_only_with_space(self) -> None:
        self.assertNotIn("--label", get_adaptor("hc").launch(self.req()).command)
        cmd = get_adaptor("hc").launch(self.req(herdr_space="sp1")).command
        self.assertIn("herdr.space=sp1", cmd)
        self.assertIn("--outlive-parent", cmd)

    def test_orca_rules(self) -> None:
        orca = get_adaptor("orca")
        self.assertEqual(orca.validate(None, "high"), "ORCA_EFFORT_NEEDS_MODEL")
        cmd = orca.launch(self.req(cli="agy", branch="feat/x")).command
        self.assertIn("antigravity", cmd)
        self.assertIn("new-child", cmd)
        self.assertEqual(orca.parse_ref('{"result": {"dispatch": {"dispatchId": "d1"}}}', "w"), "d1")
        self.assertNotIn("--effort", orca.launch(self.req(effort="none")).command)
        self.assertNotIn("--effort", get_adaptor("hc").launch(self.req(effort="none")).command)

    def test_native_has_no_push_or_probe(self) -> None:
        native = get_adaptor("native")
        self.assertFalse(native.can_push or native.can_probe or native.needs_pinned_model)
        self.assertFalse(notify("native", "x", "hi")["sent"])
        with self.assertRaises(ValueError):
            get_adaptor("tmux")


if __name__ == "__main__":
    unittest.main()
