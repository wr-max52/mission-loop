# Mission Loop

A research desk run by AI agents on your own computer. Give it a question, or a line of inspiration and let it
find its own leads. It searches the web, writes a sourced research brief for you to approve, writes the article,
and publishes it to a local website with RSS and JSON feeds. You stay in the loop: nothing is written until you
approve the brief, and nothing is published until you approve the article.

## Install

You need a Mac with Python 3.10 or newer (3.12 recommended). Research also uses
[Node.js](https://nodejs.org) and [uv](https://docs.astral.sh/uv) for its search and page-reading tools:

```bash
brew install python@3.12 node uv
```

Then get the code and start it:

```bash
git clone https://github.com/wr-max52/mission-loop.git
```

```bash
cd mission-loop
```

```bash
./start.sh
```

`start.sh` makes a virtual environment in `.venv`, installs the packages the first time, and opens the app at
http://127.0.0.1:8770. Run it again any time to start the app.

## First launch

The Missions page opens with a **Get started** card. It shows what is done and what is left, and nothing on it
is required just to look around:

1. **Connect a model.** Two workers share the work: the *Research Assistant* (a small, fast model) searches and
   reads, and the *Principal Investigator* (the strongest model you can run) writes the briefs and articles.
   - With [Ollama](https://ollama.com/download) (free, runs on your computer): pull a model or two, for example
     `ollama pull qwen3:8b`, then press **Connect Ollama**. It gives each worker one of your models.
   - With a cloud model (Claude, GPT, Gemini, Grok, DeepSeek, OpenRouter and more): paste its API key on
     **Settings → Model providers**, then pick a model for each worker.
2. **Web search.** Research searches with Brave Search. Get a key at https://brave.com/search/api/ (there is a
   free tier) and paste it into the card. It is checked with Brave and saved on this machine.
3. **Give it something to work on.** An inspiration line (a question the mission finder explores, finding its own
   missions), or a mission you add yourself.
4. **Start the loop** with the play button at the top of the page.

New to the idea? The top bar's **About** shows how the desk works, and the chat bubble at the bottom right opens
**Researcher Chat**, which can tell you what is left to set up and what the loop is doing.

## Everyday use

- **Briefs and articles** wait for you under *Needs you* on the Missions page: approve, or send back with notes.
  The **Auto** switch approves briefs for you; publishing always waits for you.
- **Published articles** are in `site/`, with feeds at `site/feed.xml` and `site/feed.json`. Put them online and
  announce them from **Settings → Publishing** (GitHub Pages, Bluesky, 1F916, or any webhook or folder).
- **Outreach** (off until you switch it on under **Settings → Outreach**): after a brief is approved, an agent asks
  a few people one short question about it by email, a Bluesky mention or a reply on 1F916, then the mission waits
  for their replies (48 hours by default) before the article is written.
- **Personalize** the published site's name, tagline and About statement under **Settings → Publication**, and
  the app's appearance under **Settings → Display**.
- **Use the desk from another app.** **Settings → MCP server** shares every tool Researcher Chat has (the loop,
  the Theorist's moves, the library, web research) as one MCP server on your local network, with a token and a
  ready-to-paste setup for Claude Code, Claude Desktop, Cursor, LM Studio and others. Only devices on your network
  get in; nothing goes through the cloud.
- **From the terminal:** `source .venv/bin/activate`, then `python run.py --help` lists every command.

## When something isn't working

```bash
.venv/bin/python run.py doctor
```

`doctor` checks the models, their connection, web search and the tools research needs, and says how to fix each
one. **Settings → Setup** shows the same list.

- **The play button says Waiting.** The loop is running but can't work (no model chosen, Ollama closed, a key
  that stopped working). Hover over it for the reason. It tries again every 30 seconds, so fixing the cause is
  enough.
- **"Port 8770 is already in use".** Another app has the port (a running Mission Loop is simply opened again). Start
  it on another port: `./start.sh --port 8771`.
- **Missions get parked.** A mission is set aside after three failed attempts. Its page says why; requeue it once
  the cause is fixed.

## Running the loop in the background

`ops/com.missionloop.loop.plist` runs the loop at login with macOS launchd. Change the paths in it to this folder
first; the comments at its top say how to install, pause and remove it.

## What stays on your computer

Everything you set up stays in this folder and is never committed: settings in `data/settings.json`, API keys in
`data/providers.json` and `data/search.json`, the MCP server's token in `data/mcp_share.json` and the outreach email
login in `data/email.json` (readable only by you), the missions in `data/loop.db`, the
published site in `site/`. A `.env` file works too, if you prefer one (see `.env.example`); a value saved in the
app wins over it.

## For developers

`AGENTS.md` describes how everything works. Run the tests with `.venv/bin/python -m pytest -q`; they need no
models and no network.
