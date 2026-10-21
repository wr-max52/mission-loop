"""Research agent: a bounded tool loop on Ollama that ends in a ResearchBrief."""
import asyncio
from pydantic import ValidationError
from contracts import Mission, ResearchBrief
from agents import llm
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40

SYSTEM = """You are a research analyst.
Topic: {topic}
Mission: {objective}

Find one specific, defensible thesis and prove it with primary sources.
Rules:
- Use the tools to search, then fetch and read full pages. Only cite pages you actually fetched.
- For every source, copy one exact sentence from that page as its quote. Never paraphrase a quote.
- Find at least 3 supporting sources on 3 different websites, and at least 1 credible source that disagrees.
- Find at least 3 points of contact (experts, authors, spokespeople) whose email address appears
  on a public page you fetched. Record that page as contact_source_url.
- When you have enough evidence, stop calling tools and reply with the word DONE.
{notes}"""

FINAL = ("Now output the research brief as JSON matching the schema. "
         "Use only sources you fetched, with quotes copied exactly from those pages.")

class ResearchFailed(Exception):
    pass

def _notes(m: Mission) -> str:
    if not m.validator_notes:
        return ""
    return f"\nYour previous brief was rejected. Fix every problem below:\n{m.validator_notes}\n"

async def run_research(m: Mission, toolbox_factory=Toolbox) -> ResearchBrief:
    messages = [
        {"role": "system", "content": SYSTEM.format(topic=m.topic, objective=m.objective or m.topic,
                                                    notes=_notes(m))},
        {"role": "user", "content": "Begin."},
    ]
    async with toolbox_factory(m.tools) as tb:
        calls = 0
        while calls < MAX_TOOL_CALLS:
            msg = await llm.chat(messages, tools=tb.tools)
            messages.append(msg)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                if calls >= MAX_TOOL_CALLS:
                    break
                calls += 1
                result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
                messages.append({"role": "tool", "tool_name": tc.function.name, "content": result})

    messages.append({"role": "user", "content": FINAL})
    schema = ResearchBrief.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=schema)
        try:
            return ResearchBrief.model_validate_json(msg.content)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise ResearchFailed(f"model could not produce a valid brief: {error}")

def research(m: Mission) -> ResearchBrief:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_research(m))
