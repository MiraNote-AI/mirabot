import json
import unittest
from unittest import mock

import jwt
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_issues
from github_issues import ISSUE_WORD, GitHubApp, IssueDraft, issue_body, parse_draft


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


class IssueWordTests(unittest.TestCase):
    def test_matches_issue_as_a_word(self):
        for text in ["pls file an issue", "Issue: login breaks",
                     "\u62c9\u4e2aissue", "issue\u5427"]:  # Chinese around it
            self.assertTrue(ISSUE_WORD.search(text), text)

    def test_ignores_longer_words(self):
        for text in ["any open issues?", "pass me a tissue"]:
            self.assertFalse(ISSUE_WORD.search(text), text)


class IssueBodyTests(unittest.TestCase):
    def test_links_the_discussion_and_the_requester(self):
        body = issue_body(IssueDraft("mirabot", "T", "## Goal"), "https://discord.com/x", "Jason")
        self.assertTrue(body.startswith("## Goal"))
        self.assertIn("(https://discord.com/x)", body)
        self.assertIn("requested by Jason", body)


class GitHubAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = key.public_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.calls = []

        async def installation(request):
            self.calls.append(("installation", request.headers["Authorization"]))
            return web.json_response({"id": 42})

        async def token(request):
            self.calls.append(("token", request.match_info["id"]))
            return web.json_response({"token": "inst-token"})

        async def issue(request):
            self.calls.append(("issue", request.match_info["repo"], request.headers["Authorization"],
                               await request.json()))
            return web.json_response({"number": 7, "html_url": "https://github.com/i/7"}, status=201)

        app = web.Application()
        app.router.add_get("/orgs/MiraNote-AI/installation", installation)
        app.router.add_post("/app/installations/{id}/access_tokens", token)
        app.router.add_post("/repos/MiraNote-AI/{repo}/issues", issue)
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
        self.assertEqual(self.calls[1], ("token", "42"))
        self.assertEqual(self.calls[2], ("issue", "mirabot", "Bearer inst-token",
                                         {"title": "Title", "body": "Body"}))

    async def test_reuses_the_installation_token(self):
        await self.github.create_issue("mirabot", "A", "B")
        await self.github.create_issue("mirabot", "C", "D")

        self.assertEqual([c[0] for c in self.calls], ["installation", "token", "issue", "issue"])


if __name__ == "__main__":
    unittest.main()
