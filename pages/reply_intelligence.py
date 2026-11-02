"""Reply Intelligence: what every reply says, and what's working, across all campaigns.

Flow: pull every reply from EmailBison (cached 15 min) -> classify new replies with Claude
(results cached on disk by reply ID) -> join categories to lead and campaign attributes ->
KPIs, category chart, AI takeaways, "what's working" tables and an action queue.

Secrets:
    EMAILBISON_API_KEY, EMAILBISON_BASE_URL  - same as the main dashboard
    ANTHROPIC_API_KEY                        - Claude API key for classification
    ANTHROPIC_MODEL                          - optional; defaults to the latest Sonnet
"""

import html
import logging

import altair as alt
import pandas as pd
import requests
import streamlit as st

from outbound import analysis, theme
from outbound.classifier import DEFAULT_MODEL, ClassifierAuthError, ReplyClassifier
from outbound.emailbison import DEFAULT_BASE_URL, load_reply_dataset, log_api_error

CACHE_TTL_SECONDS = 15 * 60
MIN_REPLIES_FOR_INSIGHTS = 10  # below this, skip the AI summary and what's-working tables

log = logging.getLogger("outbound_dashboard")

theme.setup_page("Reply Intelligence")
theme.hero("AI-classified replies", "Reply Intelligence", "Slipstream Engineering")

UNAVAILABLE = "We couldn't load data from EmailBison right now. Please try again in a few minutes."


# --- Config ------------------------------------------------------------------

def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except FileNotFoundError:
        return default


eb_key = secret("EMAILBISON_API_KEY")
if not eb_key:
    log.error("EMAILBISON_API_KEY is missing from Streamlit secrets.")
    st.info("This dashboard isn't connected to EmailBison yet.")
    st.stop()
eb_base = (secret("EMAILBISON_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
anthropic_key = secret("ANTHROPIC_API_KEY")
model = secret("ANTHROPIC_MODEL") or DEFAULT_MODEL


# --- Cached loaders ----------------------------------------------------------

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_data(api_key: str, base_url: str) -> dict[str, pd.DataFrame]:
    """Every reply with lead, campaign and step attributes (see outbound.emailbison)."""
    return load_reply_dataset(api_key, base_url)


@st.cache_resource
def get_classifier(api_key: str, model_id: str) -> ReplyClassifier:
    return ReplyClassifier(api_key, model_id)


class _NoSummary(Exception):
    """Raised so a failed summary isn't cached."""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def ai_summary(api_key: str, model_id: str, table_csv: str, context: str) -> list[str]:
    takeaways = get_classifier(api_key, model_id).summarize(table_csv, context)
    if not takeaways:
        raise _NoSummary
    return takeaways


# --- Data pull ---------------------------------------------------------------

_, col_refresh = st.columns([5, 1], vertical_alignment="bottom")
with col_refresh:
    if st.button("Refresh data", type="primary", width="stretch"):
        load_data.clear()
        ai_summary.clear()
        st.rerun()

try:
    with st.spinner("Loading every reply from EmailBison…"):
        data = load_data(eb_key, eb_base)
except requests.RequestException as err:
    log_api_error(err, "reply data pull")
    st.info(UNAVAILABLE)
    st.stop()

replies, campaigns, steps = data["replies"], data["campaigns"], data["steps"]

if replies.empty:
    theme.card(
        '<div class="card-title">No replies yet</div>'
        "<p>Once leads start replying to your campaigns, this page will sort every reply "
        "into interested, not now, wrong person, objection and more, and show which "
        "campaigns, steps and titles are working.</p>"
    )
    st.stop()

if not anthropic_key:
    log.error("ANTHROPIC_API_KEY is missing from Streamlit secrets.")
    theme.card(
        f'<div class="card-title">{len(replies):,} replies loaded, waiting for classification</div>'
        "<p>Add an <code>ANTHROPIC_API_KEY</code> to the app's secrets to turn on "
        "reply classification.</p>"
    )
    st.stop()


# --- Classification ----------------------------------------------------------

classifier = get_classifier(anthropic_key, model)
records = replies[["reply_id", "subject", "body", "title", "company"]].to_dict("records")

try:
    pending = sum(classifier.cache.classification(r["reply_id"]) is None for r in records)
    progress = st.progress(0.0, text=f"Classifying {pending:,} new replies…") if pending else None

    def on_progress(done: int, total: int) -> None:
        progress.progress(done / total, text=f"Classifying new replies… {done:,} of {total:,}")

    classifications, run = classifier.classify(records, on_progress if pending else None)
    if progress:
        progress.empty()

    df = analysis.join_categories(replies, classifications)
    objections = df[df["category"] == "objection"][
        ["reply_id", "subject", "body", "title", "company"]
    ].to_dict("records")
    with st.spinner("Drafting responses to objections…"):
        responses, draft_run = classifier.draft_objection_responses(objections)
except ClassifierAuthError as err:
    log.error("Anthropic API rejected the key: %s", err)
    st.info("Reply classification isn't available right now: the Anthropic API key was "
            "rejected. Check ANTHROPIC_API_KEY in the app's secrets.")
    st.stop()

if run.failed or draft_run.failed:
    st.caption(
        f"{run.failed:,} replies couldn't be classified this time"
        + (f" and {draft_run.failed:,} objection responses couldn't be drafted" if draft_run.failed else "")
        + ". They'll be retried on the next refresh."
    )


# --- Analysis ----------------------------------------------------------------

k = analysis.kpis(df, campaigns)
table = analysis.whats_working(df, campaigns, steps)
enough = k["classified"] >= MIN_REPLIES_FOR_INSIGHTS

# AI summary at the top, generated from the aggregated table only.
if enough and not table.empty:
    try:
        with st.spinner("Summarising what's working…"):
            takeaways = ai_summary(
                anthropic_key, model,
                table.round(4).to_csv(index=False),
                analysis.summary_context(k),
            )
        items = "".join(f"<li>{html.escape(t)}</li>" for t in takeaways)
        theme.card(f'<div class="card-label">AI takeaways</div><ul>{items}</ul>')
    except _NoSummary:
        pass  # failure already logged by the classifier; the rest of the page still works

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total replies", f"{k['total_replies']:,}",
          help="Every inbox reply across all campaigns.")
k2.metric(
    "Positive reply rate",
    f"{k['positive_reply_rate']:.1%}" if k["positive_reply_rate"] is not None else "–",
    help=f"Leads with a reply classified Interested ({k['interested_leads']:,}) ÷ "
         f"leads contacted across all campaigns ({k['leads_contacted']:,}).",
)
k3.metric(
    "Most common category",
    analysis.CATEGORY_LABELS.get(k["top_category"], "–"),
    help=(f"{k['top_category_share']:.0%} of classified replies."
          if k["top_category_share"] is not None else None),
)
k4.metric("Referrals found", f"{k['referrals']:,}",
          help="Replies that point you to someone else.")

if not enough:
    theme.card(
        '<div class="card-title">Not much reply data yet</div>'
        f"<p>{k['classified']:,} classified replies so far. The AI takeaways and the "
        f"what's-working tables appear once there are at least {MIN_REPLIES_FOR_INSIGHTS}.</p>"
    )

# Category breakdown: horizontal bars, Interested in the accent, the rest in light purple.
st.subheader("Reply categories")
breakdown = analysis.category_breakdown(df)
if breakdown.empty:
    st.caption("No classified replies yet.")
else:
    chart = (
        alt.Chart(breakdown)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            x=alt.X("replies:Q", title="Replies", axis=alt.Axis(tickCount=5, format="d")),
            y=alt.Y("label:N", title=None, sort="-x", axis=alt.Axis(labelLimit=200)),
            color=alt.condition(
                alt.datum.category == "interested",
                alt.value(theme.ACCENT), alt.value(theme.ACCENT_LIGHT),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="Category"),
                alt.Tooltip("replies:Q", title="Replies", format=","),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
            ],
        )
        .properties(height=max(160, 34 * len(breakdown)))
    )
    st.altair_chart(theme.style_chart(chart), width="stretch")

