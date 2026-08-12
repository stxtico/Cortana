"""attribution - stage 8 of the Ghost Typer reels pipeline (PROMPTS.md A20):
reads the conversion side of the funnel A19 could only tag, not measure.
Reuses services/marketing/publish.py's persistent Supabase client (CLAUDE.md
rule 7 - one client per process, not a second one here).

Verified against the REAL schema before writing anything, per explicit
instruction (matching A19's own "check what actually exists" precedent) -
full findings, from reading every migration in Ghosttyper-web/supabase/
migrations/ and grepping the app code, not assumed:

- content_posts.metadata (A19) carries angle/doc_type/format/hook/utm_link
  per video - real, exists today.
- credit_ledger (type='signup_grant') is the real "trial signup" event -
  granted by a DB trigger the instant auth.users.email_confirmed_at is set
  (supabase/migrations/20260718000200_credit_gating.sql). Keyed by
  user_id, timestamped by created_at. Real, exists today.
- stripe_events (event_type/user_id/processed_at) is the real "paid
  conversion" event source. Real, exists today.
- What does NOT exist, checked directly rather than assumed: zero UTM or
  referrer capture anywhere in the app - grepped the login page, the auth
  callback, and every Stripe checkout-session metadata block
  (app/api/stripe/checkout/route.ts); all of them only ever carry
  supabase_user_id/plan/pack, nothing UTM-shaped. auth.users and
  public.profiles carry no attribution column either. This means there is
  currently NO JOIN KEY connecting a content_posts row to any user's
  signup or conversion - not "the join returns zero rows," there is no
  column to join on at all yet. Same gap on the denominator:
  lib/instagram/client.ts only ever calls the publish-side Graph API
  (create container, status, publish, refresh token) - Instagram Insights
  (reach/impressions/plays) are never fetched or stored anywhere, so
  "views" has no real data source either.

Rather than invent that missing piece inside a live paying-customers'
signup/checkout flow unprompted, every function here is written against a
concrete, documented PROPOSED table (UTM_ATTRIBUTIONS_SCHEMA below) and
probes for its real existence before every query - the same
is_available()-style dormant pattern every other external dependency in
this project uses (CLAUDE.md rule 10). The moment that table exists in
Ghosttyper-web's Supabase project (created deliberately, by a human, in
that repo - not by this pipeline), every function here starts returning
real joined data with no code change.
"""

from services.marketing import publish

# The one piece missing from the real schema, documented precisely rather
# than invented silently. Capture: a middleware/edge function on the
# landing page reads utm_* query params on first visit and writes a signed
# cookie; at signup, the cookie's value is looked up and inserted here with
# event='signup' and the new user's id. This is real product work touching
# Ghosttyper-web's live signup flow, and is a deliberate call for whoever
# owns that repo - not something built here unprompted.
UTM_ATTRIBUTIONS_SCHEMA = """
create table public.utm_attributions (
  id           bigint generated always as identity primary key,
  utm_content  text not null,          -- = content_posts.metadata->>'video_id'
  utm_source   text,
  utm_medium   text,
  utm_campaign text,
  user_id      uuid references auth.users(id),
  event        text not null check (event in ('landing', 'signup')),
  created_at   timestamptz not null default now()
);
-- Then the real conversion join, using tables that already exist:
--   stripe_events.user_id -> utm_attributions.user_id -> utm_attributions.utm_content
"""


async def attribution_table_exists() -> bool:
    """Read-only existence probe (CLAUDE.md rule 10) - PostgREST returns 404
    for a table that isn't there, which is the expected, ordinary case
    right now, not an error."""
    if not publish.is_configured():
        return False
    client = publish._get_client()
    resp = await client.get("/rest/v1/utm_attributions", params={"limit": "1"})
    return resp.status_code == 200


async def fetch_content_rows() -> list[dict]:
    """All content_posts rows this pipeline has ever queued - real query
    against the real, already-existing table. Returns [] before any batch
    has published (publish_live still false, or the marketing-media bucket
    doesn't exist yet) - an empty list is the correct, expected result
    right now, not a bug."""
    if not publish.is_configured():
        return []
    client = publish._get_client()
    resp = await client.get(
        "/rest/v1/content_posts",
        params={"select": "id,metadata,status,ig_media_id,created_at", "order": "created_at.desc"},
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_conversions_for(video_id: str) -> dict:
    """Signups and paid conversions attributable to one video's UTM tag.
    Returns zeroed, explicitly measurable=False counts if utm_attributions
    doesn't exist yet - never fabricates a number to fill the gap."""
    if not await attribution_table_exists():
        return {"signups": 0, "conversions": 0, "measurable": False}

    client = publish._get_client()
    attr_resp = await client.get(
        "/rest/v1/utm_attributions",
        params={"utm_content": f"eq.{video_id}", "event": "eq.signup", "select": "user_id"},
    )
    attr_resp.raise_for_status()
    user_ids = [row["user_id"] for row in attr_resp.json() if row.get("user_id")]
    if not user_ids:
        return {"signups": 0, "conversions": 0, "measurable": True}

    ids_filter = "(" + ",".join(user_ids) + ")"
    conv_resp = await client.get(
        "/rest/v1/stripe_events",
        params={
            "user_id": f"in.{ids_filter}",
            "event_type": "in.(checkout.session.completed,customer.subscription.created)",
            "processed_at": "not.is.null",
            "select": "user_id",
        },
    )
    conv_resp.raise_for_status()
    conversions = len({row["user_id"] for row in conv_resp.json()})
    return {"signups": len(user_ids), "conversions": conversions, "measurable": True}
