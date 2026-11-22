"""The desk's library: retrieval (RAG) over everything the desk has written, for the Theorist and Researcher Chat.

Code, no chat model. Its passages are the desk's own work: each mission's topic and objective, its brief's thesis,
claims (each with the quotes it cites), counterpoint and uncited sources, and its article's title, dek and sections.
Nothing from outside the desk goes in, so a passage is background to reason from, never a source to cite.

Lightweight on purpose: one SQLite file (data/library.db, on this machine only) and plain Python, no vector database
and no numpy. It ranks two ways and fuses them (reciprocal rank fusion):
- by words, always: BM25 over every passage, so it works with no embedding model at all;
- by meaning, when an embedding model is set (settings.embed_model: an Ollama model, or "<provider>/<model>" for a
  connected cloud provider that offers embeddings). A passage is embedded once, when it first appears, and kept
  until its text changes; switching models re-embeds. A failed embedding call costs nothing: that search ranks by
  words and says why.
How many passages a search returns is the human's pick on the Settings page (settings.library_top_n).

The index follows the desk by itself: every search first syncs it (new passages added, changed or deleted ones
dropped), so there is nothing to rebuild by hand. Embedding a large desk the first time is spread over searches,
EMBED_TIME at a time; passages not embedded yet still rank by words.
Agents reach it only through its MCP server (library_tools.py, "library" in mcp.json); the UI and CLI call it
directly, like any other code."""
import hashlib, math, os, re, sqlite3, time
from array import array
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import httpx
import settings, store
from contracts import Mission, State, ResearchBrief, Article

DB = Path("data/library.db")
TRANSPORT: httpx.BaseTransport | None = None   # tests swap in a MockTransport
TIMEOUT = httpx.Timeout(120, connect=10)
QUERY_TIME = 30                                 # seconds to embed a search's query
CHUNK_CHARS = 1200                         # a long article section is split into passages about this long
PASSAGE_CHARS = 700                        # a passage as a search shows it
BATCH = 32                                 # passages per embedding request
EMBED_TIME = 60                            # seconds one sync may spend embedding; the rest waits for the next
RRF_K = 60                                 # reciprocal rank fusion's damping: the usual value
K1, B = 1.5, 0.75                          # BM25's usual parameters
KINDS = {"mission": "topic and objective", "thesis": "brief thesis", "claim": "brief claim",
         "counterpoint": "brief counterpoint", "source": "brief source", "article": "article title and dek",
         "section": "article section"}
STAGES = {State.QUEUED: "queued", State.RESEARCHING: "researching", State.BRIEF_READY: "brief in review",
          State.AWAITING_COMMENTS: "writing next", State.WRITING: "writing", State.ARTICLE_READY: "article in review",
          State.DONE: "published", State.PARKED: "parked", State.COMBINED: "combined"}
STOP = set("""a an and are as at be been but by can could did do does for from had has have he her his how i if in
into is it its may might more most no not of on or our she should so such than that the their them then there these
they this those to was we were what when where which while who whom why will with would you your about after also
any been before both each other over said says some very""".split())

class LibraryError(Exception):
    """An embedding call failed. The message is safe to show: it never holds a key."""

@dataclass
class Passage:
    id: str                                # hash of what is indexed, so changed text is a new passage
    slug: str
    kind: str
    label: str
    text: str
    url: str = ""
    topic: str = ""

    def indexed(self) -> str:
        """What is ranked and embedded: the passage under its mission's topic, so a passage that never names
        its subject still answers questions about it."""
        return f"{self.topic}\n{self.text}"

@dataclass
class Hit:
    passage: Passage
    score: float
    by: str                                # "meaning and words", "meaning" or "words"

# ---------- what goes in ----------

def _passage(m: Mission, kind: str, label: str, text: str, url: str = "") -> Passage:
    text = " ".join(text.split())
    key = f"{m.slug}|{kind}|{label}|{m.topic}|{text}"
    return Passage(id=hashlib.sha1(key.encode()).hexdigest()[:20], slug=m.slug, kind=kind, label=label, text=text,
                   url=url, topic=m.topic)

