# A5a — Persona (character brief, dry wit / padding investigation)

Moved verbatim from CLAUDE.md's Done log (2026-08-12 restructure) — see
CLAUDE.md's Done log for the one-line pointer back to this file.

- **Persona: character brief written (`config/persona.md`), two known limitations
  found and closed out rather than left open-ended.** Response shape (1-2 sentences,
  short first sentence, no editorializing closers), correction precedence (a
  correction to her own claim is taken flat, never disputed), and disagreement
  grounds (suspicion + question, never an invented specific number) are all written
  and verified live against real `gemma4:e4b` calls.
  **Known limitation: dry wit doesn't reliably fire.** Tested two different ways -
  a repeated-question scenario (meta-awareness of being asked N times) across five
  conditions (full persona, a stripped third-size persona, positive examples in the
  shape rules, a literal third-ask matching the sample line's own framing, and
  `gemma4:12b` instead of `e4b`), 25 runs, zero firings in any condition - and a
  content-only scenario (the dry aside is about the answer's content, not the
  exchange - four factual questions x5 runs, full persona, text-only), zero firings
  there too. Ruling out both prompt structure and model size, and ruling out that
  the first test's meta-awareness framing was just a harder bar than dryness itself
  requires, means this isn't a test artifact - the trait is genuinely close to absent
  under this persona/model combination. Not chasing it further. One incidental
  counter-data-point from the padding retest below: 2 of 20 responses there landed a
  genuine dry aside unprompted ("enough time to grab a coffee, but not enough to
  decide what you'll work on next") - so the trait *can* fire, just rarely, and not
  reliably from any rule change tried so far.
  **Known limitation: unsolicited padding (volunteered advice/warnings/next-steps
  beyond what was asked) holds at roughly 2/3 of responses regardless of how the
  rule is worded.** Same 20-prompt content-only test, same responses used for the
  dry-wit check above: a negative-prohibition version of the rule ("she answers only
  what was asked") measured 10/15 responses (excluding one prompt set that mostly
  produced clarifying questions instead of answers, not padding) volunteering
  unrequested checks/warnings/next-steps; rewriting it as a positive definition
  ("a complete answer contains the information asked for and nothing else, and
  never includes what to check/watch for/do next/might go wrong unless asked")
  measured 9/15 on an identical retest - flat, within noise, not a fix. Two
  different rule phrasings converging on the same rate is real evidence the lever
  isn't persona wording - this model pads by default on these prompts and the
  persona can't reliably stop it. Not re-fixing via a third rewording; if this
  needs solving it's a different kind of fix (e.g. a post-generation trim pass)
  worth scoping deliberately, not another prompt tweak.

