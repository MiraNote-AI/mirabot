"""
MiraBot — Discord AI Bot
Features:
- /ask <question> — Ask AI with channel context
- /summarize [count] — Summarize recent messages
- /notes — Extract key decisions and TODOs
- /export [count] — Export chat history to local file
- Auto-sync: periodically saves all channel logs to chat_logs/
- Responds to @mentions, replies to bot messages, and "mirabot" keyword
- Fetches URL content (including JS-rendered pages like Xiaohongshu)
"""

import os
import re
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from openai import OpenAI
from dotenv import load_dotenv

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_GUILD_ID = os.environ["DISCORD_GUILD_ID"]
AI_API_KEY = os.environ["AI_API_KEY"]
AI_API_BASE_URL = os.environ.get("AI_API_BASE_URL", "https://api.openai.com/v1")

CHAT_LOGS_DIR = Path(__file__).parent / "chat_logs"
CHAT_LOGS_DIR.mkdir(exist_ok=True)

SYNC_INTERVAL_MINUTES = 5
_sync_task = None

# Sites that need JS rendering (Playwright)
JS_RENDERED_SITES = ["xiaohongshu.com", "xhslink.com", "douyin.com", "weibo.com"]

# ---------- AI client ----------
ai = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE_URL)


async def fetch_channel_history(channel, limit=50):
    """Fetch recent messages from a channel (newest first, then reversed to chronological)."""
    messages = []
    async for msg in channel.history(limit=limit):
        messages.append({
            "author": msg.author.display_name,
            "content": msg.content,
            "timestamp": msg.created_at.isoformat(),
            "is_bot": msg.author.bot,
        })
    messages.reverse()
    return messages


def format_history_for_ai(messages):
    """Format message list into readable text for AI."""
    lines = []
    for m in messages:
        if m["is_bot"]:
            continue
        lines.append(f"[{m['timestamp'][:16]}] {m['author']}: {m['content']}")
    return "\n".join(lines)


URL_PATTERN = re.compile(r'https?://\S+')


def needs_js_rendering(url: str) -> bool:
    """Check if a URL needs JavaScript rendering."""
    return any(site in url for site in JS_RENDERED_SITES)


async def fetch_url_with_playwright(url: str) -> str:
    """Fetch page content using headless browser (for JS-rendered sites).
    For Xiaohongshu, extracts structured data from embedded SSR state."""
    if not HAS_PLAYWRIGHT:
        return "[Playwright not installed — cannot render JS pages]"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                viewport={"width": 390, "height": 844},
            )
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Try to extract structured data (Xiaohongshu SSR)
            ssr_data = await page.evaluate("() => window.__SETUP_SERVER_STATE__ || null")
            if ssr_data:
                return _format_xhs_data(ssr_data)

            # Fallback: just get page text
            text = await page.evaluate("document.body.innerText")
            await browser.close()
            return text[:5000]
    except Exception as e:
        return f"[Could not render {url}: {e}]"


def _format_xhs_data(ssr_data: dict) -> str:
    """Format Xiaohongshu SSR data into readable text."""
    page_data = ssr_data.get("LAUNCHER_SSR_STORE_PAGE_DATA", {})
    note_data = page_data.get("noteData", {})
    comment_data = page_data.get("commentData", {})

    parts = []

    # Post info
    user = note_data.get("user", {})
    author = user.get("nickName", "Unknown")
    title = note_data.get("title", "")
    desc = note_data.get("desc", "")
    interact = note_data.get("interactInfo", {})
    likes = interact.get("likedCount", "?")
    collected = interact.get("collectedCount", "?")
    comment_count = interact.get("commentCount", "?")

    parts.append(f"Author: {author}")
    if title:
        parts.append(f"Title: {title}")
    parts.append(f"Content: {desc}")
    parts.append(f"Likes: {likes} | Collected: {collected} | Comments: {comment_count}")
    parts.append("")

    # Comments
    comments = comment_data.get("comments", [])
    if comments:
        parts.append(f"=== Comments ({len(comments)} loaded of {comment_data.get('commentCount', '?')} total) ===")
        for c in comments:
            nickname = c.get("user", {}).get("nickname", "?")
            content = c.get("content", "")
            like_count = c.get("likeCount", 0)
            parts.append(f"- {nickname}: {content} (likes: {like_count})")
            for sc in c.get("subComments", []):
                sn = sc.get("user", {}).get("nickname", "?")
                scontent = sc.get("content", "")
                parts.append(f"  └ {sn}: {scontent}")

    return "\n".join(parts)[:8000]


