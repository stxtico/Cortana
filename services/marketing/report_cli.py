"""report_cli - CLI entry point for the A20 attribution report.

    uv run python -m services.marketing.report_cli
    uv run python -m services.marketing.report_cli --dimension angle

Prints rankings for all four dimensions by default (hook/format/angle/
doc_type). Every number is real or explicitly marked "unmeasurable" - see
attribution.py's docstring for exactly what's missing in Ghosttyper-web's
schema before this can report real conversions.
"""

import asyncio
import sys

from services.marketing import attribution, publish, report

_DIMENSIONS = ["hook", "format", "angle", "doc_type"]


async def main() -> None:
    args = sys.argv[1:]
    dimensions = _DIMENSIONS
    if "--dimension" in args:
        dimensions = [args[args.index("--dimension") + 1]]

    if not publish.is_configured():
        print("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set in cortana's own .env - "
              "nothing to report yet. This is expected until publishing is configured.\n")
        return

    if not await attribution.attribution_table_exists():
        print("utm_attributions doesn't exist in Ghosttyper-web's Supabase project yet - "
              "conversions/views will show as 'unmeasurable' below, not zero-because-no-data.")
        print("See services/marketing/attribution.py's UTM_ATTRIBUTIONS_SCHEMA for what's needed")
        print("to close the loop: UTM capture at landing, persisted at signup, joined to the")
        print("existing stripe_events/credit_ledger tables by user_id.\n")

    try:
        for dimension in dimensions:
            rankings = await report.build_report(dimension)
            print(report.format_report(dimension, rankings))
            print()
    finally:
        await publish.aclose()


if __name__ == "__main__":
    asyncio.run(main())
