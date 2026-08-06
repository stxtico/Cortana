"""ask_user (PROMPTS.md A10) - the real callable tool for clarifying questions.
The question side is genuinely "returned through TTS": speaks it aloud via the
real engine (services/voice/tts.py), the same one every other response uses,
not printed-and-called-done. The answer side is honestly not there yet -
agent.py runs standalone, with no wiring to services/ears/pipeline.py's
mic/STT path, so there is no way for a spoken answer to reach this tool.
Answer capture goes through services/brain/user_input.py's shared
get_answer() - CLI today, swappable for voice at the loop-integration step -
but this is deliberately NOT the same thing as A9's confirmation gate
(services/brain/agent_safety.py), even though both are keyboard-only for the
same underlying reason. confirm() stops an action already decided, enforced
so the model can't route around it; ask_user is the model choosing to ask
before deciding anything, a normal tool call with no gate semantics. They
share the input plumbing, not the meaning of an answer - see user_input.py's
docstring.

Not REQUIRES_CONFIRMATION - asking a question isn't itself an action that
needs confirming, it's how other actions get clarified before they happen.
The one-question-per-turn cap from persona.md's policy is enforced in
services/brain/agent.py's dispatcher (run_agent()), not trusted to the
prompt - see CLAUDE.md rule 4 and A9's whole premise: negative persona
constraints measured at roughly two-thirds reliability, nowhere near
something a hard cap should depend on.
"""

from services.brain import user_input
from services.voice import tts as voice_tts

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a clarifying question when something necessary is genuinely "
                "missing or ambiguous - a filename, a dimension, which of several options - "
                "rather than guessing. Spoken aloud; the answer comes back as plain text. "
                "Limited to one per turn - don't call this more than once in the same turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask, as one clause - no preamble."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional short list of choices, if the question is a pick-one.",
                    },
                },
                "required": ["question"],
            },
        },
    }


def describe(question: str, options: list[str] | None = None) -> str:
    return question if not options else f"{question} ({' / '.join(options)})"


async def _speak(text: str) -> None:
    async def _one_token():
        yield text

    await voice_tts.speak_stream(_one_token())


async def execute(question: str, options: list[str] | None = None) -> str:
    prompt_text = describe(question, options)
    await _speak(prompt_text)
    print(f"\n[ASK_USER] {prompt_text}")
    answer = await user_input.get_answer("Your answer: ")
    return answer.strip()