# What's working.
if enough:
    st.subheader("What's working")
    if table.empty:
        st.caption(f"No group has {analysis.MIN_GROUP_REPLIES}+ replies yet.")
    else:
        st.caption(
            "Positive reply rate = interested leads ÷ leads contacted. EmailBison doesn't "
            "report leads contacted per title or industry, so those are ranked by positive "
            f"share of replies. Groups with fewer than {analysis.MIN_GROUP_REPLIES} replies "
            "are hidden."
        )
        dimensions = list(dict.fromkeys(table["dimension"]))
        for tab, dimension in zip(st.tabs(dimensions), dimensions):
            with tab:
                rows = table[table["dimension"] == dimension].drop(columns="dimension")
                if rows["leads_contacted"].isna().all():
                    rows = rows.drop(columns=["leads_contacted", "positive_reply_rate"])
                st.dataframe(
                    rows, hide_index=True, width="stretch",
                    column_config={
                        "group": st.column_config.TextColumn(dimension, width="large"),
                        "replies": st.column_config.NumberColumn("Replies", format="%d"),
                        "replying_leads": st.column_config.NumberColumn("Replying leads", format="%d"),
                        "interested_leads": st.column_config.NumberColumn("Interested", format="%d"),
                        "leads_contacted": st.column_config.NumberColumn("Leads contacted", format="%d"),
                        "positive_reply_rate": st.column_config.NumberColumn(
                            "Positive reply rate", format="percent"),
                        "positive_share_of_replies": st.column_config.NumberColumn(
                            "Positive share of replies", format="percent"),
                    },
                )

# Action queue.
st.subheader("Action queue")
queue = analysis.action_queue(df, responses)
if queue.empty:
    st.caption("Nothing waiting: no not-now, wrong-person or objection replies.")
else:
    counts = queue["action"].value_counts()
    st.caption(" · ".join(f"{n:,} {a.lower()}" for a, n in counts.items()))
    st.dataframe(
        queue.drop(columns="reply_id"),
        hide_index=True, width="stretch",
        column_config={
            "action": "Action",
            "name": "Name",
            "email": "Email",
            "title": "Title",
            "company": "Company",
            "campaign": "Campaign",
            "step": "Step",
            "date_received": st.column_config.DatetimeColumn("Replied", format="MMM D, YYYY"),
            "follow_up_date": st.column_config.DateColumn("Follow up on", format="MMM D, YYYY"),
            "referral_name": "Referral",
            "suggested_response": st.column_config.TextColumn("Suggested response", width="large"),
            "reply": st.column_config.TextColumn("Their reply", width="large"),
            "reason": st.column_config.TextColumn("Why", width="medium"),
        },
    )
    st.download_button(
        "Download CSV",
        data=queue.to_csv(index=False).encode("utf-8"),
        file_name="reply_action_queue.csv",
        mime="text/csv",
        type="primary",
    )
