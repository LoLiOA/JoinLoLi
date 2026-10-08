#!/usr/bin/env python3
"""join_loli.py 的本地回归测试: 用标准库起一个假的 GitHub API, 不联网。"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "join_loli.py"

_spec = importlib.util.spec_from_file_location("join_loli", SCRIPT)
assert _spec and _spec.loader
join_loli = importlib.util.module_from_spec(_spec)
sys.modules["join_loli"] = join_loli
_spec.loader.exec_module(join_loli)


class State:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.members: set[str] = set()
        self.invitations: list[dict] = []
        self.comments: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.issue: dict = {}
        self.invite_status = 201


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, code: int, payload: object | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self.state.requests.append(("GET", path))

        match = re.fullmatch(r"/users/([^/]+)", path)
        if match:
            user = self.state.users.get(match.group(1).lower())
            return self._send(200, user) if user else self._send(404, {"message": "Not Found"})

        match = re.fullmatch(r"/orgs/([^/]+)/members/([^/]+)", path)
        if match:
            if match.group(2).lower() in self.state.members:
                return self._send(204)
            return self._send(404, {"message": "Not Found"})

        if re.fullmatch(r"/orgs/([^/]+)/invitations", path):
            return self._send(200, self.state.invitations)

        if re.fullmatch(r"/repos/.+/issues/\d+", path):
            return self._send(200, self.state.issue)

        self._send(404, {"message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.state.requests.append(("POST", path))

        if re.fullmatch(r"/orgs/([^/]+)/invitations", path):
            if self.state.invite_status != 201:
                return self._send(
                    self.state.invite_status, {"message": "You must be an admin of the organization"}
                )
            invitee_id = payload.get("invitee_id")
            login = next(
                (u["login"] for u in self.state.users.values() if u["id"] == invitee_id), "unknown"
            )
            self.state.invitations.append({"invitee": {"login": login, "id": invitee_id}})
            return self._send(201, {"id": 1, "login": login})

        if re.fullmatch(r"/repos/.+/issues/\d+/comments", path):
            self.state.comments.append(str(payload.get("body", "")))
            return self._send(201, {"id": 1})

        self._send(404, {"message": "Not Found"})


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = State()
        self.state.users = {
            "kanade": {"login": "kanade", "id": 1001},
            "kaede": {"login": "Kaede", "id": 1002},
        }
        self.state.issue = {
            "number": 7,
            "body": "### GitHub 用户名\n\nkanade\n",
            "user": {"login": "kanade"},
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.env_backup = dict(os.environ)
        os.environ.clear()
        os.environ.update(
            {
                "GITHUB_TOKEN": "fake-token",
                "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
                "LOLI_ORG": "LoLiOA",
                "GITHUB_REPOSITORY": "LoLiOA/JoinLoLi",
                "ISSUE_NUMBER": "7",
            }
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.clear()
        os.environ.update(self.env_backup)

    def run_main(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = join_loli.main([])
        return code, out.getvalue(), err.getvalue()

    def invite_posts(self) -> list[str]:
        return [p for m, p in self.state.requests if m == "POST" and p.endswith("/invitations")]


class CleanLoginTests(unittest.TestCase):
    def test_accepts_plain_and_noisy_forms(self) -> None:
        for raw, expected in [
            ("kanade", "kanade"),
            ("  @kanade  ", "kanade"),
            ("https://github.com/kanade", "kanade"),
            ("kaede (备用账号)", "kaede"),
            ("- kanade", ""),
        ]:
            with self.subTest(raw=raw):
                # "- kanade" 不是合法登录名, 交给上面的标签正则处理
                self.assertEqual(join_loli.clean_login(raw), expected)

    def test_rejects_non_answers(self) -> None:
        for raw in ["", "_No response_", "无", "none", "!!", "a" * 40]:
            with self.subTest(raw=raw):
                self.assertEqual(join_loli.clean_login(raw), "")


class ExtractLoginTests(unittest.TestCase):
    def test_reads_issue_form_heading(self) -> None:
        body = "### GitHub 用户名\n\n@kaede\n"
        self.assertEqual(join_loli.extract_login({"body": body}), "kaede")

    def test_reads_bold_label_on_same_line(self) -> None:
        body = "**GitHub 用户名**: kanade\n"
        self.assertEqual(join_loli.extract_login({"body": body}), "kanade")

    def test_reads_marker_comment(self) -> None:
        body = "随便写点什么\n<!-- login:kaede -->\n"
        self.assertEqual(join_loli.extract_login({"body": body}), "kaede")

    def test_returns_empty_when_no_answer(self) -> None:
        body = "### GitHub 用户名\n\n_No response_\n"
        self.assertEqual(join_loli.extract_login({"body": body}), "")


class IntegrationTests(BaseCase):
    def test_invites_issue_author_and_comments(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual([i["invitee"]["login"] for i in self.state.invitations], ["kanade"])
        self.assertEqual(len(self.state.comments), 1)
        self.assertIn("欢迎来到 LoLi", self.state.comments[0])

    def test_template_username_matching_author_is_case_insensitive(self) -> None:
        self.state.issue["body"] = "### GitHub 用户名\n\nKaNade\n"
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual([i["invitee"]["login"] for i in self.state.invitations], ["kanade"])

    def test_template_username_for_someone_else_is_skipped(self) -> None:
        # 安全: 不允许借 Issue 给别人刷组织邀请
        self.state.issue["body"] = "### GitHub 用户名\n\nkaede\n"
        code, _, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(self.invite_posts(), [])
        self.assertIn("不一致", self.state.comments[0])
        self.assertIn("::error::", err)

    def test_existing_member_is_not_invited_again(self) -> None:
        self.state.members.add("kanade")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.invite_posts(), [])
        self.assertIn("已经是", self.state.comments[0])

    def test_pending_invitation_is_not_sent_again(self) -> None:
        self.state.invitations.append({"invitee": {"login": "KaNade", "id": 1001}})
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.invite_posts(), [])
        self.assertIn("已经收到过", self.state.comments[0])

    def test_unknown_user_fails_politely(self) -> None:
        self.state.issue["body"] = "### GitHub 用户名\n\nghost\n"
        self.state.issue["user"] = {"login": "ghost"}
        code, _, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(self.invite_posts(), [])
        self.assertIn("找不到", self.state.comments[0])
        self.assertIn("::error::", err)

    def test_insufficient_permissions_explains_fix(self) -> None:
        self.state.invite_status = 403
        code, _, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertIn("admin:org", self.state.comments[0])
        self.assertIn("::error::", err)

    def test_dry_run_touches_nothing(self) -> None:
        os.environ["DRY_RUN"] = "true"
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.invite_posts(), [])
        self.assertEqual(self.state.comments, [])
        self.assertIn("dry_run=True", out)
        self.assertIn("演练模式", out)

    def test_missing_token_exits_early(self) -> None:
        os.environ.pop("GITHUB_TOKEN")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("::error::", err)
        self.assertEqual(self.state.requests, [])

    def test_invalid_login_is_rejected(self) -> None:
        os.environ["LOLI_LOGIN"] = "--not-a-login--"
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("::error::", err)
        self.assertEqual(self.invite_posts(), [])

    def test_workflow_dispatch_without_issue(self) -> None:
        os.environ.pop("ISSUE_NUMBER", None)
        os.environ["LOLI_LOGIN"] = "kaede"
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual([i["invitee"]["login"] for i in self.state.invitations], ["Kaede"])
        self.assertEqual(self.state.comments, [])


class WorkflowFileTests(unittest.TestCase):
    def test_workflow_is_valid_yaml_and_calls_the_script(self) -> None:
        path = ROOT / ".github" / "workflows" / "join-on-issue.yml"
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
        except ImportError:  # pragma: no cover
            self.skipTest("pyyaml 未安装")
        data = yaml.safe_load(text)
        # PyYAML 会把裸 on: 解析成布尔 True, 这里兼容两种写法
        triggers = data.get("on", data.get(True))
        self.assertIn("opened", triggers["issues"]["types"])
        self.assertIn("join_loli.py", text)
        self.assertIn("ORG_INVITE_TOKEN", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
