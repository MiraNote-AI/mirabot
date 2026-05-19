import dotenv from "dotenv";
import path from "path";
import { fileURLToPath } from "url";
import { Client, GatewayIntentBits, Partials, ChannelType } from "discord.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Reuse the existing mirabot/.env (the bot is already in the MiraNote server).
dotenv.config({ path: path.join(__dirname, "..", "..", ".env") });
// Also load mcp-server/.env if present, so it can override.
dotenv.config({ path: path.join(__dirname, "..", ".env") });

const TOKEN = process.env.MIRANOTE_DISCORD_TOKEN || process.env.DISCORD_BOT_TOKEN;
if (!TOKEN) {
	console.error(
		"[miranote-mcp] No Discord token. Set DISCORD_BOT_TOKEN in mirabot/.env or MIRANOTE_DISCORD_TOKEN in mcp-server/.env",
	);
	process.exit(1);
}

const discord = new Client({
	intents: [
		GatewayIntentBits.Guilds,
		GatewayIntentBits.GuildMessages,
		GatewayIntentBits.MessageContent,
		GatewayIntentBits.DirectMessages,
		GatewayIntentBits.GuildMembers,
	],
	partials: [Partials.Channel, Partials.Message, Partials.User],
});

console.error("[miranote-mcp] connecting to Discord...");
await new Promise((resolve, reject) => {
	discord.once("ready", () => {
		console.error(
			`[miranote-mcp] logged in as ${discord.user.tag} (${discord.user.id})`,
		);
		resolve();
	});
	discord.login(TOKEN).catch(reject);
});

const TEXT_CHANNEL_TYPES = new Set([
	ChannelType.GuildText,
	ChannelType.GuildAnnouncement,
	ChannelType.PublicThread,
	ChannelType.PrivateThread,
	ChannelType.AnnouncementThread,
	ChannelType.DM,
]);

const MAX_FETCH_BYTES = 10 * 1024 * 1024;

function htmlToText(html) {
	const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
	const title = titleMatch ? titleMatch[1].replace(/\s+/g, " ").trim() : "";
	const text = html
		.replace(/<!--[\s\S]*?-->/g, "")
		.replace(/<script[\s\S]*?<\/script>/gi, "")
		.replace(/<style[\s\S]*?<\/style>/gi, "")
		.replace(/<head[\s\S]*?<\/head>/gi, "")
		.replace(/<nav\b[\s\S]*?<\/nav>/gi, "")
		.replace(/<aside\b[\s\S]*?<\/aside>/gi, "")
		.replace(/<footer\b[\s\S]*?<\/footer>/gi, "")
		.replace(/<(br|p|div|li|h[1-6]|tr|blockquote|section|article)\b[^>]*>/gi, "\n")
		.replace(/<\/(p|div|li|h[1-6]|tr|blockquote|section|article)>/gi, "\n")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
		.replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(parseInt(n, 10)))
		.replace(/&[a-z]+;/gi, " ")
		.replace(/[ \t]+/g, " ")
		.replace(/\n[ \t]+/g, "\n")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
	return title ? `# ${title}\n\n${text}` : text;
}

async function fetchWithLimit(url, { accept } = {}) {
	const res = await fetch(url, {
		headers: {
			"User-Agent":
				"Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
			...(accept ? { Accept: accept } : {}),
			"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
		},
		redirect: "follow",
		signal: AbortSignal.timeout(30000),
	});
	if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
	const buf = Buffer.from(await res.arrayBuffer());
	if (buf.byteLength > MAX_FETCH_BYTES) {
		throw new Error(
			`Response too large: ${buf.byteLength} bytes (max ${MAX_FETCH_BYTES})`,
		);
	}
	return { buf, contentType: res.headers.get("content-type") || "", url: res.url };
}

