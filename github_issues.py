"""
GitHub issues for MiraBot: turn a Discord discussion into an issue draft and
file it through the MiraBot GitHub App (issues show as the app, not a person).
"""

import json
import re
import time
from dataclasses import dataclass

import aiohttp
import jwt

ORG = "MiraNote-AI"
REPOS = ["miranote-ios", "miranote-api", "miranote-web", "mirabot", ".github"]
API = "https://api.github.com"

# "issue" not inside a longer Latin word ("issues", "tissue"); Chinese around
# it is fine ("la ge issue").
ISSUE_WORD = re.compile(r"(?<![a-z])issue(?![a-z])", re.IGNORECASE)

DRAFT_SYSTEM = (
    "You turn a Discord discussion of the MiraNote team into one GitHub issue. "
    "MiraNote is an AI journal app. Repos: "
    "miranote-ios (the iPhone app, SwiftUI), "
    "miranote-api (FastAPI backend services), "
    "miranote-web (Next.js web app), "
    "mirabot (the team's Discord bot), "
    ".github (org-wide rules, CI checks, shared docs). "
    "The chat is often in Chinese; write the issue in English. "
    "Use only what the discussion says; do not invent decisions, owners or dates. "
    "Body sections in markdown: '## Background', '## Goal', "
    "'## Acceptance criteria' (a checklist), and '## Open questions' if any remain. "
    'Reply with JSON only: {"repo": "<one of the repos>", "title": "<imperative, '
    'under 72 characters>", "body": "<markdown>"}'
)


@dataclass
class IssueDraft:
    repo: str
    title: str
    body: str


def parse_draft(raw: str) -> IssueDraft:
    """The model's JSON reply as a draft. Raises ValueError when it is unusable."""
    # The body may hold its own code blocks, so take the outermost braces
    # rather than the text between the first two fences.
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1] if start != -1 else text)
    except json.JSONDecodeError as e:
        raise ValueError(f"draft is not JSON: {e}") from e
    repo = str(data.get("repo", "")).strip()
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not title or not body:
        raise ValueError("draft has no title or body")
    if repo not in REPOS:
        repo = REPOS[0]
    return IssueDraft(repo=repo, title=title[:120], body=body)


def issue_body(draft: IssueDraft, discussion_url: str, requested_by: str) -> str:
    """The body filed on GitHub: the draft plus where it came from."""
    return (
        f"{draft.body}\n\n---\n"
        f"Filed by MiraBot from the [Discord discussion]({discussion_url}), "
        f"requested by {requested_by}."
    )


class GitHubApp:
    """Installation-token auth for one GitHub App installed on ORG."""

    def __init__(self, app_id: str, private_key: str):
        self.app_id = app_id
        self.private_key = private_key
        self._token = ""
        self._expires = 0.0

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def _installation_token(self, session: aiohttp.ClientSession) -> str:
        # Installation tokens last an hour; renew five minutes early.
        if self._token and time.time() < self._expires - 300:
            return self._token
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
        }
        async with session.get(f"{API}/orgs/{ORG}/installation", headers=headers) as resp:
            resp.raise_for_status()
            installation = (await resp.json())["id"]
        async with session.post(
            f"{API}/app/installations/{installation}/access_tokens", headers=headers
        ) as resp:
            resp.raise_for_status()
            self._token = (await resp.json())["token"]
        self._expires = time.time() + 3600
        return self._token

    async def create_issue(self, repo: str, title: str, body: str) -> tuple[int, str]:
        """File the issue; returns its number and URL."""
        async with aiohttp.ClientSession() as session:
            token = await self._installation_token(session)
            async with session.post(
                f"{API}/repos/{ORG}/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"title": title, "body": body},
            ) as resp:
                if resp.status == 401:
                    self._token = ""  # revoked or reinstalled: mint a new one next time
                resp.raise_for_status()
                issue = await resp.json()
        return issue["number"], issue["html_url"]
