"""relevance - the part of A11 that decides whether this works at all
(PROMPTS.md). Same calibration lesson as A10's ask_user, one level up: this
model responds to concrete, enumerated criteria with worked examples, not a
vague "use your judgment whether this is worth interrupting for" framing -
A8/A10 both found the same thing from different angles (tool descriptions
needing the real whitelist spelled out rather than a generic "only allowed
directories"; ask_user needing genuinely ambiguous, concretely-specified
scenarios to fire reliably). The prompt below is written as enumerated
yes/no categories with a worked example each, not a description of intent -
see CLAUDE.md's A11 entry for the verification that this framing (vs. a
vague "use good judgment" version tried first) is what actually made the
filter discriminate correctly.
"""

from services.brain import client as brain_client

_PROMPT = """You decide whether a background event is worth interrupting the \
user's current activity for, right now. Answer with exactly one word: yes or \
no. Nothing else - no explanation, no punctuation.

Interrupt (yes) for things like:
- A calendar event starting within the next 20 minutes that the user hasn't \
already been told about.
  Example: "Meeting with the Henderson account starts in 18 minutes" -> yes
- An email that matches a rule the user explicitly configured as important.
  Example: "Email from a client asking about invoice #4021, matches your \
'client emails' rule" -> yes
- A timer the user explicitly set, now going off.
  Example: "Timer 'pasta' just went off" -> yes

Do NOT interrupt (no) for things like:
- Routine or low-urgency information that could wait until asked.
  Example: "Newsletter subject: 10 tips for productivity" -> no
- A calendar event outside the interruption window.
  Example: "Calendar event 'Lunch' happening in 3 hours" -> no
- A vague or trivial reminder with no real time pressure or consequence.
  Example: "Reminder: today is Tuesday" -> no

Event to evaluate:
{summary}
{detail}

Answer (yes or no):"""


async def is_relevant(candidate: dict, think: bool = False) -> bool:
    prompt = _PROMPT.format(summary=candidate.get("summary", ""), detail=candidate.get("detail", ""))
    chunks = []
    async for token in brain_client.stream([{"role": "user", "content": prompt}], think=think):
        chunks.append(token)
    answer = "".join(chunks).strip().lower()
    return answer.startswith("yes")