const formatMessage = (m) => ({
	id: m.id,
	author: { id: m.author.id, username: m.author.username, bot: m.author.bot },
	content: m.content,
	timestamp: m.createdAt.toISOString(),
	channel_id: m.channelId,
	attachments: [...m.attachments.values()].map((a) => ({
		id: a.id,
		url: a.url,
		name: a.name,
		content_type: a.contentType,
		size: a.size,
	})),
	reactions: [...m.reactions.cache.values()].map((r) => ({
		emoji: r.emoji.name ?? r.emoji.id,
		count: r.count,
	})),
	reply_to: m.reference?.messageId ?? null,
	edited: m.editedAt?.toISOString() ?? null,
});

const asText = (obj) => ({
	content: [{ type: "text", text: JSON.stringify(obj, null, 2) }],
});

const server = new McpServer({ name: "miranote-discord", version: "0.1.0" });

server.tool(
	"discord_whoami",
	"Return info about the bot identity, plus the guilds (servers) and channels it can see. Call this first to discover guild IDs and channel IDs.",
	{},
	async () => {
		const guilds = [];
		for (const cachedGuild of discord.guilds.cache.values()) {
			const guild = await cachedGuild.fetch();
			const channels = await guild.channels.fetch();
			const accessible = [];
			for (const ch of channels.values()) {
				if (!ch || !TEXT_CHANNEL_TYPES.has(ch.type)) continue;
				const perms = ch.permissionsFor(discord.user);
				if (!perms?.has("ViewChannel")) continue;
				accessible.push({
					id: ch.id,
					name: ch.name,
					type: ChannelType[ch.type],
					can_send: perms.has("SendMessages"),
					can_read_history: perms.has("ReadMessageHistory"),
					can_react: perms.has("AddReactions"),
				});
			}
			guilds.push({
				id: guild.id,
				name: guild.name,
				member_count: guild.memberCount,
				accessible_channels: accessible,
			});
		}
		return asText({
			bot: {
				id: discord.user.id,
				username: discord.user.username,
				tag: discord.user.tag,
			},
			guilds,
		});
	},
);

server.tool(
	"discord_list_channels",
	"List all text channels the bot can access. Optionally filter to one guild.",
	{
		guild_id: z.string().optional().describe("Optional: filter to this guild ID."),
	},
	async ({ guild_id }) => {
		const guildList = guild_id
			? [await discord.guilds.fetch(guild_id)]
			: [...discord.guilds.cache.values()];
		const result = [];
		for (const g of guildList) {
			const guild = await g.fetch();
			const channels = await guild.channels.fetch();
			for (const ch of channels.values()) {
				if (!ch || !TEXT_CHANNEL_TYPES.has(ch.type)) continue;
				const perms = ch.permissionsFor(discord.user);
				if (!perms?.has("ViewChannel")) continue;
				result.push({
					id: ch.id,
					name: ch.name,
					type: ChannelType[ch.type],
					guild_id: guild.id,
					guild_name: guild.name,
				});
			}
		}
		return asText(result);
	},
);

server.tool(
	"discord_lookup_member",
	"Resolve a Discord username or display-name substring (e.g. 'mengjia') to numeric user ID(s) within a guild. Use this when you need to programmatically @-mention a user via the <@USER_ID> format but only know their username.",
	{
		guild_id: z
			.string()
			.optional()
			.describe("Guild to search. If omitted and the bot is in exactly one guild, that guild is used."),
		query: z.string().min(1).describe("Username or display-name substring to search for."),
		limit: z.number().int().min(1).max(100).default(10).describe("Maximum matches."),
	},
	async ({ guild_id, query, limit }) => {
		let guild;
		if (guild_id) {
			guild = await discord.guilds.fetch(guild_id);
		} else if (discord.guilds.cache.size === 1) {
			guild = discord.guilds.cache.first();
		} else {
			throw new Error(
				`guild_id is required: bot is in ${discord.guilds.cache.size} guilds. Call discord_whoami to list them.`,
			);
		}
		const members = await guild.members.fetch({ query, limit });
		const result = [];
		for (const m of members.values()) {
			result.push({
				id: m.user.id,
				username: m.user.username,
				global_name: m.user.globalName ?? null,
				nick: m.nickname ?? null,
				bot: m.user.bot,
			});
		}
		return asText({
			guild_id: guild.id,
			guild_name: guild.name,
			query,
			count: result.length,
			members: result,
		});
	},
);

