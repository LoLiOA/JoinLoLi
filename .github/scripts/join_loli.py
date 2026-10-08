#!/usr/bin/env python3
"""把 Issue 作者邀请进 LoLiOA 组织 (仅供 GitHub Actions / 本地测试调用)。

设计要点:
  * 只依赖 Python 标准库, runner 上无需 pip install。
  * 幂等: 已是成员 / 已有待接受邀请 => 直接跳过, 不算失败。
  * 组织邀请属于 Org 权限, github.token 永远没有该权限,
    必须通过 ORG_INVITE_TOKEN 传入一个带 admin:org 权限的 PAT。
  * 失败一律以非 0 退出, 并把原因回帖到 Issue, 方便用户自助排查。

环境变量:
  GITHUB_TOKEN       必填, 带 admin:org 权限的 PAT
  GITHUB_API_URL     可选, 默认 https://api.github.com (测试 / GHES)
  LOLI_ORG           可选, 默认 LoLiOA
  LOLI_LOGIN         可选, 直接指定要邀请的用户名 (workflow_dispatch 用)
  GITHUB_ACTOR       可选, 触发者, 作为兜底
  GITHUB_REPOSITORY  可选, owner/repo, 用于回帖
  ISSUE_NUMBER       可选, 要回帖的 Issue 编号
  DRY_RUN            可选, true 时只打印计划, 不邀请也不回帖
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ORG = "LoLiOA"
DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT = 30

# GitHub 登录名: 1-39 位, 字母数字开头, 中间允许单个连字符
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# Issue 模板里 <input>/<textarea> 的值不会出现在正文中, 因此正文只作为兜底:
# 1) 显式标记 <!-- login:xxx -->
MARKED_LOGIN_RE = re.compile(r"<!--\s*login:\s*([^\s>]+)\s*-->")
# 2) 常见写法 "GitHub 用户名: xxx" / "用户名：xxx" / "- 用户名: @xxx"
LABELED_LOGIN_RE = re.compile(
    r"^[ \t]*(?:[-*+]\s*|#{1,6}\s*)?(?:\*\*)?"
    r"(?:github\s*(?:用户名|账号|username|user\s*name|user\s*id|id)"
    r"|用户名|用户\s*id)"
    r"(?:\*\*)?\s*[:：]?\s*(?:\*\*)?\s*[@]?",
    re.IGNORECASE | re.MULTILINE,
)


class ApiError(Exception):
    def __init__(self, status: int, message: str, url: str = "") -> None:
        super().__init__(f"HTTP {status} {message} ({url})")
        self.status = status
        self.message = message
        self.url = url


class Client:
    """极小的 GitHub REST 客户端 (标准库 urllib, 无第三方依赖)。"""

    def __init__(self, token: str, api: str = DEFAULT_API) -> None:
        self.token = token
        self.api = api.rstrip("/")
        self.calls: list[tuple[str, str]] = []  # (METHOD, path), 便于测试断言

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
    ) -> tuple[int, object]:
        url = f"{self.api}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "Accept": "application/vnd.github+json",
            "User-Agent": "JoinLoLi",
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        self.calls.append((method.upper(), path))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode() or "null"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"message": raw[:300]}
            message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            raise ApiError(exc.code, message, path) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"网络错误: {exc.reason}", path) from None

    # ---- 业务方法 ----
    def get_issue(self, repo: str, number: int) -> dict:
        _, data = self.request("GET", f"/repos/{repo}/issues/{number}")
        return data if isinstance(data, dict) else {}

    def get_user(self, login: str) -> dict:
        _, data = self.request("GET", f"/users/{urllib.parse.quote(login)}")
        return data if isinstance(data, dict) else {}

    def is_member(self, org: str, login: str) -> bool:
        try:
            status, _ = self.request(
                "GET",
                f"/orgs/{urllib.parse.quote(org)}/members/{urllib.parse.quote(login)}",
            )
            return status == 204
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise

    def list_pending_invitations(self, org: str) -> list:
        found: list = []
        page = 1
        while True:
            _, data = self.request(
                "GET", f"/orgs/{urllib.parse.quote(org)}/invitations?per_page=100&page={page}"
            )
            if not isinstance(data, list) or not data:
                return found
            found.extend(data)
            if len(data) < 100:
                return found
            page += 1

    def invite(self, org: str, user_id: int, role: str = "direct_member") -> dict:
        _, data = self.request(
            "POST",
            f"/orgs/{urllib.parse.quote(org)}/invitations",
            {"invitee_id": user_id, "role": role},
        )
        return data if isinstance(data, dict) else {}

    def comment(self, repo: str, number: int, body: str) -> None:
        self.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def clean_login(value: str) -> str:
    """从模板答案里抠出登录名, 去掉 @ / 反引号 / 链接 / 括号等噪声。"""
    text = (value or "").strip().replace("`", " ").replace('"', " ").strip()
    if not text or text.lower() in {"none", "无", "n/a", "na", "-", "xxx", "your-name"}:
        return ""
    match = re.search(r"github\.com/([A-Za-z0-9-]+)", text, re.IGNORECASE)
    if match:
        text = match.group(1)
    else:
        # 取第一个 token, 再剥掉结尾标点
        text = re.split(r"[\s,;/|<>()\[\]]+", text.lstrip("@#"))[0]
    text = text.strip().strip(".").strip("-")
    return text if LOGIN_RE.match(text) else ""


def extract_login(issue: dict) -> str:
    """优先取正文里标记过的用户名, 否则取 "标签: 值" 写法。"""
    body = issue.get("body") or ""
    marked = MARKED_LOGIN_RE.search(body)
    if marked:
        candidate = clean_login(marked.group(1))
        if candidate:
            return candidate
    labeled = LABELED_LOGIN_RE.search(body)
    if labeled:
        # 答案通常在下一行 (Issue Form 渲染成 "### 字段名\n\n值"), 因此跳过空行
        for line in body[labeled.end() :].splitlines():
            if line.strip():
                candidate = clean_login(line)
                if candidate:
                    return candidate
                break
    return ""


def resolve_invitee(client: Client, org: str, login: str) -> tuple[str, str]:
    """返回 (结果, 面向用户的说明)。结果取值:
    invited / already_member / already_invited / not_found"""
    try:
        user = client.get_user(login)
    except ApiError as exc:
        if exc.status == 404:
            return "not_found", f"GitHub 上找不到用户 `{login}`"
        raise

    real_login = str(user.get("login") or login)
    if not user.get("id"):
        return "not_found", f"GitHub 上找不到用户 `{login}`"
    user_id = int(user["id"])

    if client.is_member(org, real_login):
        return "already_member", f"`{real_login}` 已经是 `{org}` 的成员, 无需重复邀请"

    for invite in client.list_pending_invitations(org):
        if not isinstance(invite, dict):
            continue
        invitee = invite.get("invitee") or {}
        if str(invitee.get("login", "")).lower() == real_login.lower():
            return "already_invited", (
                f"`{real_login}` 已经收到过 `{org}` 的邀请, "
                f"请到邮箱或 [邀请页面](https://github.com/orgs/{org}/invitation) 接受"
            )

    client.invite(org, user_id)
    return "invited", (
        f"已向 `{real_login}` 发出 `{org}` 的邀请, "
        f"请到邮箱或 [邀请页面](https://github.com/orgs/{org}/invitation) 接受"
    )


OK_OUTCOMES = {"invited", "already_member", "already_invited", "preview"}


def build_comment(outcome: str, detail: str, dry_run: bool) -> str:
    prefix = "🧪 **[Dry Run]** " if dry_run else ""
    head = "🎉 欢迎来到 LoLi!" if outcome in OK_OUTCOMES else "😢 邀请没能发出"
    return (
        f"{prefix}{head}\n\n{detail}\n\n"
        "<sub>本 Issue 由 Join LoLi workflow 自动处理, 关闭 / 删除它不会撤销邀请。</sub>"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--dry-run" in argv:
        os.environ["DRY_RUN"] = "true"

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    org = (os.environ.get("LOLI_ORG") or DEFAULT_ORG).strip()
    api = (os.environ.get("GITHUB_API_URL") or DEFAULT_API).strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    dry_run = env_flag("DRY_RUN")

    try:
        issue_number = int(os.environ.get("ISSUE_NUMBER", "") or 0)
    except ValueError:
        issue_number = 0

    if not token:
        print("::error::缺少 GITHUB_TOKEN / ORG_INVITE_TOKEN, 无法邀请成员", file=sys.stderr)
        return 1

    # 手工输入的用户名必须严格合法, 绝不做"-"剥离之类的猜测
    raw_explicit = (os.environ.get("LOLI_LOGIN") or "").strip().lstrip("@").strip()
    if raw_explicit and not LOGIN_RE.match(raw_explicit):
        print(
            f"::error::`{raw_explicit}` 不是合法的 GitHub 登录名, 已中止",
            file=sys.stderr,
        )
        return 2

    client = Client(token, api)

    login = raw_explicit
    source = "workflow_dispatch 输入"
    claimed = ""
    if not login and issue_number and repo:
        try:
            issue = client.get_issue(repo, issue_number)
            # 安全: 一律以 Issue 作者为邀请对象。模板里填的用户名只是"自述",
            # 若与作者不一致, 说明有人想替第三方刷邀请, 直接跳过并提示。
            login = clean_login(str(issue.get("user", {}).get("login", "")))
            claimed = extract_login(issue)
            source = "Issue 作者"
        except ApiError as exc:
            print(f"::warning::读取 Issue 失败, 回退到触发者: {exc}", file=sys.stderr)
    if not login and actor:
        login = clean_login(actor)
        source = "触发者"

    if not login:
        print("::error::无法确定要邀请的登录名", file=sys.stderr)
        if issue_number and repo and not dry_run:
            message = "没能确定要邀请的 GitHub 用户, 请联系组织管理员手动处理。"
            try:
                client.comment(repo, issue_number, build_comment("invalid", message, dry_run))
            except ApiError as exc:
                print(f"::warning::回帖失败: {exc}", file=sys.stderr)
        return 2

    if claimed and claimed.lower() != login.lower():
        detail = (
            f"模板里填写的用户名 `{claimed}` 与本 Issue 作者 `{login}` 不一致, "
            "为避免误邀请他人, 本次不发送邀请。\n\n"
            f"如果 `{claimed}` 确实需要加入, 请让 TA 自己提一个 Issue, "
            f"或由组织管理员到 Actions -> Join LoLi -> Run workflow 手动指定。"
        )
        print(f"::error::{detail}", file=sys.stderr)
        if not dry_run:
            try:
                client.comment(repo, issue_number, build_comment("mismatch", detail, dry_run))
            except ApiError as exc:
                print(f"::warning::回帖失败: {exc}", file=sys.stderr)
        return 3

    print(f"组织={org} 用户={login} 来源={source} dry_run={dry_run}")

    if dry_run:
        outcome = "preview"
        detail = f"演练模式: 本应邀请 `{login}` 加入 `{org}`, 未真正发送。"
    else:
        try:
            outcome, detail = resolve_invitee(client, org, login)
        except ApiError as exc:
            outcome = "error"
            if exc.status in (401, 403, 404):
                detail = (
                    f"邀请 `{login}` 失败: 令牌权限不足或配置有误 "
                    f"(`{exc.status} {exc.message}`)。\n\n"
                    f"请确认 `ORG_INVITE_TOKEN` 确实带有 `admin:org` 权限, "
                    f"且持有人是 `{org}` 的组织管理员。"
                )
            else:
                detail = f"邀请 `{login}` 失败: `{exc}`"

    print(f"结果: {outcome} - {detail}")

    # 除"演练"外的非成功结果都打注解, 让 Actions 页面直接飘红, 便于维护者发现
    if outcome not in OK_OUTCOMES:
        print(f"::error::{detail}", file=sys.stderr)

    if issue_number and repo and not dry_run:
        try:
            client.comment(repo, issue_number, build_comment(outcome, detail, dry_run))
        except ApiError as exc:
            print(f"::warning::回帖失败: {exc}", file=sys.stderr)

    return 0 if outcome in OK_OUTCOMES else 3


if __name__ == "__main__":
    sys.exit(main())
