# Linka — Building & Running Your AI Agent

Every Linka user can activate their own personal AI agent — an assistant that can act *as you*, sending and reading messages in your chats, based on rules you set up. This reference covers how to build, configure, and run one.

## What the agent is

Your agent has its own dedicated 1:1 chat with you (created automatically) — that's where you configure it and talk to it directly. Everywhere else (real chats with other people, or groups), it acts on your behalf according to the rules you've defined, sending messages that show up exactly like messages from you.

The agent is off by default when first created. You turn it on/off with a single master toggle. You can give it an optional name (used only in how it refers to itself, not shown anywhere in the UI) and decide whether it's allowed to admit — if directly asked — that it's an AI rather than you personally; by default it doesn't volunteer or admit this.

## Building your agent — the guided interview

The first time you set up your agent, it walks you through a guided conversation (in its own chat) covering five things:
1. **Triggers** — when should it wake up and respond (see below).
2. **Per-trigger behavior** — what should it actually do in each situation.
3. **Escalation/handoff rules** — when should it stop and hand the conversation to you.
4. **Tone and boundaries** — how it should sound, and what topics/actions are off-limits.
5. **Identity and disclosure** — its name (optional) and whether it can admit to being AI if asked.

You can pause the interview at any point and come back to it later — nothing you've already set up gets lost. You can also just talk to your agent directly and ask it to do something for you right away (e.g. "send this person a message and tell me what they say") without finishing the full setup first.

## Triggers — when your agent wakes up

You choose one or more ways your agent decides to respond:
- **Specific chats** — wake up only in chats you've chosen, optionally only when a message contains certain keywords (leave keywords blank to respond to anything in that chat).
- **Time window** — only be active during certain hours of the day.
- **Unknown senders** — automatically respond the first time someone new messages you privately; once it replies, that person is remembered so the agent keeps replying to them going forward (up to a cap of 200 such auto-added contacts).
- **Reply to every new private message** — a broader always-on option that responds to any private (non-group) message, without needing to add each chat individually.
- **Schedule** — have it run a one-off or recurring task at a specific time you set, following a plain-language instruction you give it (e.g. "every Monday at 9am, message the team a reminder"). Up to 10 scheduled tasks.

By default the agent never responds inside groups unless you explicitly allow it. You can also tell it never to read or respond in specific chats you've excluded.

## Knowledge base

You can give your agent reference material to draw on when answering — price lists, FAQs, policies, inventory info, and similar. There are two ways to add it:
- **Paste/type reference text** directly in your conversation with the agent — it can recognize on its own that what you've shared looks like reference material worth saving, and will tell you what it saved and why.
- **Upload a file** (text, Markdown, or PDF) from the agent's settings or directly in its chat via the attach menu.

The agent searches this knowledge base itself when answering questions, rather than needing the whole thing repeated to it every time. There's a cap of 20 documents / 2000 chunks of content per agent.

You can also send your agent a file directly in your own chat with it (a photo, PDF, etc.) — it will remember it's available and can resend that exact file into a real conversation later if it's relevant (e.g. "send them the price list"), without needing you to re-upload it.

## Escalation — handing off to you

If your agent gets stuck, is asked something outside its boundaries, or the other person explicitly asks to speak to a real person, it will pause that conversation and notify you — you'll get a message in your own agent chat describing who it was talking to and why, plus a push notification if you're not in the app. The other person is also told (in their own language, naturally) that they're being connected with a real person, rather than being left hanging.

Once paused, the agent won't respond in that conversation again until you explicitly resume it (by naming the person to your agent) or 24 hours pass, after which it automatically resumes on its own.

There's also a safety check that runs before every reply to someone else — if a message looks like it's trying to manipulate the agent, extract confidential info, or get it to run code, the agent politely declines and — for the more serious cases — automatically pauses and flags it to you the same way, so you're aware even if you weren't watching.

## Usage limits

Your agent has several built-in usage limits to keep it responsive and prevent runaway use — for example: how many times per hour it can start a new conversation, how much it can actually be "thinking/working" per day, and overall usage budgets that reset every few hours and every week. If a limit is hit, your agent simply goes quiet until the relevant window resets, and — for the usage budgets — you'll get a one-time notice in your agent chat telling you it happened and roughly when it'll be available again. You can check your current usage at any time from the agent chat (a small ring/popover shows how much of your rolling usage windows you've used, with a live countdown to when they reset). If you're ever blocked, the composer in your agent chat will show you exactly when it unblocks.

You can also ask your agent directly (while setting it up) how close it is to any of its limits, and roughly how many new conversations per hour it could currently handle.

## Resetting your agent

If you want to start over completely, there's a one-click reset that wipes your entire conversation history with the agent, deletes its knowledge base, and resets every setting (tone, triggers, boundaries, name, disclosure choice) back to default — you'll be asked to confirm since this can't be undone. Whether the agent is turned on/off is not affected by a reset.