async def fetch_url_simple(url: str) -> str:
    """Fetch page content with simple HTTP request."""
    if not HAS_AIOHTTP:
        return ""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return f"[Could not fetch {url}: HTTP {resp.status}]"
                html = await resp.text()
                html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                html = re.sub(r'<[^>]+>', ' ', html)
                html = re.sub(r'\s+', ' ', html).strip()
                return html[:3000]
    except Exception as e:
        return f"[Could not fetch {url}: {e}]"


async def fetch_url_content(url: str) -> str:
    """Fetch URL content, using Playwright for JS-heavy sites."""
    if needs_js_rendering(url):
        return await fetch_url_with_playwright(url)
    return await fetch_url_simple(url)


async def extract_url_context(text: str) -> str:
    """Find URLs in text and fetch their content."""
    urls = URL_PATTERN.findall(text)
    if not urls:
        return ""
    # Deduplicate
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    contents = []
    for url in unique_urls[:3]:  # max 3 URLs
        content = await fetch_url_content(url)
        if content:
            contents.append(f"--- Content from {url} ---\n{content}\n--- End ---")
    return "\n\n".join(contents)


async def ask_ai(question: str, chat_history: str = "", system_override: str = "", url_context: str = "") -> str:
    """Send a question to the AI backend with channel history as context."""
    system_prompt = system_override or "You are MiraBot, a helpful AI assistant on Discord. Answer concisely."
    if chat_history:
        system_prompt += (
            "\n\nBelow is the recent chat history from this Discord channel. "
            "Use it to understand context and answer questions about the conversation.\n\n"
            f"--- Chat History ---\n{chat_history}\n--- End History ---"
        )
    if url_context:
        system_prompt += (
            "\n\nBelow is the content fetched from URLs mentioned in the conversation. "
            "This includes the full page content with comments if available. "
            "Use it to answer questions about those links.\n\n" + url_context
        )
    resp = await asyncio.to_thread(
        ai.chat.completions.create,
        model="gemini-flash-latest",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        max_tokens=2048,
    )
    return resp.choices[0].message.content


def save_messages_to_file(channel_name: str, messages: list):
    """Save messages to a JSON file in chat_logs/."""
    filepath = CHAT_LOGS_DIR / f"{channel_name}.json"
    existing = []
    if filepath.exists():
        existing = json.loads(filepath.read_text())
    existing_timestamps = {m["timestamp"] for m in existing}
    for m in messages:
        if m["timestamp"] not in existing_timestamps:
            existing.append(m)
    existing.sort(key=lambda m: m["timestamp"])
    filepath.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    return filepath


# ---------- Discord bot ----------
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))