server.tool(
	"discord_read_channel",
	"Fetch recent messages from a channel (returned newest-first). Bot needs View Channel + Read Message History.",
	{
		channel_id: z.string().describe("Channel ID to read from."),
		limit: z.number().int().min(1).max(100).default(50).describe("How many messages (max 100)."),
		before: z.string().optional().describe("Pagination: only fetch messages older than this message ID."),
	},
	async ({ channel_id, limit, before }) => {
		const channel = await discord.channels.fetch(channel_id);
		const opts = { limit: Math.min(limit, 100) };
		if (before) opts.before = before;
		const messages = await channel.messages.fetch(opts);
		const formatted = [...messages.values()].map(formatMessage);
		return asText({
			channel: { id: channel.id, name: channel.name ?? null },
			count: formatted.length,
			messages: formatted,
		});
	},
);

server.tool(
	"discord_send_message",
	"Send a message to a channel. 2000 char max. Set reply_to to reply to an existing message.",
	{
		channel_id: z.string().describe("Target channel ID."),
		content: z.string().min(1).max(2000).describe("Message text."),
		reply_to: z.string().optional().describe("Optional: existing message ID to reply to."),
	},
	async ({ channel_id, content, reply_to }) => {
		const channel = await discord.channels.fetch(channel_id);
		let sent;
		if (reply_to) {
			const original = await channel.messages.fetch(reply_to);
			sent = await original.reply(content);
		} else {
			sent = await channel.send(content);
		}
		return asText({
			sent: true,
			message_id: sent.id,
			channel_id: channel.id,
			timestamp: sent.createdAt.toISOString(),
		});
	},
);

server.tool(
	"discord_react",
	"Add an emoji reaction to a message.",
	{
		channel_id: z.string(),
		message_id: z.string(),
		emoji: z.string().describe("Unicode emoji or custom emoji ID."),
	},
	async ({ channel_id, message_id, emoji }) => {
		const channel = await discord.channels.fetch(channel_id);
		const msg = await channel.messages.fetch(message_id);
		await msg.react(emoji);
		return asText({ reacted: true, emoji });
	},
);

server.tool(
	"discord_send_dm",
	"Send a direct message to a user (by their Discord user ID).",
	{
		user_id: z.string().describe("Target user's Discord ID."),
		content: z.string().min(1).max(2000),
	},
	async ({ user_id, content }) => {
		const user = await discord.users.fetch(user_id);
		const sent = await user.send(content);
		return asText({
			sent: true,
			message_id: sent.id,
			user: { id: user.id, username: user.username },
		});
	},
);

server.tool(
	"discord_read_dms",
	"Read recent DM history with a specific user.",
	{
		user_id: z.string(),
		limit: z.number().int().min(1).max(100).default(50),
	},
	async ({ user_id, limit }) => {
		const user = await discord.users.fetch(user_id);
		const dm = await user.createDM();
		const messages = await dm.messages.fetch({ limit: Math.min(limit, 100) });
		const formatted = [...messages.values()].map(formatMessage);
		return asText({
			user: { id: user.id, username: user.username },
			count: formatted.length,
			messages: formatted,
		});
	},
);

server.tool(
	"discord_search_messages",
	"Scan recent messages in a channel and return ones containing the query string (case-insensitive substring). Discord's API has no real bot-accessible search, so this fetches recent N and filters locally.",
	{
		channel_id: z.string(),
		query: z.string().min(1),
		scan_limit: z.number().int().min(1).max(1000).default(200).describe("Max recent messages to scan."),
	},
	async ({ channel_id, query, scan_limit }) => {
		const channel = await discord.channels.fetch(channel_id);
		const q = query.toLowerCase();
		const matches = [];
		let before;
		let scanned = 0;
		while (scanned < scan_limit) {
			const batchSize = Math.min(100, scan_limit - scanned);
			const batch = await channel.messages.fetch({ limit: batchSize, before });
			if (batch.size === 0) break;
			const arr = [...batch.values()];
			for (const m of arr) {
				if (m.content.toLowerCase().includes(q)) matches.push(formatMessage(m));
			}
			scanned += arr.length;
			before = arr[arr.length - 1].id;
			if (arr.length < batchSize) break;
		}
		return asText({ query, scanned, match_count: matches.length, matches });
	},
);

