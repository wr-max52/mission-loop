# AGENTS.md

## Critical constraint: missions stay local

The code lives in the private GitHub repo `wr-max52/mission-loop` (`origin`). Push only when asked.
Never commit or push `missions.yaml`, `missions/`, `data/`, `site/`, or `.env`; they stay on this
machine. A local `pre-push` hook blocks commits containing them. Branches started before the repo
moved to GitHub must be rebased onto `origin/main` before pushing. Publishing writes a static site
into `site/` on this machine, posts the article text to 1f916.ai only once `data/1f916.json` holds a key, pushes
`site/` to a separate public GitHub Pages repo only once GitHub is connected and a repo linked (`data/github.json`,
from the Settings page), and announces it on Bluesky only once a login is saved (`data/bluesky.json`, from the
Settings page) or `.env` has `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`. `data/1f916.json` is the citizen's whole
identity (no recovery), `data/github.json` holds a GitHub token, `data/bluesky.json` an app password and
`data/providers.json` the cloud model providers' API keys, `data/search.json` the Brave Search key,
`data/mcp_share.json` the token LAN clients send to the shared MCP server (the Settings page shows it to the owner to
copy) and `data/email.json` the outreach email login (an app password): never commit, print or move any of them. The Pages repo is the one place `site/` is meant to go.

## How a task becomes a merged PR (coding agents, this repo only)

The written issue is what Will approves; nothing after it waits on him until the merge.

1. **Issue first.** Given a task, write the issue (the write-issue skill's format) and show it. Once Will okays it,
   create it.
2. **Then roll.** Build it on its own branch off the latest `origin/main` without stopping to ask, and open a PR that
   closes the issue. Opening that PR is the push the approved issue asked for.
3. **Ready to merge, not a draft.** The PR is ready for review when opened: `pytest -q` passes, AGENTS.md (and its
   test count) says what changed, the branch merges cleanly into `main`, and the description says what was checked
   and what wasn't. This replaces the write-issue skill's "open it as a draft" here.
4. **Watch it until it's merged.** For a few hours after opening it, watch the PR for review comments (in the Claude
   desktop app, turn on the PR monitor's comment handling instead of polling) and answer or fix each one on the same
   branch, keeping it ready to merge. Will merges; don't merge it yourself unless he asks.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- First launch: `./start.sh` (checks Python 3.10+, makes `.venv`, installs `requirements.txt` the first time and
  whenever it changes, notes a missing npx, uvx or Ollama, then runs `run.py ui`, which opens the browser). `README.md` is the newcomer's guide.
- CLI: `python run.py <command>` — `ui mcp doctor init add inspire theorize run loop stop auto approve reject review requeue push show status ask join-1f916 check-bluesky epics epic rename-epic
  combines approve-combine reject-combine link unlink library`
- Tests: `pytest -q` (expects 773 passed; no Ollama or network needed; `tests/test_sprites.py` runs the sprite checks in `node` and skips without it; any request to 1F916, GitHub, Bluesky (its
  public read API too), a custom webhook, a cloud model provider, an embedding model, the Wayback Machine or a mail
  server (SMTP, IMAP) fails the test, and `BLUESKY_*`, `MISSION_LOOP_GITHUB_*`, `MISSION_LOOP_EMBED_MODEL`,
  `MISSION_LOOP_OUTREACH` and every provider's key variable are cleared so a real `.env` never switches one on.
  `stub_agents` turns outreach on with a pretend channel and the stub Outreach agent; the `no_outreach` fixtures turn it off)
- Config, not code: nothing machine-specific is written in the code. Models, the Ollama address, the cloud model
  providers' keys, the 1F916 and Bluesky servers, the GitHub server and OAuth app, and the site address are looked
  up in `data/settings.json` (the
  Settings page writes it), then `.env` (see `.env.example`), and only a service address falls back to that
  service's public default (Ollama's local `http://127.0.0.1:11434`, `https://1f916.ai`, `https://bsky.social`,
  `https://github.com`; the site address falls back to the linked GitHub Pages address). Models have no default: with none
  chosen the loop won't start and says so. All lookups live in `settings.py`; don't add a model name or server
  address anywhere else (`settings.PROVIDERS` holds each provider's public API address). Tests set their own fake
  models in `tests/conftest.py`.
- Model providers: a worker's model is a bare name for Ollama (`qwen3:30b`, as it always was) or
  `<provider>/<model>` for a cloud provider (`anthropic/...`, `openai/...`, `xai/...`, `deepseek/...`, `gemini/...`,
  `mistral/...`, `groq/...`, `openrouter/<lab>/<model>`, `together/...`, or `custom/...` for any OpenAI-compatible
  server such as LM Studio or vLLM); `settings.split_model` tells them apart. A provider is on once it has a key:
  pasted on the Settings page's Model providers section (checked by listing its models, then saved to
  `data/providers.json`, mode 600, which wins over `.env`) or its variable in `.env` (`.env.example` lists them).
  Each worker can use a different provider. `agents/providers.py` (only `llm.py` calls it) speaks the two wire
  shapes that cover them all, Anthropic's Messages API and OpenAI's chat completions, over httpx (`TRANSPORT` for
  tests), and turns the loop's Ollama-shaped messages into theirs and the reply back into an `ollama.Message`, so no
  agent knows which provider answered: tool calls get ids, every call gets a result, thinking is never sent back,
  structured replies use the provider's JSON schema mode (one retry in plain JSON mode with the schema in words if
  it refuses the schema; DeepSeek always) and are streamed, so a stop keeps what was written. Cloud calls get
  `llm.CLOUD_CONTEXT` (64k) of prompt room and `CLOUD_REPLY_TOKENS` (16k, since reasoning models think inside it)
  instead of Ollama's 32k and 8k/2k; the time limit, Overrun and Cut work the same. `llm.ready` asks Ollama only
  when a working model is on it, and checks a cloud provider by listing its models, so a cloud-only machine needs
  no Ollama. "Connect Ollama" on the Settings page is the one-click start for a fresh clone: it checks Ollama
  answers and gives each worker without a model one of the installed ones (biggest to the PI, smallest to the
  Research Assistant; never overwrites a choice). Researcher Chat's picker lists cloud models too.
- Setup check (`doctor.py`): one list of what this machine still needs, each with how to fix it: a model for each
  worker that's on, those models answering (`deep=True` asks `llm.ready`, else Ollama's quick `llm.connection` and
  whether a cloud provider has a key), the Brave key, `npx` and `uvx` (from `toolbox.server_status`), and something to
  work on (a mission or an inspiration line). `python run.py doctor` prints it (exit 1 while a model is missing), the
  Settings page's Setup section shows it, Researcher Chat reads it (`setup_status`), and the home page's Get started
  card (`ui.setup_card`) walks through it: Connect Ollama (or links to get Ollama or add a cloud key), the Brave key
  (checked with Brave by `doctor.check_search_key`, `TRANSPORT` for tests; offline it is saved with a note, a refusal
  is not saved), an inspiration line with example lines (`ui.INSPIRATIONS`) or Add a mission, then play. The card goes
  once every check passes, or when hidden (`settings.setup_dismissed`; Settings → Setup brings it back). It never
  blocks anything. Every CLI command creates the database first (`run._start`), `ui` refuses a port with a live
  listener (`run._port_free`) and writes the site's index and About page when `site/` has none, so `/site/` and the
  top bar's About (`/about`) work before anything is published.
- An honest loop: play (and Add and start loop) asks `agents.ready` first (`ui.can_start`) and refuses with the
  reason. A running loop that can't work records why in `data/waiting.json` (`doctor.set_waiting`, logged once per
  reason) and checks again every `run.WAIT_RETRY` (30 s) instead of resting; the top bar then says Waiting (amber,
  the reason on hover) instead of Running, and the loop panel says what to fix. It is cleared when the loop can work
  or stops.
- Personalization: Settings → Publication sets the published site's name (`settings.publication_name`, default
  "Mission Loop"), a tagline (under the index's heading and as the feeds' description) and an About statement (its
  paragraphs open the About page, else the built-in line). Saving redraws every published page, the index, the About
  page and the feeds (`orchestrator.refresh_site`). Nothing is stored at the defaults.
- `MISSION_LOOP_STUBS=1` runs every agent on stubs (no Ollama, no network) and turns the 1F916 and Bluesky channels off.
- Specs for how it was built: `milestone1.md`, `milestone2.md`.

## Pipeline (v1)

```
(Theorist: evolves the inspiration line, may propose epics, 1 mission and 1 combine, links missions; then the finder)
QUEUED -> RESEARCHING -> BRIEF_READY -> OUTREACH -> AWAITING_COMMENTS -> WRITING -> ARTICLE_READY -> DONE (published)
          (validator,     (human:        (asks up to  (reply wait:        (article check, (human:
           3 attempts)     approve)       N people)    timer or all        then rating;    review)
                                                       answered)           3 attempts)
          OUTREACH and the reply wait are skipped while outreach is off or no outreach channel is set up.
QUEUED / RESEARCHING / BRIEF_READY / PARKED -> COMBINED (folded into a bigger mission; out of the flow)
```