def _windows(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Paragraph-sized pieces of at most about size characters (a paragraph longer than that stays whole)."""
    out, cur = [], ""
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    return out + ([cur] if cur else [])

def passages_of(m: Mission) -> list[Passage]:
    """Everything one mission has written, as passages."""
    out = [_passage(m, "mission", "topic and objective", f"{m.topic} {m.objective}")]
    raw = store.load_artifact(m.slug, "brief")
    if raw:
        try:
            brief = ResearchBrief(**raw)
        except Exception:
            brief = None
        if brief:
            by_id = {s.id: s for s in brief.sources}
            out.append(_passage(m, "thesis", "brief thesis", brief.thesis))
            cited = set()
            for i, c in enumerate(brief.claims, 1):
                cites = [by_id[s] for s in c.source_ids if s in by_id]
                cited |= {s.id for s in cites}
                quotes = " ".join(f'[{s.id}] "{s.quote}" ({s.title}, {s.url})' for s in cites)
                out.append(_passage(m, "claim", f"brief claim {i} ({c.kind})", f"{c.text} {quotes}",
                                    cites[0].url if cites else ""))
            out.append(_passage(m, "counterpoint", "brief counterpoint", brief.counterpoint))
            for s in brief.sources:
                if s.id not in cited:
                    out.append(_passage(m, "source", f"brief source {s.id} ({s.stance})",
                                        f'"{s.quote}" ({s.title}, {s.url})', s.url))
    raw = store.load_artifact(m.slug, "article")
    if raw:
        try:
            article = Article(**raw)
        except Exception:
            article = None
        if article:
            out.append(_passage(m, "article", "article title and dek", f"{article.title}. {article.dek}"))
            for sec in article.sections:
                for n, piece in enumerate(_windows("\n".join(sec.paragraphs)), 1):
                    label = f"article section: {sec.heading}" + (f" (part {n})" if n > 1 else "")
                    out.append(_passage(m, "section", label, f"{sec.heading}. {piece}"))
    return [p for p in out if p.text]

def documents() -> list[Passage]:
    """Every passage on the desk now, newest missions last. Each id once (a repeated claim counts once)."""
    seen, out = set(), []
    for m in store.all_missions():
        for p in passages_of(m):
            if p.id not in seen:
                seen.add(p.id)
                out.append(p)
    return out

# ---------- the index ----------

def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)    # the chat's and the Theorist's servers may sync at the same moment
    c.execute("CREATE TABLE IF NOT EXISTS passages (id TEXT PRIMARY KEY, slug TEXT, kind TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS vectors (id TEXT, model TEXT, vec BLOB, PRIMARY KEY (id, model))")
    return c

def embed_model() -> str:
    """The embedding model in use; "" ranks by words only. Stub runs never reach the network."""
    return "" if os.environ.get("MISSION_LOOP_STUBS") == "1" else settings.embed_model()

def sync(ref: str | None = None, time_limit: float = EMBED_TIME) -> tuple[list[Passage], dict[str, list[float]], str]:
    """Bring the index up to the desk: add new passages, drop gone ones (and every vector of another model), then
    embed what has no vector yet for at most time_limit seconds. Returns (passages, vectors by id, note); the note
    says why some or all passages rank by words only ("" when every one has a vector, or no model is set)."""
    ref = embed_model() if ref is None else ref
    found = documents()
    ids = {p.id for p in found}
    with _db() as c:
        have = {r[0] for r in c.execute("SELECT id FROM passages")}
        gone = list(have - ids)
        c.executemany("DELETE FROM passages WHERE id = ?", [(i,) for i in gone])
        c.executemany("DELETE FROM vectors WHERE id = ?", [(i,) for i in gone])
        c.executemany("INSERT OR IGNORE INTO passages VALUES (?, ?, ?)",   # another server may add it first
                      [(p.id, p.slug, p.kind) for p in found if p.id not in have])
        if ref:                                      # with no model set, keep the vectors for when one is back
            c.execute("DELETE FROM vectors WHERE model != ?", (ref,))
        c.execute("DELETE FROM vectors WHERE id NOT IN (SELECT id FROM passages)")   # left by a racing sync
        vectors = {r[0]: _unpack(r[1]) for r in c.execute("SELECT id, vec FROM vectors WHERE model = ?", (ref,))}
    if not ref:
        return found, {}, ""
    todo = [p for p in found if p.id not in vectors]
    started, note = time.monotonic(), ""
    for i in range(0, len(todo), BATCH):
        if time.monotonic() - started > time_limit:
            note = f"{len(found) - len(vectors)} of {len(found)} passages are not embedded yet; they rank by words"
            break
        batch = todo[i:i + BATCH]
        try:
            got = embed([p.indexed() for p in batch], ref, time_limit - (time.monotonic() - started))
        except LibraryError as e:
            note = f"ranked by words only: {e}"
            break
        with _db() as c:
            c.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                          [(p.id, ref, _pack(v)) for p, v in zip(batch, got)])
        vectors |= {p.id: _norm(v) for p, v in zip(batch, got)}
    return found, vectors, note

def status() -> dict:
    """How big the index is, without syncing it: {"passages", "missions", "embedded", "model"}."""
    ref = embed_model()
    if not DB.exists():
        return {"passages": 0, "missions": 0, "embedded": 0, "model": ref}
    with _db() as c:
        n, missions = c.execute("SELECT COUNT(*), COUNT(DISTINCT slug) FROM passages").fetchone()
        done = c.execute("SELECT COUNT(*) FROM vectors v JOIN passages p ON p.id = v.id WHERE v.model = ?",
                         (ref,)).fetchone()[0] if ref else 0
    return {"passages": n, "missions": missions, "embedded": done, "model": ref}

def _norm(v: list[float]) -> list[float]:
    size = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / size for x in v]

def _pack(v: list[float]) -> bytes:
    return array("f", _norm(v)).tobytes()

def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()

# ---------- embeddings: Ollama, or a cloud provider's OpenAI-shaped /embeddings ----------

def _client(seconds: float | None = None) -> httpx.Client:
    timeout = TIMEOUT if seconds is None else httpx.Timeout(max(1.0, min(seconds, TIMEOUT.read)), connect=10)
    return httpx.Client(timeout=timeout, transport=TRANSPORT) if TRANSPORT else httpx.Client(timeout=timeout)

def embed(texts: list[str], ref: str, seconds: float | None = None) -> list[list[float]]:
    """One vector per text from the embedding model ref, waiting at most seconds (default TIMEOUT). Raises
    LibraryError for a provider that can't embed, isn't connected, refuses or answers with something else."""
    from agents import providers           # late: agents import settings, which is fine, but keep this module light
    pid, name = settings.split_model(ref)
    if not name:
        raise LibraryError("no embedding model is set")
    if not settings.can_embed(pid):
        raise LibraryError(f"{settings.PROVIDERS[pid]['name']} offers no embedding models")
    if pid == settings.LOCAL:
        url, body, headers = settings.ollama_host() + "/api/embed", {"model": name, "input": texts,
                                                                    "keep_alive": "5m"}, {}
    else:
        if not settings.provider_on(pid):
            raise LibraryError(f"{settings.PROVIDERS[pid]['name']} isn't connected; add its key on the Settings page")
        url = settings.provider_url(pid) + "/embeddings"
        body, headers = {"model": name, "input": texts}, providers._headers(pid, settings.provider_key(pid))
    label = settings.PROVIDERS[pid]["name"]
    try:
        with _client(seconds) as c:
            r = c.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise LibraryError(f"{label} is not reachable ({type(e).__name__})")
    if r.status_code >= 400:
        raise LibraryError(f"{label} refused the embedding model {name} ({providers._why(r)})")
    try:
        data = r.json()
        if pid == settings.LOCAL:
            vectors = data["embeddings"]
        else:
            vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
        vectors = [[float(x) for x in v] for v in vectors]
    except (ValueError, KeyError, TypeError):
        raise LibraryError(f"{label} answered with something that isn't embeddings")
    if len(vectors) != len(texts) or not all(vectors):
        raise LibraryError(f"{label} sent {len(vectors)} embeddings for {len(texts)} passages")
    return vectors

def check(ref: str) -> int:
    """Embed one line with ref, so the Settings page saves only a model that works. Returns its dimensions."""
    return len(embed(["The desk's library checks its embedding model."], ref)[0])

# ---------- ranking ----------

def tokens(text: str) -> list[str]:
    """Lowercase words without the commonest ones, plural s dropped: enough stemming for BM25 to match taxes and tax-cuts."""
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out

def bm25(query: str, found: list[Passage]) -> dict[str, float]:
    """id -> BM25 score for passages sharing a word with the query."""
    wanted = set(tokens(query))
    if not wanted or not found:
        return {}
    docs = [Counter(tokens(p.indexed())) for p in found]
    lengths = [sum(d.values()) for d in docs]
    avg = sum(lengths) / len(docs) or 1
    n = len(docs)
    idf = {}
    for w in wanted:
        df = sum(w in d for d in docs)
        idf[w] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    scores = {}
    for p, d, size in zip(found, docs, lengths):
        score = sum(idf[w] * d[w] * (K1 + 1) / (d[w] + K1 * (1 - B + B * size / avg)) for w in wanted if w in d)
        if score:
            scores[p.id] = score
    return scores

def _ranks(scores: dict[str, float]) -> dict[str, int]:
    return {i: r for r, i in enumerate(sorted(scores, key=lambda i: -scores[i]), 1)}

def search(query: str, top_n: int | None = None, epic: str = "", mission: str = "") -> tuple[list[Hit], str]:
    """The top_n passages (settings.library_top_n unless given) most like the query, by meaning and words fused,
    optionally only in one epic (title or slug) or one mission (slug). No mission fills more than half the list
    while others have a match. Returns (hits, note): the note says what limited the ranking, if anything."""
    top_n = top_n or settings.library_top_n()
    found, vectors, note = sync()
    missions = {m.slug: m for m in store.all_missions()}
    if epic.strip():
        e = store.find_epic(epic)
        if not e:
            return [], f"no epic called {epic.strip()}"
        found = [p for p in found if missions.get(p.slug) and missions[p.slug].epic == e.slug]
    if mission.strip():
        found = [p for p in found if p.slug == mission.strip()]
    if not found:
        return [], note
    words_rank = _ranks(bm25(query, found))
    meaning_rank: dict[str, int] = {}
    ref = embed_model()
    if ref and any(p.id in vectors for p in found) and not note.startswith("ranked by words only"):
        try:
            q = _norm(embed([query], ref, QUERY_TIME)[0])
            sims = {p.id: sum(a * b for a, b in zip(q, vectors[p.id])) for p in found if p.id in vectors}
            meaning_rank = _ranks(sims)
        except LibraryError as e:
            note = f"ranked by words only: {e}"
    fused: dict[str, tuple[float, str]] = {}
    for p in found:
        w, m = words_rank.get(p.id), meaning_rank.get(p.id)
        if w is None and m is None:
            continue
        score = (1 / (RRF_K + w) if w else 0) + (1 / (RRF_K + m) if m else 0)
        fused[p.id] = (score, "meaning and words" if w and m else "meaning" if m else "words")
    by_id = {p.id: p for p in found}
    ordered = sorted(fused, key=lambda i: -fused[i][0])
    cap = max(2, math.ceil(top_n / 2)) if not mission.strip() else top_n
    picked, per = [], {}
    for i in ordered:                      # first pass: no mission takes more than its share
        s = by_id[i].slug
        if per.get(s, 0) < cap:
            picked.append(i)
            per[s] = per.get(s, 0) + 1
        if len(picked) == top_n:
            break
    for i in ordered:                      # then fill up with the rest, in rank order
        if len(picked) == top_n:
            break
        if i not in picked:
            picked.append(i)
    picked.sort(key=lambda i: -fused[i][0])
    return [Hit(by_id[i], round(fused[i][0] * 1000, 1), fused[i][1]) for i in picked], note

def render(query: str, hits: list[Hit], note: str = "", top_n: int | None = None) -> str:
    """A search as an agent reads it: one numbered passage per hit with its mission, stage, epic and where in
    the mission's work it comes from."""
    st = status()
    how = f"by meaning ({st['model']}) and words" if st["model"] else "by words (no embedding model is set)"
    head = (f"Library: {st['passages']} passages from {st['missions']} missions, ranked {how}. "
            f"Top {top_n or settings.library_top_n()} for: {query.strip()}")
    if note:
        head += f"\nNote: {note}"
    if not hits:
        return head + "\nNo passage matches. Try other words, or no epic or mission filter."
    missions, titles = {m.slug: m for m in store.all_missions()}, store.epic_titles()
    lines = [head]
    for n, h in enumerate(hits, 1):
        p, m = h.passage, missions.get(h.passage.slug)
        stage = STAGES[m.state] if m else "?"
        epic = f" · epic {titles[m.epic]}" if m and m.epic in titles else ""
        text = p.text if len(p.text) <= PASSAGE_CHARS else p.text[:PASSAGE_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(f"{n}. [{p.slug}] {p.topic[:100]} · {stage}{epic} · {p.label}\n   {text}")
    return "\n".join(lines)

def ask(query: str, top_n: int | None = None, epic: str = "", mission: str = "") -> str:
    """search, rendered: what the MCP tool and the CLI return."""
    query = " ".join(query.split())[:500]
    if not tokens(query) and not embed_model():
        return "Give a few words to look for."
    hits, note = search(query, top_n, epic, mission)
    return render(query, hits, note, top_n)

def summary() -> str:
    """One line for the Settings page and the CLI."""
    st = status()
    how = f"embedded with {st['model']}: {st['embedded']} of {st['passages']}" if st["model"] else "ranked by words only"
    return f"{st['passages']} passages from {st['missions']} missions; {how}"
