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
        self.assertEqual(draft.title, f"Ping @{J}here")
        self.assertEqual(draft.body,
                         f"cc @{J}everyone @{J}user @{J}user @{J}role @{J}jason @{J}MiraNote-AI/core")


J = "\u2060"  # word joiner


class NeutralizeMentionsTests(unittest.TestCase):
    def test_leaves_emails_alone(self):
        text = "mail a@b.com or x.y-z@example.org"
        self.assertEqual(neutralize_mentions(text), text)

    def test_handle_inside_an_inline_code_span_is_neutralized_in_place(self):
        self.assertEqual(neutralize_mentions("Run `deploy --as @octocat now` here"),
                         f"Run `deploy --as @{J}octocat now` here")

    def test_handles_inside_code_are_neutralized_too(self):
        self.assertEqual(neutralize_mentions("``a ` @b`` and\n```sh\ngit log --author @c\n```\n~~~\n@d"),
                         f"``a ` @{J}b`` and\n```sh\ngit log --author @{J}c\n```\n~~~\n@{J}d")

    def test_backticks_broken_by_a_paragraph(self):
        # GitHub ends the paragraph at the blank line, so these backticks are
        # literal and the handle would be live.
        self.assertEqual(neutralize_mentions("Start `code\n\ncc @octocat please\n\nend` here"),
                         f"Start `code\n\ncc @{J}octocat please\n\nend` here")

    def test_escaped_backticks(self):
        self.assertEqual(neutralize_mentions("Use \\`not code @octocat \\` here"),
                         f"Use \\`not code @{J}octocat \\` here")

    def test_stray_backtick_is_kept(self):
        self.assertEqual(neutralize_mentions("a ` @g"), f"a ` @{J}g")

    def test_is_idempotent(self):
        once = neutralize_mentions("@a @everyone")
        self.assertEqual(neutralize_mentions(once), once)

    def test_handles_github_still_linked_get_the_joiner(self):
        # Each of these rendered a live mention on GitHub (POST /markdown)
        # when the joiner depended on a Unicode \w lookbehind: a Chinese or
        # accented letter before "@", a second "@", emphasis that ends in "_",
        # and "@" written as an entity.
        cases = [
            ("cc @@octocat please", f"cc @@{J}octocat please"),
            ("Jos\u00e9@octocat and \u00e9@octocat", f"Jos\u00e9@{J}octocat and \u00e9@{J}octocat"),
            ("\u8bf7@octocat \u770b\u4e00\u4e0b", f"\u8bf7@{J}octocat \u770b\u4e00\u4e0b"),
            ("\u8bf7<@123456789012345678>\u770b\u4e00\u4e0b", f"\u8bf7@{J}user\u770b\u4e00\u4e0b"),
            ("\u5927\u5bb6@everyone\u770b\u4e00\u4e0b", f"\u5927\u5bb6@{J}everyone\u770b\u4e00\u4e0b"),
            ("_a_@octocat", f"_a_@{J}octocat"),
            ("__a__@octocat", f"__a__@{J}octocat"),
            ("cc &#64;octocat", f"cc @{J}octocat"),
            ("cc &#064;octocat", f"cc @{J}octocat"),
            ("cc &#x40;octocat", f"cc @{J}octocat"),
            ("cc &commat;octocat", f"cc @{J}octocat"),
        ]
        for text, neutral in cases:
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), neutral)

    def test_leaves_email_links_and_git_remotes_alone(self):
        for text in ["<jason@example.com> and [mail](mailto:jason@example.com)",
                     "git clone git@github.com:MiraNote-AI/mirabot.git"]:
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), text)

    def test_a_domain_after_the_name_does_not_protect_a_handle(self):
        # GitHub links each of these, so only an email's "@" (a letter or
        # digit before it) is left alone.
        for text in ["cc @octocat.", "cc @octocat..com", "cc @octocat.http://example.com",
                     "cc @octocat.ftp://example.com"]:
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), text.replace("@", f"@{J}"))


class IssueBodyTests(unittest.TestCase):
    def test_links_the_discussion_and_who_confirmed(self):
        body = issue_body(IssueDraft("mirabot", "T", "## Goal"), "https://discord.com/x", "Jason")
        self.assertTrue(body.startswith("## Goal"))
        self.assertIn("(https://discord.com/x)", body)
        self.assertIn("confirmed by `Jason`.", body)

    def test_display_name_cannot_add_markdown(self):
        body = issue_body(IssueDraft("mirabot", "T", "B"), "https://discord.com/x", "**`@x`** [y](z)")
        self.assertIn(f"confirmed by `**'@{J}x'** [y](z)`.", body)


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
        self.token_status = 200

        async def installation(request):
            self.calls.append(("installation", request.headers["Authorization"]))
            return web.json_response({"id": 42})

        async def token(request):
            self.calls.append(("token", request.match_info["id"], await request.json()))
            if self.token_status != 200:
                return web.json_response({"message": "There is at least one repository that does not exist "
                                          "or is not accessible"}, status=self.token_status)
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

    async def test_repo_outside_the_installation_gives_a_clear_error(self):
        self.token_status = 422
        with self.assertRaisesRegex(RuntimeError, "installed on all of them"):
            await self.github.create_issue("mirabot", "A", "B")
        self.assertNotIn("issue", [c[0] for c in self.calls])

    async def test_a_4xx_is_not_looked_up(self):
        self.issue_status = 422
        with self.assertRaises(aiohttp.ClientResponseError):
            await self.github.create_issue("mirabot", "A", "B")
        self.assertEqual(self.calls[-1][0], "issue")


if __name__ == "__main__":
    unittest.main()