- Order of work (`orchestrator.work`): there are no fixed passes. The loop works until nothing is left it
  can do on its own, reading missions fresh every few seconds, always in this order:
  1. Every code step, farthest along first (`in_flow_order`): publish a reviewed article, move an approved
     brief on, check for replies and end the reply wait, start a queued mission. No model is called. Right after the first sweep,
     and whenever a sweep publishes something, the channels post.
  2. Each idle worker takes the farthest-along mission for its role that hasn't had a turn this round
     (`_pick`), so writing goes before a brief, a brief before a new search, and a brief a human approves
     mid-task is next in line. A mission added with "Add and start loop" (`Mission.rush`) goes ahead of all of
     them for the Research Assistant, once; the flag is cleared when it is picked. A failed attempt shows as before and the mission waits until the others with
     work for that worker have had a turn; then a new round starts from the farthest along.
  When the Theorist is due, it is the Principal Investigator's first task of the run, and the finder waits for
  it. When the Research Assistant's role has nothing left, the mission finder runs once (what it finds is worked on
  straight away); when no worker has anything left, the loop rests (`--every` minutes, or until the UI wakes
  it). A step that raises is logged on its mission and skipped until the loop starts again; the others go on.
  `run.py loop` sends its notifications after every model task.
- Theorist: `agents/theorist.py`, the mastermind at the very start of the flow. On the Principal Investigator's
  model: first it searches the desk's library (see Library below; code looks up the inspiration line itself, then the
  Theorist may run up to `LOOKUPS` (4) searches of its own, all in `LOOKUP_TIME`, 4 minutes, through the `library` MCP
  server), then one structured call with what it found (`findings_text`, within `findings_room()`: `FINDINGS_CHARS`,
  or two searches at the Settings page's Top N, at most `FINDINGS_MAX`) and no tools. Stop is checked before each search,
  and a search still running past `stopping.TURN_GRACE` is given up. A library that won't start or runs out of time never stops it (`Theory.library` says why, kept in its notes); what
  it searched for is `Theory.lookups`, in `data/theory.json` and its notes. It reads every epic with its latest missions, the missions in no epic,
  the latest published articles with the possibilities they left open, the links and combines so far (and the ones
  a human turned down), and how the line got here (`data/inspiration_log.jsonl`, every change with who made it and
  why). It looks for patterns inside an epic and themes emerging across epics, and replies with those patterns, the
  line evolved, why, up to 2 new epics, at most 1 new mission, at most 1 combine and up to 3 links (see Connecting
  dots below). Code holds it to that (`theorist.settle`): the line may only ask questions (1 to 3, each
  ending in "?", at most 500 characters) and must keep at least half of the current line's meaningful words, so
  it evolves instead of being rewritten (one repair round, else the line stays); a new epic needs 2+ missions
  (counting its new mission) and a name no epic has; the new mission must not repeat a topic; a combine and a link
  follow the rules below (`theorist.settle_combine`, `settle_links`). The orchestrator
  applies it on the main thread between tasks (`orchestrator.apply_theory`), so no worker saves over it: a human
  edit to the line while it was thinking wins, its epics are *proposed* like the finder's (each mission moved
  keeps its old epic in `Mission.epic_fallback`; missions a worker is busy with stay put), and its mission is
  queued like a found one (it still needs brief review); its combine is proposed and its links are drawn right
  away. It runs when it has a line and missions, the desk changed
  since its last turn (`desk_fingerprint`: missions, their epics, which are published or combined, the epics, links and combines) and 6 hours
  have passed (`THEORY_GAP`), or right away when a human asks (`data/THINK`, from the UI's Run Theorist button
  or `python run.py theorize`). Its last turn is in `data/theory.json`, shown under the inspiration box and by
  the chat assistant's `theorist_notes`. A failure is noted there, waits out the gap, and never stops the loop.
  Every third turn (`orchestrator.BOARD_EVERY`; `theory.json` counts turns, failed ones and a human's asks
  included) is a board turn instead (#69), unless a human asked for that turn, which is always for ideas: `agents.tidy_board` (`theorist.tidy_board`, the same model, one
  structured call, no tools) reads the cork board as text with no style (every note's id, stage, title, x, y and
  size, every string, and a rough character grid of where the clusters sit) and replies with note moves and sizes
  and why; nothing about epics, missions, combines, links or the line. Code holds it to the board
  (`theorist.settle_board`): known notes only, each once, at most `MAX_MOVES` (12), sizes clamped, positions kept
  within `BOARD_MARGIN` of the board's edges, and a note dropped on another nudged to the nearest free spot; what
  it drops is noted. `orchestrator.apply_board` saves the moves `by="theorist"` on the main thread between tasks
  (it may move a note a human dragged) and keeps the last turn that looked across the desk under `"ideas"` in
  `theory.json` (`store.last_ideas`), so the notes show the board turn and the turn before it. Its gating is the
  same as any turn's.
