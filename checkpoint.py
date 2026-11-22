"""Work in progress, saved the moment a piece of it is done.

The database otherwise changes only when a task ends. A long task (a 15-minute search, a brief in two steps,
an article in several sections) saves each finished piece straight away through save(), so neither the stop
button nor a killed loop throws it away: the next start picks up after the last saved piece.

The orchestrator owns the database and hands each worker's task a saver (use()). Outside a task save() does
nothing, so agents behave the same in tests and on the CLI. A failed save is reported, never raised: losing a
checkpoint must not cost the work it was protecting.
"""
import threading

_here = threading.local()

def use(saver) -> None:
    """Called at the start of a worker's task, on its own thread: saver(mission, artifacts) stores progress.
    None when the task ends."""
    _here.saver = saver

def save(m, **artifacts) -> None:
    """Save the mission's progress fields now, and any artifacts given (kind=object)."""
    saver = getattr(_here, "saver", None)
    if saver is None:
        return
    try:
        saver(m, artifacts)
    except Exception as e:
        print(f"{m.slug}: progress not saved: {type(e).__name__}: {e}", flush=True)
