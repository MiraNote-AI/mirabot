import json
import unittest
from unittest import mock

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_issues
from github_issues import GitHubApp, IssueDraft, issue_body, load_issue_config, neutralize_mentions, parse_draft


def make_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


class ParseDraftTests(unittest.TestCase):
    def test_plain_json(self):
        draft = parse_draft('{"repo": "miranote-api", "title": "Add accounts", "body": "## Goal\\nx"}')
        self.assertEqual(draft, IssueDraft("miranote-api", "Add accounts", "## Goal\nx"))

    def test_json_in_a_code_fence(self):
        raw = 'Here it is:\n```json\n{"repo": "mirabot", "title": "T", "body": "B"}\n```'
        self.assertEqual(parse_draft(raw).repo, "mirabot")

    def test_unknown_repo_falls_back_to_the_first_one(self):
        # The preview shows the repo and the picker can change it.
        draft = parse_draft('{"repo": "miranote-android", "title": "T", "body": "B"}')
        self.assertEqual(draft.repo, "miranote-ios")

    def test_body_with_its_own_code_block(self):
        raw = json.dumps({"repo": "miranote-api", "title": "Fix 500",
                          "body": "## Background\n```\nTraceback ...\n```"})
        self.assertIn("Traceback", parse_draft(raw).body)
        self.assertEqual(parse_draft(f"```json\n{raw}\n```").title, "Fix 500")

    def test_not_json_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_draft("Sure! The issue should be about accounts.")

    def test_missing_title_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_draft('{"repo": "mirabot", "title": " ", "body": "B"}')


    def test_draft_pings_no_one(self):
        raw = json.dumps({"repo": "mirabot", "title": "Ping @here",
                          "body": "cc @everyone <@123> <@!456> <@&789> @jason @MiraNote-AI/core"})
        draft = parse_draft(raw)
        self.assertEqual(draft.title, "Ping `@here`")
        self.assertEqual(draft.body,
                         "cc `@everyone` `@user` `@user` `@role` `@jason` `@MiraNote-AI/core`")


class NeutralizeMentionsTests(unittest.TestCase):
    def test_leaves_emails_and_code_spans_alone(self):
        text = "mail a@b.com, run `@decorator`"
        self.assertEqual(neutralize_mentions(text), text)


class IssueBodyTests(unittest.TestCase):
    def test_links_the_discussion_and_who_confirmed(self):
        body = issue_body(IssueDraft("mirabot", "T", "## Goal"), "https://discord.com/x", "Jason")
        self.assertTrue(body.startswith("## Goal"))
        self.assertIn("(https://discord.com/x)", body)
        self.assertIn("confirmed by `Jason`.", body)

    def test_display_name_cannot_add_markdown(self):
        body = issue_body(IssueDraft("mirabot", "T", "B"), "https://discord.com/x", "**`@x`** [y](z)")
        self.assertIn("confirmed by `**'@x'** [y](z)`.", body)


class LoadIssueConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = make_pem()[1]

    def env(self, **overrides):
        env = {"GITHUB_APP_ID": "1234", "GITHUB_APP_PRIVATE_KEY": self.pem,
               "TEAM_DISCORD_IDS": "11, 22"}
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def test_valid_settings(self):
        config = load_issue_config(self.env())
        self.assertEqual(config.team_ids, {11, 22})
        self.assertEqual(config.github.app_id, "1234")

    def test_key_with_escaped_newlines(self):
        config = load_issue_config(self.env(GITHUB_APP_PRIVATE_KEY=self.pem.replace("\n", "\\n")))
        self.assertEqual(config.github.private_key, self.pem)

    def test_missing_or_malformed_settings_raise_value_error(self):
        for overrides in [{"GITHUB_APP_ID": None}, {"GITHUB_APP_ID": "abc"},
                          {"GITHUB_APP_PRIVATE_KEY": None}, {"GITHUB_APP_PRIVATE_KEY": "not a key"},
                          {"TEAM_DISCORD_IDS": None}, {"TEAM_DISCORD_IDS": "11,jason"}]:
            with self.subTest(overrides), self.assertRaises(ValueError):
                load_issue_config(self.env(**overrides))


class GitHubAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key, pem = make_pem()
        self.public_key = key.public_key()
        self.calls = []
        self.issue_status = 201
        self.listed = []

        async def installation(request):
            self.calls.append(("installation", request.headers["Authorization"]))
            return web.json_response({"id": 42})

        async def token(request):
            self.calls.append(("token", request.match_info["id"], await request.json()))
            return web.json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})

        async def issue(request):
            self.calls.append(("issue", request.match_info["repo"], request.headers["Authorization"],
                               await request.json()))
            return web.json_response({"number": 7, "html_url": "https://github.com/i/7"},
                                     status=self.issue_status)

        async def list_issues(request):
            self.calls.append(("list", request.match_info["repo"], request.query["state"]))
            return web.json_response(self.listed)

        app = web.Application()
        app.router.add_get("/orgs/MiraNote-AI/installation", installation)
        app.router.add_post("/app/installations/{id}/access_tokens", token)
        app.router.add_post("/repos/MiraNote-AI/{repo}/issues", issue)
        app.router.add_get("/repos/MiraNote-AI/{repo}/issues", list_issues)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.api = mock.patch.object(github_issues, "API", f"http://127.0.0.1:{port}")
        self.api.start()
        self.github = GitHubApp("1234", pem)

    async def asyncTearDown(self):
        self.api.stop()
        await self.runner.cleanup()

    async def test_files_the_issue_with_an_installation_token(self):
        number, url = await self.github.create_issue("mirabot", "Title", "Body")

        self.assertEqual((number, url), (7, "https://github.com/i/7"))
        app_jwt = self.calls[0][1].removeprefix("Bearer ")
        claims = jwt.decode(app_jwt, self.public_key, algorithms=["RS256"])
        self.assertEqual(claims["iss"], "1234")
        self.assertEqual(self.calls[1], ("token", "42", {
            "repositories": github_issues.REPOS, "permissions": {"issues": "write"}}))
        self.assertEqual(self.calls[2], ("issue", "mirabot", "Bearer inst-token",
                                         {"title": "Title", "body": "Body"}))

    async def test_reuses_the_installation_token(self):
        await self.github.create_issue("mirabot", "A", "B")
        await self.github.create_issue("mirabot", "C", "D")

        self.assertEqual([c[0] for c in self.calls], ["installation", "token", "issue", "issue"])

    async def test_token_expiry_comes_from_github(self):
        await self.github.create_issue("mirabot", "A", "B")
        self.assertEqual(self.github._expires, 4070908800.0)  # 2099-01-01T00:00:00Z

    async def test_after_a_5xx_returns_the_issue_github_filed_anyway(self):
        self.issue_status = 502
        self.listed = [
            {"number": 6, "title": "Other", "user": {"type": "Bot"}, "html_url": "u6"},
            {"number": 8, "title": "A", "user": {"type": "User"}, "html_url": "u8"},
            {"number": 9, "title": "A", "user": {"type": "Bot"}, "html_url": "u9"},
        ]
        self.assertEqual(await self.github.create_issue("mirabot", "A", "B"), (9, "u9"))
        self.assertEqual(self.calls[-1], ("list", "mirabot", "all"))

    async def test_after_a_5xx_with_nothing_filed_raises(self):
        self.issue_status = 503
        with self.assertRaises(aiohttp.ClientResponseError):
            await self.github.create_issue("mirabot", "A", "B")
        self.assertEqual(self.calls[-1][0], "list")

    async def test_a_4xx_is_not_looked_up(self):
        self.issue_status = 422
        with self.assertRaises(aiohttp.ClientResponseError):
            await self.github.create_issue("mirabot", "A", "B")
        self.assertEqual(self.calls[-1][0], "issue")


if __name__ == "__main__":
    unittest.main()