- Connecting dots (the Theorist): two kinds, both in their own tables (`links`, `combines`), never on the mission,
  so a worker saving the mission it is busy with can't save over one.
  - Links (`contracts.Link`, one row per pair, `a < b`): two missions kept separate but connected, with why: two open
    missions, or an open mission and a published article (`contracts.is_open`: not DONE, not COMBINED). Never two
    published ones, a mission and itself, a pair already linked, or one combined away. They are drawn right away
    (`orchestrator.link`; they change no mission, only what research sees), at most 3 a turn. A human can link by
    hand (a mission page's Connections, `run.py link`) or unlink (`orchestrator.unlink`, `run.py unlink`); an unlinked
    pair stays as `removed`, so the Theorist never draws it again. Research gets each linked mission's topic and
    brief so far, or a linked article's dek, open possibilities and cited pages, with why (`related.connections_block`,
    before the similar published articles, which then leave out the linked ones). Once both are published, each
    article's page lists the other under "Connected articles" (`publisher.connected`); publishing one redraws the
    other, and unlinking redraws both.
  - Combines (`contracts.Combine`): 2 to 4 missions not yet being written (`contracts.COMBINABLE`: queued,
    researching, brief ready, parked) that are really one question, folded into one bigger mission with a topic,
    an objective and an epic (one of its new epics, an active epic, else the one its members share). At most one a
    turn, its topic new to the desk apart from its members, no member in another proposed combine, and never a set a
    human has seen. It is *proposed*: it waits under Needs you on the home page (the strip counts it at the epic
    review) or `run.py combines`, and auto mode approves it like an epic, waiting while a worker is busy with a member.
    `orchestrator.approve_combine` queues the bigger mission (`Mission.combines`; its brief still needs review), sets
    each member to `State.COMBINED` with `combined_into`, and passes the members' links on. Members that reached
    writing meanwhile carry on; with fewer than 2 left the proposal is dropped. Rejecting keeps it as `rejected`, so it
    is never proposed again. Research on the bigger mission gets each member's topic, objective and brief so far (or
    its last failed brief). A COMBINED mission is out of the flow (no role, not in progress, left out of the
    Theorist's desk) and its page links to the mission that took it over.
- Library: `library.py` (code, no chat model), retrieval (RAG) over the desk's own work for the Theorist and
  Researcher Chat, reached by agents only through its MCP server (`library_tools.py`, `library` in `mcp.json`, one
  tool: `search_library(query, epic, mission)`). Its passages (`library.passages_of`): each mission's topic and
  objective, its brief's thesis, each claim with the quotes it cites, counterpoint and uncited sources, and its
  article's title and dek and sections (long ones cut into `CHUNK_CHARS` pieces), each indexed under its mission's
  topic; nothing from outside the desk, so a passage is background, never a source. Lightweight: one SQLite file
  (`data/library.db`, on this machine only), plain Python, no vector database or numpy (about 0.6 s a search at 3,600
  passages with 768-dim vectors). It always ranks by words (BM25) and, with an embedding model, by meaning (cosine),
  fused by reciprocal rank; no mission fills more than half the list while others match. The embedding model is
  config (`settings.embed_model`: the Settings page's Library section, then `MISSION_LOOP_EMBED_MODEL`, else none, which
  ranks by words only): an Ollama model (`/api/embed`) or `<provider>/<model>` for a connected provider with an
  embeddings API (the OpenAI shape's `/embeddings`; `settings.PROVIDERS` marks Anthropic, DeepSeek and Groq
  `"embed": False`). The Settings page checks a model with one embedding before saving it. How many passages a search
  returns is a pick from `settings.TOP_N_CHOICES` (3, 5, 8, 10, 15, 20; default 5, `settings.library_top_n`). Every
  search first syncs the index with the desk (`library.sync`: new passages added, changed or gone ones dropped, other
  models' vectors dropped), then embeds what has no vector for at most `EMBED_TIME` (60 s); the rest ranks by words
  until a later search or Index now on the Settings page (or `python run.py library`) embeds it. A failed embedding
  call ranks by words and says why. Embedding calls are made there, not in `agents/llm.py` (which is for chat
  models). Stub runs never embed. `library.TRANSPORT` lets tests block the network.
- Mission finder: `agents/finder.py`, once each time the loop runs out of model work for existing missions. It works from the
  human's inspiration line (`data/inspiration.txt`; no line means it is off). The small model runs at most
  5 searches from 2 randomly drawn lateral angles, then the main model proposes missions. Code keeps at
  most 3, each resting on a URL the searches returned and not too close to an existing topic. They are
  queued and researched straight away, and still go through human brief review. A finder failure is
  logged and never stops the loop.
- Epics: every mission can sit in one epic, a 1 to 4 word thematic title for a long-running line of
  inquiry (`epics` table; `Mission.epic` is its slug, which never changes when the title does). Humans
  name an epic when adding a mission; a new name makes a new, active epic. The finder sees each active
  epic with a few of its topics and must give every mission the best existing fit. It may also propose
  a new epic, but code keeps that only when 2+ missions from the same search share it; the proposal then
  waits on the UI home page (approve, rename and approve, move to another epic, or reject, which puts
  each mission back in the finder's best fit, `Mission.epic_fallback`). Epics show as pills on the
  board and the published site (approved epics only); renaming redraws the published pages.
- Workers: two, set on the Settings page (`settings.WORKERS`, saved in `data/settings.json`). The
  Research Assistant (the small model: `assistant_model` in settings.json, else `MISSION_LOOP_SEARCH_MODEL`) does
  the "assistant" role: searching and reading. The Principal Investigator (the big model: `pi_model`, else
  `MISSION_LOOP_MODEL`) does the "pi" role: the brief, outreach, the article and the finder's picks. The loop runs them on
  two threads (`orchestrator.work`): each takes the next task for its role (`orchestrator.role`), so one
  mission's search runs while another's article is written; steps with no model run between tasks on the
  main thread. A worker switched off hands its role to the other (`settings.worker_for`), which then works one
  task at a time; one always stays on. Each worker also has a count (1 to `settings.MAX_COUNT`, 8; the − / + stepper
  or a typed number on its card, saved as `<worker>_count` only above 1): that many copies, seats, run its role at
  once, each on its own mission and all on its one model (`settings.seats()`: `assistant`, `assistant-2`, ...,
  `pi`; `seat_worker` and `seat_name` map back). The loop reads the seats every sweep, so a new count applies at its
  next look for work; every copy shares its worker's rounds in `_pick`, and the Theorist and finder still run once.
  Ollama runs as many requests at once as its `OLLAMA_NUM_PARALLEL` allows and queues the rest. Model choices come from the model providers (`llm.catalog`: the Ollama at
  `settings.ollama_host()`, which every Ollama call uses, set on the Settings page or with `OLLAMA_HOST`, then each
  connected cloud provider's models). A cloud provider runs as many requests at once as your account's rate limit allows. `data/activity.json` holds one entry per seat; the UI
  names an extra seat "Research Assistant 2" and gives it its worker's crew of researchers.
- Research: `agents/researcher.py` (Ollama + MCP search/fetch), checked by `checks/validator.py`, in two
  steps that can be done at different times. `gather` (Research Assistant) runs the search/fetch tool loop
  for at most 15 minutes (`SOURCE_TIME_LIMIT`) and saves the transcript as the mission's `evidence` artifact
  (`Mission.gathered`); a research attempt counts from here. `write_brief` (Principal Investigator) writes the
  brief from it, then the evidence is dropped. A stop during the search pauses it (see Stopping and progress). The search loop's
  `Guard` answers calls that can't help without running them, saying what to do instead: the call just made, a
  search already run, a blocked site, a site that refused us earlier in the run; and a failed fetch says what it
  means (a 401/403 site is unreadable for the rest of the run; try its Wayback copy or another site). After
  `MAX_REFUSED` (3) refused calls in a row the search ends with what it found. In the last `LOW_BUDGET` (10)
  calls or third of the time, each result says what is left (the brief writer doesn't see that note).
  The brief is written in two steps, so no reply is long and the first is kept: step 1 picks the thesis and
  sources (`SourcePick`), and code checks each quote against its page right away (`validator.source_problem`;
  a page that won't load is checked against its Wayback copy, which is then cited). Failed sources go back with
  what the check found (the closest real sentence, an archived copy) for up to 2 rounds of fixes (`SourceFix`);
  the ones still failing are dropped, and fewer than 3 supporting websites left fails the attempt with its draft
  and notes (`BriefFailed`). The passed sources are saved on the mission (`Mission.brief_sources`) until the
  attempt ends (the raw pick, `Mission.brief_pick`, is saved before its checks). Step 2 writes the claims and counterpoint (`ClaimSet`) citing only
  those sources; code fixes what it safely can (repeats dropped, a claim worded as a possibility labeled one),
  feeds back the rest with the claim numbers (`validator.claim_problems`, each problem once) for up to 2 rounds,
  then drops claims that still break a rule. `validate` is still the gate on the whole brief. `page_tools.py` adds `find_in_page` (exact sentences from the text
  the validator checks), `page_links` (a page's links to other sites) and `wayback_versions` (the Wayback
  Machine's copies of a page: the ones that changed, spread over time, and the one nearest a date), and
  requests only public addresses. `wayback.py` (code, no model) makes an archived copy work like a live page:
  a snapshot address counts as its original site for domain counts and the block list, and is read as the
  raw copy, without the archive's toolbar. When `find_in_page` or `page_links` can't read a live page, they
  read its latest Wayback copy and say which address to cite; when a cited page can't be fetched, the
  validator's note names its archived copy. The prompt sends the agent to the archive for sites that are
  gone and to check whether a page changed what it says. Published pages label archived sources with
  their date. `related.py` (code, no model) adds up to 3 published articles most like the mission by
  word overlap, any epic (its own epic ranks a little higher), with the possibilities they left open
  and the pages they cited. They are background only: the site never leaves this machine, so they
  can't be cited, but their cited pages can be fetched again and cited fresh. Research never looks for people to
  contact: the Outreach agent does, after the brief is approved.
- Writing: `agents/writer.py`, checked by `checks/article_check.py`, in steps: a plan (`ArticlePlan`: title,
  dek, the claim box, 3 to 5 sections, which claims each carries, which is the counterpoint), then each section
  on its own,
  seeing the plan and the sections before it. Code checks each piece as soon as it is written
  (`article_check.plan_problems`, `section_problems`: citations, unknown ids, links, quotes not in a source
  quote, with the closest real one shown, possibilities it must carry, the counterpoint's citation) and sends back
  only that piece's problems: one round for the plan (then code places unplaced possibilities and adds a missing
  counterpoint section), up to 2 for a section (then the version with the fewest problems is kept). The plan and
  every finished section are saved on the mission (`Mission.article_draft`): a stop or crash resumes after the
  last saved section, and after a failed article check only the sections its notes name are rewritten (all of
  them for a problem not tied to one section), each shown its last version and its problems. A passed article,
  a human's send-back and a push-through clear the draft.
  The brief stays as it is; the article is for readers, so the prompts ask for plain language (short sentences,
  active voice, no filler, each possibility said once with "It is possible that" and no extra hedging, since the
  page labels it) in a fixed order: the claim box (`Article.claim`, 1 or 2 cited sentences: who did what and why
  it matters, shown above the body as "The claim"), an opening section that starts with a short scene built only
  from cited facts and then states the claim (`writer.OPENING`), the evidence (`EVIDENCE`), the counterpoint, and
  the fine print last (`FINE_PRINT`: what the sources don't establish). The words that carry a claim (who did what)
  are in `**bold**` (`contracts.BOLD`; rendered `<strong>`, kept as Markdown in the plain text copy). Code drops
  bold from the title, dek and headings and stray asterisks (`contracts.tidy_bold`), and a quotation matches its
  source with bold ignored. The claim box is checked like a paragraph (`article_check.claim_problems`); one still
  failing after the plan's fix round is left out (`_settle_plan`), so it never fails the article. Articles written
  before it have `claim` empty and no box. Articles cite sources by short id (`[s1]`).
  A source whose id isn't one (a model sometimes uses the URL) gets the next free `s<n>` whenever a brief
  or source pick is read (`ResearchBrief._short_ids`, `SourcePick._short_ids`, so saved briefs are fixed
  too), and each written section has a bracketed citation of a brief source's URL turned into its id
  (`contracts.cite_by_id`) before it is checked; any other URL still fails.
- Article rating: `agents/rater.py` (`agents.rate_article`), the one judgement the code check can't make. An article
  that passes the article check is kept on the mission (`Mission.checked_article`), which stays WRITING with role
  "assistant" (`orchestrator.role`), so the Research Assistant rates it as its own task (`orchestrator._rate`) while
  the Principal Investigator moves on. One structured call on the small model, no tools: it reads the article and the
  brief's sources it cites (site, title, quote, stance) and scores 1 to 10 (`contracts.RATING_SCALE`), each after its
  reason (`RatingReply`, clamped by code): `logic` (the strength of the claim and its logic), `intrigue` and `sources`
  (their credibility). Code decides (`ArticleRating.passed`): a total of `RATING_FAIL_AT` (20) or less out of 30 fails
  the writing attempt like a failed check (`orchestrator._article_failed`): the notes are the rating
  (`ArticleRating.notes`), `RejectedArticle.rating` marks it, the draft is dropped so the writer starts from a fresh
  plan, and its prompt shows the last article with the editor's reasons (`writer._notes`); after 3 attempts the
  mission parks (counted at the rating's check on the strip) and may still be pushed through. A passing rating goes to
  review with the article (`Mission.article_rating`: the review card, the mission page's Rating section, the home
  card, `run.py show`, the chat's `mission_status`). A rater that crashes sends the article to review unrated
  (`ArticleRating.error`, "Not rated: ..."): the code check passed, so nothing is held back for want of an opinion. A
  stop keeps `checked_article`, so the rating runs when the loop starts again. It is never shown on the published site.
- Every model call goes through `agents/llm.py`, which keeps it bounded (cloud models go on from there to
  `agents/providers.py`; the numbers below are Ollama's, see Model providers for theirs). Before the call, tool results
  are cut to fit the 32k context (the longest pages lose their ends first; instructions and the final
  request stay whole), because Ollama otherwise drops the start of the prompt or, on the MLX runner,
  lets the model run on forever. The search loop cuts deeper (`llm.compact`, to 70% of the room) and keeps
  its transcript cut, so the next turns only append and Ollama reuses its prompt cache instead of rereading
  ~25k tokens every turn; results the model hasn't read yet stay whole. The brief writer gets the transcript
  without the small model's thinking, failed tool calls cut to one line and repeated results dropped. Replies are capped at `REPLY_TOKENS` (8192; a brief is about 4k) or
  `TURN_TOKENS` (2048) for tool turns, and at `REPLY_TIME_LIMIT` (10 minutes; a brief takes about 2).
  A reply that hits either limit raises `llm.Overrun`, and so does Ollama giving up on a model that keeps
  repeating tokens ("token repeat limit reached"): a stuck search turn ends the search, and a stuck structured
  reply keeps whatever valid JSON it wrote before (checked as usual), else the attempt fails. `llm.ask` is the
  shared structured call: one repair round for malformed JSON, the Overrun salvage, and stop handling.
- Publishing has two parts: the local site, the foundation every article is written to first, and channels,
  everywhere else it goes, each posting from the local site's feed. Where each article went is recorded per
  mission in `Mission.channels` (key -> when and where):
  - `site`: `agents/publisher.py` writes `site/<slug>/index.html` and `meta.json` (with the article as plain
    text, `render.article_text`), then rebuilds `site/index.html` and the feeds `site/feed.json` (JSON Feed 1.1)
    and `site/feed.xml` (RSS 2.0) from the `meta.json` files, newest first, full text. Always on; reaching it is
    what makes a mission DONE. Feed links are relative unless a site address is set (the UI's Settings page
    saves it in `data/settings.json`, which wins over `MISSION_LOOP_SITE_URL` in `.env`); entry ids are the
    slugs either way. Each time the loop starts working it gives older `meta.json` files their text and rebuilds.
    The site is meant to be browsed on its own (GitHub Pages): every page has Articles and About links, the index
    filters by tag (an approved epic is called a tag on the site; `?tag=` in the address, so an article's tag pill
    links to its list), and an article page has a link back and its newer and older article, read from
    `feed.json` so pages written once stay current. `site/about/` shows the pipeline strip (`flowchart.py`, the
    same snake the UI draws, always with the human reviews: auto mode is the UI's own switch) playing a made-up run, a few seconds per stage with both workers busy
    (`assets/about.js` drives it through `assets/flow.js`; `data-demo` means nothing is polled), says what each
    agent does (`render.AGENT_NOTES`), and has a floor where a researcher or two drops by for a short visit and
    says something (`window.RESEARCHERS_PACE`). Rebuilding the index rewrites it and copies those three scripts to
    `site/assets/`, and starting the UI copies any that differ from the code's (`publisher.refresh_assets`), so an
    updated script reaches the site without waiting for the next article. The UI serves `/assets` and `/site` with
    `Cache-Control: no-cache` (`ui.FreshFiles`) and writes every script's address with a stamp of its contents
    (`ui.asset`, `ui.script`: `/assets/researchers.js?v=1a2b3c4d`), so a browser never keeps drawing with an old script. `about` and `assets` are the site's own folders, so no mission gets them as its slug
    (`contracts.SITE_FOLDERS`).
  - Every other channel posts from the feed (`agents/channel.py` says what a channel module provides; add it to
    `CHANNELS` in `agents/__init__.py`; `agents.channels()` is those plus the custom ones). When the loop starts,
    whenever it publishes and whenever a human asks to publish an article again, `orchestrator.post_to_channels`
    runs a publishing pass, and during long model work at least every `PASS_EVERY` (5 minutes): pages asked to be
    rewritten, then each enabled channel works through its whole backlog: entries a human asked for first (even ones
    it already posted), then feed entries it hasn't posted yet, newest first, refused ones last, until its allowance
    runs out. So a new channel with no cap (GitHub Pages, a folder, a webhook) gets every past article on its first pass, while
    1F916 takes one a day and Bluesky one per 10 minutes until they catch up. A pass gives up on a channel after
    `MAX_REFUSALS` (3) refusals.
    A refusal costs nothing (the reason goes in `Mission.channel_errors` and the next entry is tried); network or key
    failures lose nothing and are retried the next time something is published or the loop starts again. Stub runs have no channels.
  - Publishing again: the Published page (or a published mission's page) ticks the local site and any channels
    and records them in `Mission.publish_requests` (`orchestrator.request_publish`). While the loop runs, its next
    sweep sees the new request and runs a pass, within seconds even mid-task. With the loop off the UI does it
    itself on a background thread (`ui.publish_now` -> `orchestrator.publish_requested`): only what was asked for,
    the rest of each channel's backlog waits for the loop, and a leftover stop doesn't block it. Every pass, the
    loop's or the UI's, holds `data/publish.lock`, so two never overlap. "site" rewrites the page and feeds (the
    article keeps its first `published_at`). A channel's own politeness gap is skipped for a request
    (`MANUAL_SKIPS_WAIT`, Bluesky) but a real cap is not (1F916's daily post); a request that fails stays and
    goes first on the next pass. Requests for a channel since switched off are dropped.
  - custom: `agents/custom.py`, any number, added on the Settings page and saved in `data/settings.json`
    (`settings.custom_channels`): a name, a webhook (POST) or a folder (one file per article), and a format
    (a JSON Feed 1.1 item, Markdown or plain text). Keys are `custom-<id>`; no cap. A webhook's address is never
    recorded as where an article went (it often holds a token): a JSON reply's `url`, else its host.
  - `1f916`: `agents/f916.py` posts the feed entry's title and text to 1F916 as our citizen. The server to join is
    config (`settings.f916_url`: Settings page, `MISSION_LOOP_1F916_URL`, else https://1f916.ai); the key file records
    the server that issued it (`api`) and posting always goes there (`f916.api`). On only
    when `data/1f916.json` has a key (`python run.py join-1f916 <handle>`, once). The site allows one post per
    UTC day (`data/1f916_state.json` remembers when it is spent), so a just-published article goes out right after
    publishing if the day's post is free, and the rest wait, newest first.
  - `github`: `agents/github.py` keeps a separate public GitHub repo identical to `site/`, so GitHub Pages serves
    the site. Set up once on the Settings page: Connect GitHub (GitHub's device flow: the page shows a code to
    enter at github.com/login/device; scope `public_repo` only; token saved to `data/github.json`, mode 600), then
    create the site's repo (default `<login>.github.io`, which GitHub serves with nothing to switch on) or paste
    the link of one. Linking turns Pages on where the API allows it, else names the repo settings page to do it in,
    and refuses private repos, repos it can't push to and repos holding other files (the site replaces everything
    except `KEEP`: CNAME, README, licence). The channel's `sync` runs before its queue on every channel turn (an
    optional hook in `agents/channel.py`) and pushes whatever changed as one commit through the Git Data API (only
    changed files are uploaded; no git needed; `data/github_state.json` remembers the last push, so an unchanged
    site costs no request). Each entry's post records its page there. While no site address is typed in, the
    Pages address is the site address (`settings.site_url`), so the feeds use it and Bluesky links to it, and
    Bluesky holds an entry until GitHub has it. The sign-in uses mission-loop's OAuth app
    (`settings.GITHUB_CLIENT_ID`; a client ID is public, there is no secret); a copy without one asks for one on
    the Settings page (`MISSION_LOOP_GITHUB_CLIENT_ID` in `.env` also works).
  - `bluesky`: `agents/bluesky.py` announces the feed entry on Bluesky as one post (never a thread of replies), at
    most 300 characters and with no model or labels (the article page carries "Possibility, not verified" and a
    failed check): the title, cut on a sentence if ever too long, then "Full article: site · 1F916", whose words link
    (facets) to the article on the site when a site address is set (Settings page or `MISSION_LOOP_SITE_URL`, else the
    linked GitHub Pages address) and to its 1F916 post (1F916 comes first in `CHANNELS`, so a fresh one counts),
    whichever exist. The post gets a link card with the title and dek (the site, else 1F916). With GitHub Pages on,
    an entry waits until it is on the site.
    On only with a handle and app password (never the account password): the
    Settings page checks them with Bluesky and saves them to `data/bluesky.json` (mode 600, wins over `.env`'s
    `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`); `python run.py check-bluesky` logs in without posting. A login saved on
    the Settings page keeps the server it was checked against; a `.env` login uses `BLUESKY_SERVICE` (default bsky.social). After each
    article it waits `POST_GAP` (10 minutes), so a backlog goes out at most one every 10 minutes. `data/bluesky_state.json`
    holds that wait and the last failure, which the UI and `status` show.
- Auto mode: the switch at the top of every UI page (or `python run.py auto on|off`; `settings.auto_mode`, saved in
  `data/settings.json` only while on) approves everything except publishing. `orchestrator.auto_approve` approves
  every brief waiting for review, every proposed epic (the Theorist's and the finder's) and every proposed combine,
  logged "by auto mode"; the loop runs it at the top of every sweep, so a new brief moves on within seconds, and an
  epic or combine with a mission a worker is busy with waits for a later sweep. Turning it on approves what already waits right away, and a brief
  pushed through while it is on is approved too. Articles are never touched: publishing always waits for
  `review`. No "brief ready" notification is sent while it is on. The flow chart draws the two reviews it takes over
  as green "Auto-approve epics" and "Auto-approve brief" steps; every code check and the article review stay as they are.
- Outreach (`agents/outreach_agent.py`, `contacts.py`, `outreach_tools.py`): off until switched on under Settings →
  Outreach (`settings.outreach_on`: `outreach` in settings.json, else `MISSION_LOOP_OUTREACH=1`). While on and a ticked
  channel is set up (`agents.outreach_channels()` = `contacts.ready_channels()`; stub runs have none), an approved brief
  goes to `State.OUTREACH`, a Principal Investigator task (`orchestrator.role`): the agent reads the brief and, with
  `search`, `fetch`, `pages` and the `outreach` MCP server, finds up to `settings.outreach_contacts()` (default 3, at
  most 6) people who know first-hand and calls `send_outreach {channel, address, name, topic, question}` for each
  (`MAX_CALLS` 30 in `TIME_LIMIT` 10 minutes; `researcher.tool_loop` with `role="pi"` and an `OutreachGuard`). It never
  writes a message: code (`contacts.send`) fills `contacts.TEMPLATE` ("Hi {name}, I'm an AI Agent on a research mission
  about {topic} and I need your help answering this: {question} You can learn about my missions here {site}", the site
  part only while `settings.site_url()` has one), checks, sends and records it as an `OutreachRecord` in the mission's
  `outreach` artifact the moment it goes, so a crash or stop loses nothing. A message that fails is not sent and the
  agent is told why and what to fix, and tries again or picks someone else: a channel not ready, a malformed address,
  our own address, a name that isn't a first name (or a title and surname), a topic over 12 words, a question that
  isn't one short question ending in "?" (10 to 200 characters, no links), someone already asked on this mission or
  about another mission within `contacts.GAP` (a week), the mission's count reached, a Bluesky post over 300
  characters (it says by how much), a handle that doesn't resolve, a 1F916 handle that never commented on our posts,
  a mail server's refusal. The guard refuses, before the tool runs, an email address that wasn't in any page or search
  result of the run. The mission comes from `MISSION_LOOP_MISSION`, which `Toolbox(env=...)` gives the server, never
  from the model. Channels: `email` (SMTP from `data/email.json`, saved on the Settings page after a login check,
  `contacts.check_email`; port 465 SSL, else STARTTLS; replies read over IMAP by `In-Reply-To`, the quoted message cut
  off), `bluesky` (a new post from the publishing login with a mention facet for the person's DID and a link facet on
  the site's address, shown without its scheme; replies are the person's posts in its thread, read from
  `settings.BLUESKY_APPVIEW`; `find_on_bluesky` searches people there) and `1f916` (a reply under the person's latest
  comment on one of our posts, `@handle` first; `list_1f916_commenters` lists who can be asked; replies are their
  comments under ours). Outreach never blocks: a run that asks nobody or crashes moves the mission straight to writing
  (noted); only a stop keeps it in OUTREACH to carry on, told who was already asked. Then `AWAITING_COMMENTS` waits
  `settings.outreach_wait_hours()` (default 48, at most 336), asking the channels for replies at most every
  `orchestrator.REPLY_CHECK_EVERY` (10 minutes; `Mission.replies_checked_at`) and moving on early once every contact
  answered, straight to the writer (farthest along goes first). The wait is code only, no model: each check reads the
  replies and gives every contact who answered one canned reply (`contacts.thank`: `contacts.THANKS` plus the site
  link, in their thread on the same channel: an email `In-Reply-To` their answer, a Bluesky reply to it, a 1F916
  comment under it; `OutreachRecord.thanked_at`, `thanks_where`), once however many times they answer. A channel
  that can't be read or written is skipped and tried at the next check. Replies (untrusted text, kept up to
  `REPLY_MAX`) reach the writer as contacts' comments it may quote. The mission page lists who was asked, where, the
  exact message and their replies; the flow strip shows Outreach, its check and Replies only while outreach is on;
  the setup check (`doctor`) flags outreach that is on with no channel ready.
- Claims have a `kind`: `fact` or `possibility`. The prompts treat every source as part truth, part spin.
  Research explores widely, drops only what specific evidence contradicts, and leads with pointed
  possibilities that each rest on a cited detail. A `fact` means only what the record directly shows (what
  a document says, what someone said). Code can't verify a possibility, so it enforces the label instead:
  possibilities must say "It is possible that", a brief needs at least one fact, and the article marks
  each one "Possibility, not verified".
- The thesis must be checkable (who, what, when, how much). Research must look for the case against it,
  but a contradicting source is optional, since a broad thesis often has none: any it cites is verified
  like every source, and the brief always needs a counterpoint. Without a contradicting source the
  counterpoint is the skeptic's objection, marked "No source found makes this case" in the brief, and the
  article says so. The hard gate is the claims: cited, quotes found on the page, 3 supporting websites.
- A parked mission can be pushed through: its last failed brief or article moves to the human review
  that follows, and stays marked as failing its check (`unchecked_brief` / `unchecked_article`), including
  on the published page. This needs a failed draft; if every attempt crashed, only requeue works.
- `agents/__init__.py` is still the only switchboard. Validators never call the LLM.
- Tools are MCP servers, always: a new agent tool goes in a small server module registered in `mcp.json`
  (like `page_tools.py`, `status_tools.py`) and is reached through `agents/toolbox.py`, never imported directly,
  so other clients can use it later. Script paths in `mcp.json` are relative to `mcp.json`, which `toolbox.CONFIG`
  finds next to the code whatever the working folder. A server this machine can't run is skipped, not fatal: its
  command isn't installed (`toolbox.WHICH`, swapped in tests) or a variable it lists under `requires` is empty (the
  search server's `BRAVE_API_KEY`, which `settings.search_key` also reads from `data/search.json`). `Toolbox.skipped`
  says which and why; the finder stops with that reason instead of searching with no tool.
- Sharing the tools on this network (`mcp_share.py`, Settings -> MCP server): every tool Researcher Chat has, and so
  every tool the Theorist and the other agents use, as one MCP server other apps connect to (Claude Code, Claude
  Desktop through `mcp-remote`, Cursor, VS Code, LM Studio, another machine's agent). No tool is written twice: the
  gateway opens exactly the chat's servers (`assistant.servers()`, so a new server or tool there is shared too)
  through `agents/toolbox.py` and serves their tools over MCP's streamable HTTP at `http://<LAN address>:<port>/mcp`
  (`settings.mcp_port`, default 8780). LAN only, no cloud: it listens on every interface, `Guard` refuses any client
  address not on this network (`on_this_network`: loopback, private, link-local; never public nor 100.64/10) and any
  request without `Authorization: Bearer <settings.mcp_token()>` (made on first use, `data/mcp_share.json` mode 600,
  read each request, so New token takes effect at once). Off unless switched on (`settings.mcp_share`, saved only
  while on). The UI runs it on a thread beside itself (`mcp_share.start`/`stop`; started when `ui` starts if on),
  restarts it when the web search key changes (the tool list is read at start), and the section shows the address,
  the token (hidden until Show), the tools it shares and what it skips, and ready-to-paste setups
  (`ui.mcp_setups`). `python run.py mcp` runs it alone in the foreground. Writes go through the chat's checks;
  approving, publishing, start and stop stay UI buttons, never tools.
- Researcher Chat: `agents/assistant.py` answers the human's questions about the loop and the desk's findings, looks
  things up on the web, and can do everything the Theorist can (read the desk as it does, search the library, evolve
  the line, propose epics and combines, link missions, queue a mission, ask for its turn), through the Theorist's own
  checks (`theorist.settle_epic`, `settle_combine`, `settle_links`) and the same orchestrator functions
  (`orchestrator.propose_epic`, `propose_combine`, `link`, with `by="chat"`; proposals still wait for the human): the chat bubble at the bottom right of every UI page opens it in a panel over the left third, or
  `python run.py ask "..." [--model]`. It needs Ollama but not the loop, so it works with the loop stopped. The panel
  has a model picker (installed Ollama models that can call tools; the default is shared with the loop, any other is
  loaded beside it), and keeps the conversation, the unsent draft and a question still being answered in that
  browser tab only (sessionStorage), so refreshes, moving between pages and closing the panel lose nothing; Clear starts
  over and Copy puts the conversation on the clipboard as text. A question carries an id, and the UI holds the last 20
  answers in memory (`ui.CHAT_JOBS`, never on disk), so a page that navigates mid-answer hands it to the next page
  (`/chat/answer`). With
  animations on, a pixel researcher for the picked model walks in at the top of the panel and says which model it
  is (`Guide` in `assets/researchers.js`, sent by the chat's `chatguide` event; the script loads the first time the
  panel opens): a worker's own model brings someone from that worker's crew, any other model the tenured crew from
  20B parameters up (`assistant.crew`, `BIG_MODEL`), else the young one, and the model's family picks who, so one
  model always brings the same person. Picking another model sends the last one walking off. It is not a worker: a
  short prompt that explains the app, at most 8 tool calls in 3 minutes, tool results cut to 2,500 characters (web
  pages and searches 4,000), only the last 4 messages the page sends back, no memory and nothing saved. Its tools
  are MCP servers: the `loop` server (`status_tools.py`: `loop_status`, `list_missions`, `mission_status`,
  `agent_steps`, `search_missions`, `publication_status`, `list_epics`, `add_mission`, `set_mission_epic`, `theorist_notes`,
  and the Theorist's own moves: `desk_overview` (the desk exactly as the Theorist reads it, up to `MAX_DESK_CHARS`),
  `set_inspiration` (logged `by: chat`), `ask_theorist`, `propose_epic`, `propose_combine`, `link_missions`,
  `board_overview` and `arrange_board` (the Theorist's board turn: the board as it reads it, and moves held by
  `theorist.settle_board`, saved `by="chat"`), and
  `setup_status` (the setup check, `doctor.py`); `mission_status` names a mission's links and
  combines, `theorist_notes` the Theorist's last combine, links and library searches), the `library` server (`search_library`), the researcher's web tools `fetch` and `pages` (`find_in_page`, `page_links`,
  `wayback_versions`), and `search` (Brave web and news search) when `BRAVE_API_KEY` is set; without a key the
  panel says it can only read pages it is given. Mission listings name each mission's epic. All loop tools but
  `add_mission`, `set_mission_epic` and the Theorist's moves only read the database, `data/activity.json`, the channels' state files, the
  trails and `data/loop.log`, so asking never touches a running agent. `add_mission` queues a new mission like the
  UI's Add mission box (it still needs brief approval); `set_mission_epic` puts any mission, published ones
  included, in an epic like the Epic field on its page (`orchestrator.assign_epic`). Neither changes a mission's
  stage or work. The panel renders a small, escaped Markdown subset in replies (bold, italic, code, links, lists).
  By default it uses the small model with the loop's options (`settings.chat_model`: `chat_model` in settings.json,
  then `MISSION_LOOP_CHAT_MODEL`, else the Research Assistant's model), so Ollama reuses the loaded copy instead of
  reloading it. With no model chosen anywhere (a fresh clone) it uses the first installed model that can call tools,
  else a connected cloud provider's first, so it can help with setup; with none at all it says how to get one.
- Stopping and progress (`stopping.py`, `checkpoint.py`): the database otherwise changes only when a task ends,
  so long tasks save each finished piece the moment it is done through `checkpoint.save`, and the orchestrator
  writes it (`orchestrator.save_progress`, set up per worker task in `_as_worker`). A checkpoint writes only the
  progress fields (`PROGRESS`: `brief_pick`, `brief_sources`, `article_draft`) onto the stored mission, plus any
  artifact given, so it never undoes a human's change made mid-task; everything else is saved when the task
  ends, as before. A stop or a killed loop loses at most the piece in flight:
  - Search: the transcript is saved after every answered turn (artifact `evidence`, `finished=False`, with its
    `attempt` and `seconds`). A stop pauses the search (`Mission.search_paused`) instead of handing a half
    search to the brief writer; the next start carries it on (`gather(m, now, resume)`), as the same attempt,
    with the tool calls and time it has left. A transcript a killed loop left behind carries on the same way; a
    finished one is used as it is; a crashed search's is deleted, so the retry starts afresh.
  - Brief: the source pick is saved as soon as the model writes it, checks stop between source checks (each can
    take seconds), and a resumed run checks the saved pick again instead of asking the model. Once step 1 is
    checked and saved, a stop ends the task before step 2 starts.
  - Article: the plan and each finished section are saved as they are done, and a stop never starts a new
    section. After a failed check, a section already rewritten before a stop isn't rewritten again.
  Pieces are short, so whatever is in flight when stop is pressed usually finishes inside the grace
  (`TURN_GRACE` 30 s for a tool turn, `GRACE` 120 s for a reply). The UI's mission cards say how far saved work
  got ("Carrying on the paused search.", "1 of 3 sections written.").
- Cork board (`board.py`, code, no model; `/board`, the Board button between Published and Settings): every
  mission and epic as a sticky note on one zoomable cork board, joined by string (#66, #67). A note gets a position
  the first time the board is read and keeps it (`board` table, `contracts.BoardNote`): a new epic takes the first
  free spot on wide rings round the centre, a new mission the first free spot spiralling out from its epic's note
  (missions in no epic round an Unsorted label, a combined mission's members round the mission that took them
  over), the same desk always placed the same way. Size means importance: from its stage plus a little per string
  (`STAGE_SIZE`, `PER_STRING`), unless a human or the Theorist set one (clamped to `SIZE_MIN`..`SIZE_MAX`). Strings
  are never stored: epic to mission, the active links, and mentions (`mentions` table, `contracts.Mention`, never on
  the mission): `source` (two briefs cite the same page; an archived copy counts as its original), `name` (a brief
  or article contains another mission's full topic or article title, `MIN_NAME` (4)+ words, or its slug written with
  its hyphens, whole phrase), both found by `board.find_mentions` whenever a brief or article is saved
  (`orchestrator._mentions`; display only, a failure is printed) and for an older desk the first time a board opens
  each server start (`find_all_mentions`: each mission read once, pairs checked once, one transaction; an artifact
  that won't load is skipped); and `background` (research was shown a related
  published article, recorded by `related.similar_block`). `assets/board.js` draws it (no library: one CSS
  transform for pan and zoom, strings in SVG, kept cheap so a zoomed board never shows flat patches: one shadow or
  glow on the whole strings layer, sized to the strings, never one per string; the laser pulse fades that layer, never
  each string; strings are kept and only moved ones rewritten; the view's `--gx/--gy/--gz` don't inherit; a look that
  didn't change draws nothing): wheel or pinch zooms round the pointer, every fit glides, a drag moves
  a note and its corner resizes it (`POST /board/move`, saved `by="human"`; only a resize fixes the size; the Theorist's board turn may move it later), a
  mission note opens a preview (title, thesis or objective, stage, a link to its page), and an epic note picks it:
  the bar at the top shows its missions and a degree selector (1 = its own missions, each step adds what is one more
  string away, All) that fades the rest and glides to fit. It polls `/api/board` every `POLL_SECONDS` and updates
  in place, so a new mission appears on its own. Every mission card and mission page has "On the board"
  (`/board?focus=<slug>`, zoomed onto it). Its look (#68) is the gear on the board or Settings → Display → Board, the
  same setting (`settings.board_style`, saved in `data/settings.json` only when not cork, sticky notes and red
  string): a surface (cork, paper, grid, night, fridge, arcade; grid, night and arcade move with the view), cards (sticky notes;
  fridge magnets, where a mission grows from rainbow alphabet letters while queued or researching, to word-puzzle
  tiles from the brief on, to a postcard once its article is ready, and an epic is a souvenir magnet; or hologram
  panels glowing in their stage's colour; or pixel cards in flat 8-bit stage colours with stepped borders), and what
  joins them (string, laser, wire, or pixel: right-angle elbows) in a preset or any colour (the gear and Settings → Display → Board both have the picker). A card's colour is its
  stage in every style but magnets, whose shape is; an epic's colour comes from its name. With Animations on, a note
  a model is working on gets `data-lab` and its worker (`data-worker`), so `assets/researchers.js` sends a pixel
  researcher from that worker's crew: on a sticky note it stands inside the card on its bottom edge (the busy note
  grows room there) and tilts with it; on the other styles it walks along the top, which stays level. Laser pulses and holograms
  flicker only while Animations are on. Board notes override the published site's `.note` and `.epic` styles,
  which share their class names.
- Step log: `activity.py` appends every agent step that ends (the seat's next `begin`, or `clear`) to
  `data/steps.jsonl` (`activity.LOG`, on this machine only, never trimmed): one JSON line with the seat, mission slug
  (`""` for the Theorist and the finder), agent, start, end and outcome: `done`, or `failed`/`stopped` with why
  (`activity.fail(e)` from the orchestrator's excepts, an `Interrupted` counting as stopped; `activity.outcome` for a
  failed source or article check or a paused search; the first outcome given stands). Under the pipeline strip
  (`ui.flow_status`, served in `/api/flow`'s `now`) the agents at work come first, then the last `RECENT_STEPS` (3)
  finished steps, each "Agent · mission · took 4m 12s · 3m ago" (`activity.recent` reads only the log's end), then Full log, which
  opens `/log` (`ui.log_page`): every step newest first, `LOG_PAGE` (200) a page, the ones at work on top, `?m=<slug>`
  for one mission (a mission page's History links to it). Display-only, like the activity file.
- Trails: `trail.py` keeps each mission's last 40 agent steps (model thinking and replies, tool calls, tool
  results, each cut to 400 characters) in `data/trail/<slug>.jsonl` (`_finder.jsonl` for the finder, `_theorist.jsonl` for the Theorist). Only the
  loop writes them: `activity.begin` starts a trail, `llm.chat` and `researcher.tool_loop` add to it. Display-only.

## Operating it

The easy way: `python run.py ui`, which opens http://127.0.0.1:8770 in your browser (`--no-open` doesn't; with a UI
already running on the port it opens that one and exits). It lists every mission, shows briefs and
article previews, and has buttons for approve / send back / publish / requeue / push through / add mission / save
the finder's inspiration line and to wake the loop. The top bar of every page has the Auto switch and, beside
it, the loop's play/pause button with its state (`ui.loop_button`; green and pulsing while the loop is off): play starts the loop, which runs in the
background, logs to `data/loop.log` and rests `settings.loop_every` minutes when idle (the Loop field on the
Settings page, default 5); pause asks it to stop after the agent at work saves (disabled while it stops). "Check for
work now" stays in the Missions page's loop panel while the loop runs.
The Missions page reads like the flow chart from its end: what needs you at the top, then everything in
progress closest to published first (publishing, writing, briefs, searches) and new missions last. Publishing
there is one card (`ui.queue_card`): how many articles are queued and for which places, leading to the queue on
the Published page. Published articles are not listed there, only the Published card at the very bottom of the
page, for anyone scrolling to find it. The Published button in the top bar opens that page too (`/published`,
with a link back): the local site, every channel, the queue (`ui.publish_queue`: each place's articles in the
order they go out, requested ones marked), and each article with where it went and boxes to publish it again. Its Settings page (`/settings`) is grouped into sections (`ui.SETTINGS_SECTIONS`: Setup, Workers with the loop's rest
between looks for work, Model providers, Research (web search and the library), MCP server (sharing the tools on this network), Display (appearance, chat colour, board,
animations, avatars), Publication and Publishing), listed with icons in a nav down its left (a bar across the top on a
narrow screen): a click glides to its section and scrolling lights the one in view (`ui.SETTINGS_NAV_JS`). Publishing has the local site (always on: feeds, the hosted address), the channels that are set up, and Add a
channel (1F916, the same `f916.register` as the CLI; Bluesky; custom); a fresh clone needs no settings. `ui.py`
only records human decisions through the same store/orchestrator functions as the CLI; it never runs
agents. It binds to 127.0.0.1 and refuses cross-site form posts. Every button is a form post the server redirects back from, so `ui.KEEP_SCROLL_JS` (on every page) scrolls back to where you were when it lands on the same page, and the done/error message is a toast over the page (`.flash`; a done fades, an error stays until clicked) whose `?done=`/`?error=` is taken out of the address. Every decision waiting on you (a brief, an article, a parked mission or a proposed epic) has one look (`ui.ask`, with the finger icon), In progress is grouped by stage (Publishing as one queue card, then Writing, Research, Queued), then come Inspiration and Epics. Adding a mission is one long slim card right under the pipeline strip (`ui.new_mission`: a plus and a pencil, "Add a mission") (grey) that opens a three-step wizard in a dialog (topic, objective, then epic and short name; green), which posts to
`/missions` with Add mission, or Add and start loop (`go=start`): the mission is marked `rush`, so the Research Assistant
takes it next, and the loop is started if it is off ("Add and research next" while it runs); the page's refresh holds off while it is open; every section heading has an icon (`icons.py`, inline SVG). The home page's pipeline strip (`flowchart.py`, shared with the published About page) never scrolls sideways: `assets/flow.js` shrinks it to the panel (`--s`) and, below a readable size, wraps it between units. It is always drawn,
unlit while the loop is off, as a snake (`flowchart.track`, `flowchart.lanes`, which `ui.flow_strip` and the About page share): the steps in runtime order are cut into
lanes at each human review. Lane 0 runs left to right from the top left (the Theorist) to the first review, a
square end on the right; lane 1 comes back right to left one level lower to the next review on the left; and so on,
each review spanning the two lanes it joins, so the snake turns through it; the last lane (Publish alone, one step
whatever the channels) ends bottom left. Agents are tiles with an icon and a line saying what they do (`flowchart.TILES`),
each code check a narrow shield tile right after the agent it checks (the article rating is an agent tile, Rating,
on the Research Assistant, after the article check, with its own check for the 20-or-less rule) (with a red count of missions parked there),
and each review shows how many missions wait on you. Researchers on the strip stand on their own step's row
(`FlowStage.row`). Every code check is on it (`flowchart.CHECKS`, whose
titles say what each holds its agent to): the Theorist's (`theorist.settle`) and the finder's (`finder._keep` and
the 2+ missions rule for an epic), which drop what fails and park nothing; the outreach check (with Outreach and
Replies, only while outreach is on), which refuses a bad message and parks nothing; the source check and the article
check. So is every human review: the epics both the Theorist
and the finder propose (after the finder's check; it also counts the Theorist's proposed combines), the brief and
the article. In auto mode exactly two of them,
the epic and brief reviews (`flowchart.AUTO_STEPS`), are drawn as automatic instead ("Auto-approve epics", "Auto-approve
brief": green, a bolt, no count); every check and the article review stay as they are. The strip's `data-shape`
marks the automatic ones, so an open page redraws the strip when the switch flips. The steps working right now are lit, one per
busy worker, read from `data/activity.json` (written by `activity.py`). `assets/flow.js` polls `/api/flow` every 2
s and relights the strip in place, and the mission cards below with it (green outline, "working on this now",
researcher visitors), one per busy worker, so they never wait on the page refresh, which holds off while you type or
chat; the rest of the page refreshes by swapping in fresh HTML around the live strip, never by reloading. Small pixel researchers (`assets/researchers.js`, decorative only) live on the cards of
missions a model is working on right now (none while no model runs) and on the strip, never more than four on the
page at once (one on each busy worker's lit step, one on each busy card). Each character is in one worker's crew (`worker` in
`ROSTER`: young ones the Research Assistant's, tenured ones the Principal Investigator's), and 80% of a card's
visitors (`FAVOR`) come from the crew of the worker running it (`data-worker`), the rest from the other crew; on the Settings page each worker card
has its whole crew standing about. A card's visitor keeps a timer: a random first wait, 15-30 s on stage, walks
off, then 10-20 s before the next; the longest-waiting card goes next, and a card nobody works on any more sees its
visitor walk off. Every arrival, the strip's included, comes one at a time, a random 12-20 s apart, so a full page
fills over about a minute; a lit step's newcomer goes ahead of the cards, and walks in at least 10 s after the one
it replaces walked off. Each busy worker's lit step
gets a researcher from that worker's crew, who follows the work instead: it stays on the lit step doing that
step's kind of work, runs after its mission when the mission moves on (sources, brief, check), and when the work
ends, moves to another mission or passes to the other worker it says a closing line about where the work went
(your review, parked, published, another try) and walks off, and a new one walks in for whatever started. Visitors
come from a shuffled cast and their lines from a shuffled deck of 500+ academic quips and memes, so nothing
repeats soon. Nobody is ever on the page twice at once (the chat's guide included), and the last three arrivals wait
while anyone else can come. Each character has a habit of their own between bits (`QUIRKS`: Sam's bubblegum, Quill's
giant eye through the magnifier, Pip's propeller hover, ...). A new sprite is a JSON spec, never code: `SPRITES.md` is
the rulebook (parts, pinned pixel maps, keyframe quirks, one size, faces stay drawn by code), the sprite studio the place to try one (`/sprites` in the app,
from Settings → Avatars; it is `tools/sprites.html`, which also opens from a served copy of the repo), and a page sets `window.RESEARCHER_SPRITES = {add, off}` to add sprites or switch any off. The avatar
studio (`/avatars`, `assets/avatars.js`) builds them: pieces drawn as your own avatar wearing them, a live preview,
and "Ask a model" (`agents/avatar.py`, `agents.edit_avatar`: one call held to a JSON schema of the real parts, thinking
off, a picture only to a local model Ollama says can see, every result checked in the browser with up to two repair
rounds; nudity refused before any model). Avatars and the on/off list live in `data/sprites.json` (`avatars.py`),
which `ui.page` hands to every page; Who visits switches anyone off. Between bits they stand about for 4-6.5 s (`rest`; a crew longer), sometimes taking a step or two, and
most bits pass without a word. They work through about 40 bits weighted by mood (the lit step on the strip): reading, digging and
typing, plus lab life like a chemistry poof, a model rocket, a rubber duck, a reviewer's stamp, a bird landing on
a head or the office cat; naps are rare.
The Settings page's Animations switch (on by default, saved in `data/settings.json` only while off) removes them.
The Settings page's Display section also holds the Language (English, Chinese, French, Spanish, Portuguese or German; `settings.language()`, saved only when not English). Every UI string is English in the code, wrapped in `i18n._()`, and looked up in `locales/<code>.json`; `tests/test_i18n.py` fails if any locale misses one. Page scripts get theirs as `window.I18N`: the shared few in `ui.JS_STRINGS`, and the board's and avatar studio's from every string literal inside their `tr(...)` calls (`ui.script_strings`, so no list to keep). The setup checks (`doctor.py`) are wrapped too; `run.py doctor` and Researcher Chat's `setup_status` read them in English. Labels stay short and plain: say what a thing is or does, no narration. The published site, prompts and agents stay English (`i18n.english()` draws the About page's strip), but Researcher Chat answers in the UI's language. The pixel researchers' lines are written per language, not translated (`assets/lines/<code>.js`, loaded before `assets/researchers.js`; anything missing falls back to English). It also holds the Appearance (System, Light or Dark; `settings.theme()`, saved only
when not System) and the chat colour (the app's red, seven pastels in `settings.CHAT_COLORS`, or any colour picked;
`settings.chat_color()`). Both go on the UI's `<html>` from the server (`ui.html_attrs`: `data-theme` and the
`--chat`/`--chat-ink` variables, the ink picked for contrast by `settings.ink_on`), so there is no theme switch in the
UI's top bar. The article preview the UI serves (`/m/<slug>/preview`, `ui.themed_preview`) is forced to the same
appearance and hides its own switch; the published site keeps its switch for readers.

| You want to | Command |
|---|---|
| Install and open it the first time | `./start.sh` (again any time to start the app) |
| Open the UI | `python run.py ui` → opens http://127.0.0.1:8770 in your browser (`--no-open` doesn't) |
| Write a sprite by hand and see it drawn | Settings → Avatars → Open the sprite studio (`/sprites`); the rules are in `SPRITES.md` |
| Use the desk's tools from another app on this network | Settings → MCP server: switch on, copy a setup (or `python run.py mcp`) |
| Check this machine's setup | `python run.py doctor`, or Settings → Setup |
| Set the published site's name, tagline and About statement | Settings → Publication |
| Save the Brave search key | the Get started card or Settings → Web search (or `BRAVE_API_KEY` in `.env`) |
| Add a mission | edit `missions.yaml` then `python run.py init`, or `python run.py add <slug> "<topic>" --objective "..."` |
| Put a mission in an epic | the Epic field on its page, or `python run.py epic <slug> "<title>"` (`""` clears) |
| See / rename epics | `python run.py epics`, `python run.py rename-epic "<old>" "<new>"`, or Epics on the UI home page |
| Approve a proposed epic | the "New epic proposed" panel on the UI home page |
| Approve or reject a proposed combine | its card under Needs you on the UI home page, or `python run.py combines`, then `approve-combine <slug>` / `reject-combine <slug>` |
| Link two missions, or remove a link | Connections on a mission's page, or `python run.py link <a> <b> --why "..."` / `unlink <a> <b>` |
| Set the mission finder's inspiration | the box on the UI home page, or `python run.py inspire "<line>"` (`""` turns it and the Theorist off) |
| Search what the desk has found (the library) | `python run.py library "<words>" [--epic "<title>"] [--mission <slug>]`; no words indexes it; or ask Researcher Chat |
| Pick the library's embedding model or top N | Settings → Library (provider, model, passages per search) |
| Have the Theorist look across the desk now | Run Theorist under the inspiration box, or `python run.py theorize` (its notes fold out under the box) |
| Run once until blocked | `python run.py run` |
| Run forever | `python run.py loop --every 15` (or install `ops/com.missionloop.loop.plist`, see the comments in it) |
| Stop | `python run.py stop` (the piece in flight finishes or is cut at the grace; everything finished is already saved) |
| Read a brief / article | `python run.py show <slug>`; the article preview is `missions/<slug>/preview.html` |
| Approve a brief | `python run.py approve <slug>` |
| Approve every brief and new epic automatically (never publishing) | the Auto switch at the top of the UI, or `python run.py auto on` (`off` to stop) |
| Publish an article | `python run.py review <slug>`; while the loop runs it is published within seconds, even mid-task |
| Send back with notes | `python run.py reject <slug> --notes "..."` (brief → research, article → writer) |
| Revive a parked mission | `python run.py requeue <slug>` (resumes at writing if the brief was already approved) |
| Override a parked mission | `python run.py push <slug>` (its last failed draft goes to your review, marked as failed) |
| View the site | Published in the UI's top bar, open `site/index.html`, or `python -m http.server 8765 -d site` |
| Publish an article again | tick where on the UI's Published page and press Publish (done within seconds; the loop needn't run) |
| Add a custom channel | Settings → Publishing → Add a channel → Custom channel (a webhook or a folder, and a format) |
| Subscribe | `site/feed.xml` (RSS) or `site/feed.json` (JSON Feed); set the hosted address on the Settings page once it is hosted |
| Turn on Bluesky posting | add your handle and an app password on the Settings page (or `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env`, then `python run.py check-bluesky`) |
| Put the site online (GitHub Pages) | Connect GitHub on the Settings page, then create or paste the site's repo |
| Turn on 1F916 posting | Join on the Settings page, or `python run.py join-1f916 <handle>` once (saves `data/1f916.json`); `status` and the UI show the queue |
| Ask what is going on, or look something up | Researcher Chat, the bubble at the bottom right of the UI, or `python run.py ask "where is the grid mission at?"` |
| See every mission and epic on one map | Board in the UI's top bar, or On the board on any mission card |
| Pick each worker's model, how many of it run at once, or switch one off | the Workers cards on the Settings page (saved in `data/settings.json`) |
| Point at another Ollama | the Ollama address under Model providers on the Settings page, or `OLLAMA_HOST` in `.env` |
| Start on a fresh clone with Ollama | Settings → Model providers → Connect Ollama (fills in each worker's model), then play |
| Use Claude, GPT, Grok, DeepSeek, Gemini, ... | Settings → Model providers → Add a provider → paste its API key, then pick its models under Workers (or `ANTHROPIC_API_KEY` etc. in `.env` and `MISSION_LOOP_MODEL=anthropic/<model>`) |
| Ask people about each approved brief (outreach) | Settings → Outreach: switch it on, set the reply wait and people per mission, tick channels, set up email (Bluesky and 1F916 come from Publishing) |
| Block a source site | add the domain to `blocked_domains.txt` |

Only one `run`/`loop` can work at a time (`data/loop.lock`). A UI action touches `data/WAKE`, so a
resting loop looks for work right away instead of waiting out `--every`. Before it starts working the loop checks that Ollama
is up, so a closed Ollama app doesn't burn every mission's attempts. The loop sends a macOS notification
when a mission needs you (brief ready, article ready, parked) or was published.