# ===== /ask =====
@tree.command(name="ask", description="Ask MiraBot a question (with channel context)", guild=guild_obj)
@app_commands.describe(question="Your question for MiraBot")
async def ask_command(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        msgs = await fetch_channel_history(interaction.channel)
        history = format_history_for_ai(msgs)
        url_ctx = await extract_url_context(question)
        answer = await ask_ai(question, history, url_context=url_ctx)
    except Exception as e:
        answer = f"Sorry, something went wrong: {e}"
    await interaction.followup.send(answer)


# ===== /summarize =====
@tree.command(name="summarize", description="Summarize recent chat messages", guild=guild_obj)
@app_commands.describe(count="Number of messages to summarize (default 50)")
async def summarize_command(interaction: discord.Interaction, count: int = 50):
    await interaction.response.defer(thinking=True)
    try:
        msgs = await fetch_channel_history(interaction.channel, limit=count)
        history = format_history_for_ai(msgs)
        answer = await ask_ai(
            f"Summarize the following {count} messages. Highlight key topics, decisions, and action items.",
            history,
            system_override="You are MiraBot. Provide a clear, structured summary of Discord conversations. Use bullet points.",
        )
    except Exception as e:
        answer = f"Sorry, something went wrong: {e}"
    await interaction.followup.send(answer)


# ===== /notes =====
@tree.command(name="notes", description="Extract key decisions and TODOs from recent chat", guild=guild_obj)
@app_commands.describe(count="Number of messages to analyze (default 100)")
async def notes_command(interaction: discord.Interaction, count: int = 100):
    await interaction.response.defer(thinking=True)
    try:
        msgs = await fetch_channel_history(interaction.channel, limit=count)
        history = format_history_for_ai(msgs)
        answer = await ask_ai(
            "Extract from this conversation:\n"
            "1. **Key Decisions** — things the team agreed on\n"
            "2. **TODOs / Action Items** — tasks mentioned, with who is responsible if stated\n"
            "3. **Open Questions** — unresolved questions\n"
            "4. **Key Ideas** — important ideas or suggestions discussed",
            history,
            system_override="You are MiraBot, a project note-taking assistant. Be thorough but concise. Use markdown formatting.",
        )
    except Exception as e:
        answer = f"Sorry, something went wrong: {e}"
    await interaction.followup.send(answer)


# ===== /export =====
@tree.command(name="export", description="Export chat history to local file for Claude Code", guild=guild_obj)
@app_commands.describe(count="Number of messages to export (default 200)")
async def export_command(interaction: discord.Interaction, count: int = 200):
    await interaction.response.defer(thinking=True)
    try:
        msgs = await fetch_channel_history(interaction.channel, limit=count)
        channel_name = interaction.channel.name
        filepath = save_messages_to_file(channel_name, msgs)
        answer = f"Exported {len(msgs)} messages to `{filepath}`\nClaude Code can now read this file."
    except Exception as e:
        answer = f"Sorry, something went wrong: {e}"
    await interaction.followup.send(answer)


# ===== Respond to @mentions, replies, and "mirabot" keyword =====
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    should_reply = False

    # 1. @MiraBot — someone mentioned the bot
    if bot.user in message.mentions:
        should_reply = True

    # 2. Reply to bot's message — fetch the referenced message if not cached
    if message.reference:
        try:
            ref_msg = message.reference.resolved
            if ref_msg is None:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            if ref_msg.author == bot.user:
                should_reply = True
        except Exception:
            pass

    # 3. Keyword "mirabot"
    if "mirabot" in message.content.lower():
        should_reply = True

    if should_reply:
        async with message.channel.typing():
            try:
                msgs = await fetch_channel_history(message.channel, limit=300)
                history = format_history_for_ai(msgs)
                url_ctx = await extract_url_context(message.content)
                answer = await ask_ai(message.content, history, url_context=url_ctx)
            except Exception as e:
                answer = f"Sorry, something went wrong: {e}"
            await message.reply(answer)


# ===== Auto-sync: save all channel logs periodically =====
async def auto_sync_logs():
    """Periodically sync all text channel messages to local files."""
    await bot.wait_until_ready()
    guild = bot.get_guild(int(DISCORD_GUILD_ID))
    while not bot.is_closed():
        if guild:
            for channel in guild.text_channels:
                try:
                    msgs = await fetch_channel_history(channel, limit=200)
                    save_messages_to_file(channel.name, msgs)
                except Exception:
                    pass
            print(f"[{datetime.now(timezone.utc).isoformat()[:19]}] Auto-synced chat logs for all channels")
        await asyncio.sleep(SYNC_INTERVAL_MINUTES * 60)


# ===== On ready =====
@bot.event
async def on_ready():
    global _sync_task
    await tree.sync(guild=guild_obj)
    print(f"MiraBot is online as {bot.user} — slash commands synced to guild {DISCORD_GUILD_ID}")
    if _sync_task is not None:
        _sync_task.cancel()
    _sync_task = bot.loop.create_task(auto_sync_logs())


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)