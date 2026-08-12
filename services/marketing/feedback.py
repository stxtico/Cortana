"""feedback - stage 10 of the Ghost Typer reels pipeline (PROMPTS.md A20):
feeds top-performing hooks back into script.py's generation prompt as
examples, closing the loop PLAN.md describes ("generate -> measure what
landed -> train on the winners"). Inert (returns nothing) until
report.build_report() has at least one genuinely measurable ranking - never
fabricates a "top performer" from unmeasurable data, same discipline as
report.py itself.
"""

from services.marketing import report as report_module


async def top_performing_hooks(n: int = 3) -> list[str]:
    rankings = await report_module.build_report("hook")
    measurable = [r for r in rankings if r.measurable and r.conversions_per_1000_views]
    return [r.key for r in measurable[:n]]