server.tool(
	"discord_fetch_url",
	"Fetch a web page URL and return its text content (HTML stripped, scripts/styles/nav removed). Use for reading links shared in Discord messages. Non-text content types are rejected.",
	{
		url: z.string().url().describe("HTTP(S) URL to fetch."),
		max_chars: z.number().int().min(100).max(200000).default(50000).describe("Truncate text to this many characters."),
	},
	async ({ url, max_chars }) => {
		const { buf, contentType, url: finalUrl } = await fetchWithLimit(url, {
			accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
		});
		if (
			!contentType.includes("text/") &&
			!contentType.includes("xml") &&
			!contentType.includes("json")
		) {
			throw new Error(`Unsupported content-type for discord_fetch_url: ${contentType}`);
		}
		const body = buf.toString("utf8");
		const text = contentType.includes("html") ? htmlToText(body) : body;
		const truncated = text.length > max_chars;
		const out = truncated ? text.slice(0, max_chars) + "\n\n[... truncated ...]" : text;
		return {
			content: [
				{
					type: "text",
					text: `URL: ${finalUrl}\nContent-Type: ${contentType}\nLength: ${text.length}${truncated ? " (truncated)" : ""}\n\n${out}`,
				},
			],
		};
	},
);

server.tool(
	"discord_fetch_attachment",
	"Fetch a Discord attachment by its URL (the `url` field on attachments returned by discord_read_channel). Returns image content for images, raw text for text/json/xml, or metadata-only for unsupported binary types. 10 MB cap.",
	{
		url: z.string().url().describe("Discord CDN attachment URL."),
		max_chars: z.number().int().min(100).max(200000).default(50000).describe("For text content, truncate to this many characters."),
	},
	async ({ url, max_chars }) => {
		const { buf, contentType, url: finalUrl } = await fetchWithLimit(url);
		const mime = contentType.split(";")[0].trim().toLowerCase();

		if (mime.startsWith("image/")) {
			return {
				content: [
					{ type: "image", data: buf.toString("base64"), mimeType: mime },
					{
						type: "text",
						text: `Image attachment\nURL: ${finalUrl}\nContent-Type: ${mime}\nSize: ${buf.byteLength} bytes`,
					},
				],
			};
		}

		const isTextLike =
			mime.startsWith("text/") ||
			mime === "application/json" ||
			mime === "application/xml" ||
			mime === "application/javascript" ||
			mime === "application/x-yaml" ||
			mime === "application/yaml";
		if (isTextLike) {
			const body = buf.toString("utf8");
			const truncated = body.length > max_chars;
			const out = truncated ? body.slice(0, max_chars) + "\n\n[... truncated ...]" : body;
			return {
				content: [
					{
						type: "text",
						text: `URL: ${finalUrl}\nContent-Type: ${mime}\nSize: ${buf.byteLength} bytes${truncated ? " (truncated)" : ""}\n\n${out}`,
					},
				],
			};
		}

		return {
			content: [
				{
					type: "text",
					text: `Unsupported attachment type for inline reading.\nURL: ${finalUrl}\nContent-Type: ${mime}\nSize: ${buf.byteLength} bytes\n\nSupported types: image/*, text/*, application/json, application/xml, application/javascript, application/yaml.`,
				},
			],
			isError: true,
		};
	},
);

const shutdown = async () => {
	console.error("[miranote-mcp] shutting down");
	try {
		await discord.destroy();
	} catch {}
	process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[miranote-mcp] MCP server ready (stdio)");
