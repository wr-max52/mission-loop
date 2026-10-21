import typer, yaml
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()                              # reads API keys from .env before anything else
import clock, orchestrator, store
from contracts import Mission

app = typer.Typer()

def _set(slug: str, **fields):
    m = store.get_mission(slug)
    if not m:
        raise typer.BadParameter(f"no mission {slug}")
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

@app.command()
def init(path: str = "missions.yaml"):
    store.init()
    for item in yaml.safe_load(Path(path).read_text()) or []:
        if not store.get_mission(item["slug"]):
            store.save_mission(Mission(**item))
            print(f"added {item['slug']}")

@app.command()
def add(slug: str, topic: str, objective: str = ""):
    store.init()
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective))

@app.command()
def run():
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    n = orchestrator.run_until_idle(clock.now)
    print(f"{n} task(s) ran")
    status()

@app.command()
def stop():
    orchestrator.STOP_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.STOP_FLAG.touch()
    print("stop requested; the current task will finish first")

@app.command()
def approve(slug: str):
    _set(slug, approved=True)

@app.command()
def review(slug: str):
    _set(slug, reviewed=True)

@app.command()
def reject(slug: str, notes: str = typer.Option(..., help="What the research agent must fix")):
    orchestrator.reject(store.get_mission(slug), notes, clock.now())
    print(f"{slug} sent back to research")

@app.command()
def requeue(slug: str):
    orchestrator.requeue(store.get_mission(slug), clock.now())
    print(f"{slug} requeued")

@app.command()
def show(slug: str):
    m = store.get_mission(slug)
    brief = Path("missions") / slug / "brief.md"
    print(brief.read_text() if brief.exists() else f"no brief yet ({m.state.value})")
    if m.validator_notes:
        print("\nLast validator / reviewer notes:\n" + m.validator_notes)

@app.command()
def status():
    for m in store.all_missions():
        extra = f" (wait until {m.wait_until:%m-%d %H:%M})" if m.wait_until and m.state.value == "AWAITING_COMMENTS" else ""
        print(f"{m.slug:20} {m.state.value:18} attempts={m.attempts}{extra}")

if __name__ == "__main__":
    app()
