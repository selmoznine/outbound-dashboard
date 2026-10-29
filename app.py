"""Outbound dashboard: live campaign stats from the EmailBison API.

Secrets (in .streamlit/secrets.toml locally, or the Streamlit Cloud secrets UI):
    EMAILBISON_API_KEY  - API token from Settings -> Developer API in EmailBison
    EMAILBISON_BASE_URL - optional; your instance URL (defaults to dedi.emailbison.com)
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import requests
import streamlit as st

DEFAULT_BASE_URL = "https://dedi.emailbison.com"
CACHE_TTL_SECONDS = 300
STATS_WORKERS = 5

# Health thresholds and suggested fixes.
MAX_BOUNCE_RATE = 0.05
MIN_REPLY_RATE = 0.01
FIX_HIGH_BOUNCE = "Re-verify the list; drop invalid and catch-all emails."
FIX_LOW_REPLY = "Tighten the ICP and rewrite the opener/offer."

# Recent interested leads: how many reply pages to scan, and how many rows to show.
MAX_REPLY_PAGES = 20
RECENT_LEADS_LIMIT = 25

# Brand palette (orcusgtm.com).
ACCENT = "#875AF2"
TEXT = "#FFFFFF"
TEXT_MUTED = "#9CA3AF"
GRID = "#1F1F1F"

st.set_page_config(page_title="Outbound Performance", page_icon="📬", layout="wide")

st.markdown(
    f"""
    <style>
    #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {{
        display: none !important;
    }}
    header[data-testid="stHeader"] {{ background: transparent; }}
    [data-testid="stCaptionContainer"], .stCaption {{ color: {TEXT_MUTED}; }}
    [data-testid="stMetric"] {{
        background: #0A0A0A;
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 16px;
        padding: 18px 20px;
    }}
    [data-testid="stMetricLabel"] p {{ color: {TEXT_MUTED}; }}
    [data-testid="stMetricValue"] {{ color: {ACCENT}; }}
    .subtitle {{ color: {TEXT_MUTED}; font-size: 1.05rem; margin-top: -0.75rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# --- API client --------------------------------------------------------------

def get_config() -> tuple[str, str]:
    """Read the API key and base URL from Streamlit secrets."""
    try:
        api_key = st.secrets["EMAILBISON_API_KEY"]
    except (KeyError, FileNotFoundError):
        st.error(
            "Missing `EMAILBISON_API_KEY`. Add it to `.streamlit/secrets.toml` "
            "(see `.streamlit/secrets.toml.example`) or to your app's secrets settings."
        )
        st.stop()
    base_url = st.secrets.get("EMAILBISON_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return api_key, base_url


def api_request(method: str, path: str, api_key: str, base_url: str, **kwargs) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, f"{base_url}{path}", headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Loading campaigns…")
def fetch_campaigns(api_key: str, base_url: str) -> pd.DataFrame:
    """GET /api/campaigns, following page-based pagination to the last page."""
    rows, page = [], 1
    while True:
        payload = api_request("GET", "/api/campaigns", api_key, base_url, params={"page": page})
        rows.extend(payload.get("data", []))
        meta = payload.get("meta") or {}
        if page >= meta.get("last_page", page):
            break
        page += 1
    return pd.DataFrame(
        [{"id": c["id"], "name": c["name"], "status": c.get("status")} for c in rows],
        columns=["id", "name", "status"],
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_campaign_stats(
    api_key: str, base_url: str, campaign_id: int, start: date, end: date
) -> dict:
    """POST /api/campaigns/{id}/stats for a date range. Counts come back as strings."""
    payload = api_request(
        "POST",
        f"/api/campaigns/{campaign_id}/stats",
        api_key,
        base_url,
        json={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    data = payload.get("data") or {}
    to_int = lambda key: int(float(data.get(key) or 0))
    return {
        "emails_sent": to_int("emails_sent"),
        "leads_contacted": to_int("total_leads_contacted"),
        "replies": to_int("unique_replies_per_contact"),
        "interested": to_int("interested"),
        "bounced": to_int("bounced"),
    }


def fetch_all_stats(api_key, base_url, campaigns: pd.DataFrame, start, end) -> pd.DataFrame:
    """Fetch stats for each campaign in parallel (each call is cached individually)."""
    def one(row):
        stats = fetch_campaign_stats(api_key, base_url, int(row.id), start, end)
        return {"id": row.id, "campaign": row.name, "status": row.status, **stats}

    with ThreadPoolExecutor(max_workers=STATS_WORKERS) as pool:
        results = list(pool.map(one, campaigns.itertuples(index=False)))
    return pd.DataFrame(results)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_daily_events(
    api_key: str, base_url: str, campaign_ids: tuple[int, ...], start: date, end: date
) -> pd.DataFrame:
    """GET /api/campaign-events/stats: per-day event counts. No campaign_ids = all campaigns."""
    params = [("start_date", start.isoformat()), ("end_date", end.isoformat())]
    params += [("campaign_ids[]", cid) for cid in campaign_ids]
    payload = api_request("GET", "/api/campaign-events/stats", api_key, base_url, params=params)
    rows = [
        {"date": day, "event": series["label"], "count": int(float(count or 0))}
        for series in payload.get("data", [])
        for day, count in series.get("dates", [])
    ]
    df = pd.DataFrame(rows, columns=["date", "event", "count"])
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_interested_replies(api_key: str, base_url: str, since: date) -> pd.DataFrame:
    """GET /api/replies?status=interested, scanning pages until replies predate `since`."""
    rows = []
    for page in range(1, MAX_REPLY_PAGES + 1):
        payload = api_request(
            "GET", "/api/replies", api_key, base_url,
            params={"status": "interested", "page": page},
        )
        replies = payload.get("data", [])
        for r in replies:
            lead = r.get("lead") or {}
            name = " ".join(filter(None, [lead.get("first_name"), lead.get("last_name")]))
            rows.append({
                "lead_id": r.get("lead_id"),
                "name": name or r.get("from_name") or r.get("from_email_address"),
                "company": lead.get("company"),
                "campaign_id": r.get("campaign_id"),
                "reply_date": r.get("date_received"),
            })
        meta = payload.get("meta") or {}
        dates = pd.to_datetime([r.get("date_received") for r in replies], utc=True, errors="coerce")
        page_is_old = len(dates) and dates.max().date() < since
        if not replies or page >= meta.get("last_page", page) or page_is_old:
            break
    df = pd.DataFrame(rows, columns=["lead_id", "name", "company", "campaign_id", "reply_date"])
    df["reply_date"] = pd.to_datetime(df["reply_date"], utc=True, errors="coerce")
    return df


# --- Helpers -----------------------------------------------------------------

def rate(numerator, denominator) -> float:
    return numerator / denominator if denominator else 0.0


def totals(df: pd.DataFrame) -> dict:
    sent, contacted = int(df["emails_sent"].sum()), int(df["leads_contacted"].sum())
    replies, interested = int(df["replies"].sum()), int(df["interested"].sum())
    return {
        "sent": sent,
        "reply_rate": rate(replies, contacted),
        "positive_rate": rate(interested, contacted),
        "interested": interested,
    }


def pct_change(current: float, previous: float) -> str | None:
    """Relative change for counts, e.g. '+12.5%'. None when there's no prior baseline."""
    return f"{(current - previous) / previous:+.1%}" if previous else None


def pts_change(current: float, previous: float) -> str:
    """Change in percentage points for rates, e.g. '-0.4 pts'."""
    return f"{(current - previous) * 100:+.1f} pts"


def health(row) -> tuple[str, str]:
    if row.emails_sent == 0:
        return "– No sends", ""
    issues, fixes = [], []
    if row.bounce_rate > MAX_BOUNCE_RATE:
        issues.append("High bounce")
        fixes.append(FIX_HIGH_BOUNCE)
    if row.leads_contacted and row.reply_rate < MIN_REPLY_RATE:
        issues.append("Low reply")
        fixes.append(FIX_LOW_REPLY)
    if not issues:
        return "✅ Healthy", ""
    return "⚠️ " + " + ".join(issues), " ".join(fixes)


def style_chart(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure(background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=TEXT_MUTED, titleColor=TEXT_MUTED, gridColor=GRID,
            domainColor=GRID, tickColor=GRID, labelFontSize=12,
        )
        .configure_title(color=TEXT, anchor="start", fontSize=14, fontWeight=500)
    )


def show_api_error(err: requests.RequestException, what: str):
    if isinstance(err, requests.HTTPError):
        st.error(f"EmailBison returned {err.response.status_code} when {what}.")
    else:
        st.error(f"Could not reach EmailBison when {what}: {err}")


# --- Page --------------------------------------------------------------------

api_key, base_url = get_config()

st.title("Outbound Performance")
st.markdown('<p class="subtitle">Slipstream Engineering</p>', unsafe_allow_html=True)

try:
    campaigns = fetch_campaigns(api_key, base_url)
except requests.RequestException as err:
    show_api_error(err, "listing campaigns")
    st.caption("Check the API key and base URL.")
    st.stop()

if campaigns.empty:
    st.info("No campaigns found in this workspace.")
    st.stop()

# Filters: one row above the charts.
col_campaigns, col_dates, col_refresh = st.columns([3, 2, 1], vertical_alignment="bottom")
with col_campaigns:
    selected = st.multiselect(
        "Campaigns",
        options=campaigns["name"].tolist(),
        placeholder="All campaigns",
        help="Leave empty to include every campaign.",
    )
with col_dates:
    today = date.today()
    date_range = st.date_input(
        "Date range", value=(today - timedelta(days=30), today), max_value=today
    )
with col_refresh:
    if st.button("Refresh data", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

# The date picker returns a 1-tuple while the user is mid-selection.
if not isinstance(date_range, tuple) or len(date_range) != 2:
    st.info("Pick an end date to load stats.")
    st.stop()
start_date, end_date = date_range

filtered = campaigns[campaigns["name"].isin(selected)] if selected else campaigns

# Week-over-week windows: the 7 days ending on the selected end date vs the 7 days before.
this_week = (end_date - timedelta(days=6), end_date)
last_week = (end_date - timedelta(days=13), end_date - timedelta(days=7))

try:
    with st.spinner(f"Loading stats for {len(filtered)} campaign(s)…"):
        stats = fetch_all_stats(api_key, base_url, filtered, start_date, end_date)
        stats_this_week = fetch_all_stats(api_key, base_url, filtered, *this_week)
        stats_last_week = fetch_all_stats(api_key, base_url, filtered, *last_week)
except requests.RequestException as err:
    show_api_error(err, "loading campaign stats")
    st.stop()

stats["reply_rate"] = [rate(r, c) for r, c in zip(stats["replies"], stats["leads_contacted"])]
stats["positive_reply_rate"] = [
    rate(i, c) for i, c in zip(stats["interested"], stats["leads_contacted"])
]
stats["bounce_rate"] = [rate(b, s) for b, s in zip(stats["bounced"], stats["emails_sent"])]
flags = [health(row) for row in stats.itertuples(index=False)]
stats["health"] = [label for label, _ in flags]
stats["suggested_fix"] = [fix for _, fix in flags]

# KPI cards. Values cover the selected date range; deltas are week over week.
# Rates are recomputed from summed counts so they aren't averages of averages.
kpi, cur, prev = totals(stats), totals(stats_this_week), totals(stats_last_week)
wow_help = (
    f"Change compares {this_week[0]:%b %d}–{this_week[1]:%b %d} "
    f"with {last_week[0]:%b %d}–{last_week[1]:%b %d}."
)

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "Emails sent", f"{kpi['sent']:,}",
    delta=pct_change(cur["sent"], prev["sent"]), help=wow_help,
)
k2.metric(
    "Reply rate", f"{kpi['reply_rate']:.1%}",
    delta=pts_change(cur["reply_rate"], prev["reply_rate"]),
    help="Unique replying leads ÷ leads contacted. " + wow_help,
)
k3.metric(
    "Positive reply rate", f"{kpi['positive_rate']:.1%}",
    delta=pts_change(cur["positive_rate"], prev["positive_rate"]),
    help="Leads marked Interested ÷ leads contacted. " + wow_help,
)
k4.metric(
    "Interested leads", f"{kpi['interested']:,}",
    delta=pct_change(cur["interested"], prev["interested"]), help=wow_help,
)
st.caption("Arrows show week-over-week change: last 7 days vs the 7 days before.")

# Daily trend. Sent is ~100x replies, so each event gets its own panel and y-scale
# instead of sharing one axis (which would flatten replies and interested to zero).
st.subheader("Daily trend")
try:
    campaign_ids = tuple(int(i) for i in filtered["id"]) if selected else ()
    daily = fetch_daily_events(api_key, base_url, campaign_ids, start_date, end_date)
except requests.RequestException as err:
    show_api_error(err, "loading daily stats")
    daily = pd.DataFrame(columns=["date", "event", "count"])

trend_cols = st.columns(3)
for col, (event, title) in zip(
    trend_cols, [("Sent", "Emails sent"), ("Replied", "Replies"), ("Interested", "Interested leads")]
):
    series = daily[daily["event"] == event]
    with col:
        if series.empty:
            st.caption(f"No {title.lower()} data for this range.")
            continue
        hover = alt.selection_point(on="pointerover", nearest=True, fields=["date"], empty=False)
        base = alt.Chart(series, title=title).encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", tickCount=4)),
            y=alt.Y("count:Q", title=None, axis=alt.Axis(tickCount=4)),
        )
        line = base.mark_line(color=ACCENT, strokeWidth=2)
        points = base.mark_point(color=ACCENT, filled=True, size=70).encode(
            opacity=alt.condition(hover, alt.value(1), alt.value(0)),
            tooltip=[
                alt.Tooltip("date:T", title="Date", format="%a %b %d"),
                alt.Tooltip("count:Q", title=title, format=","),
            ],
        ).add_params(hover)
        rule = base.mark_rule(color=TEXT_MUTED, strokeWidth=1).encode(
            opacity=alt.condition(hover, alt.value(0.5), alt.value(0))
        )
        st.altair_chart(style_chart((line + rule + points).properties(height=200)), width="stretch")

# Reply rate by campaign: horizontal bars, sorted, one hue (single series -> no legend).
st.subheader("Reply rate by campaign")
chart_data = stats[stats["leads_contacted"] > 0]
if chart_data.empty:
    st.info("No leads were contacted in this date range for the selected campaigns.")
else:
    chart = (
        alt.Chart(chart_data)
        .mark_bar(color=ACCENT, cornerRadiusEnd=4)
        .encode(
            x=alt.X("reply_rate:Q", title="Reply rate", axis=alt.Axis(format="%", tickCount=5)),
            y=alt.Y("campaign:N", title=None, sort="-x", axis=alt.Axis(labelLimit=260)),
            tooltip=[
                alt.Tooltip("campaign:N", title="Campaign"),
                alt.Tooltip("reply_rate:Q", title="Reply rate", format=".1%"),
                alt.Tooltip("replies:Q", title="Replies", format=","),
                alt.Tooltip("leads_contacted:Q", title="Leads contacted", format=","),
                alt.Tooltip("positive_reply_rate:Q", title="Positive reply rate", format=".1%"),
            ],
        )
        .properties(height=max(160, 32 * len(chart_data)))
    )
    st.altair_chart(style_chart(chart), width="stretch")

# Campaign table with health flags and CSV export.
st.subheader("Campaigns")
table = stats.sort_values("reply_rate", ascending=False)[
    ["campaign", "status", "health", "suggested_fix", "emails_sent", "leads_contacted",
     "replies", "reply_rate", "interested", "positive_reply_rate", "bounced", "bounce_rate"]
]
flagged = int(table["health"].str.startswith("⚠️").sum())
st.caption(
    f"{flagged} campaign(s) flagged: bounce rate over {MAX_BOUNCE_RATE:.0%} "
    f"or reply rate under {MIN_REPLY_RATE:.0%}."
)
st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    column_config={
        "campaign": "Campaign",
        "status": "Status",
        "health": "Health",
        "suggested_fix": st.column_config.TextColumn("Suggested fix", width="large"),
        "emails_sent": st.column_config.NumberColumn("Emails sent", format="%d"),
        "leads_contacted": st.column_config.NumberColumn("Leads contacted", format="%d"),
        "replies": st.column_config.NumberColumn("Replies", format="%d"),
        "reply_rate": st.column_config.NumberColumn("Reply rate", format="percent"),
        "interested": st.column_config.NumberColumn("Interested", format="%d"),
        "positive_reply_rate": st.column_config.NumberColumn(
            "Positive reply rate", format="percent"
        ),
        "bounced": st.column_config.NumberColumn("Bounced", format="%d"),
        "bounce_rate": st.column_config.NumberColumn("Bounce rate", format="percent"),
    },
)
st.download_button(
    "Download CSV",
    data=table.to_csv(index=False).encode("utf-8"),
    file_name=f"campaigns_{start_date}_{end_date}.csv",
    mime="text/csv",
    type="primary",
)

# Recent interested leads, from replies marked Interested.
st.subheader("Recent interested leads")
try:
    interested = fetch_interested_replies(api_key, base_url, start_date)
except requests.RequestException as err:
    show_api_error(err, "loading interested replies")
    interested = pd.DataFrame(columns=["lead_id", "name", "company", "campaign_id", "reply_date"])
    interested["reply_date"] = pd.to_datetime(interested["reply_date"], utc=True)

campaign_names = dict(zip(campaigns["id"], campaigns["name"]))
interested = interested[
    interested["campaign_id"].isin(filtered["id"])
    & interested["reply_date"].dt.date.between(start_date, end_date)
].copy()
interested["campaign"] = interested["campaign_id"].map(campaign_names)
recent = (
    interested.sort_values("reply_date", ascending=False)
    .drop_duplicates("lead_id")  # one row per lead: their latest interested reply
    .head(RECENT_LEADS_LIMIT)
)
if recent.empty:
    st.caption("No interested replies in this date range for the selected campaigns.")
else:
    st.dataframe(
        recent[["name", "company", "campaign", "reply_date"]],
        hide_index=True,
        width="stretch",
        column_config={
            "name": "Name",
            "company": "Company",
            "campaign": "Campaign",
            "reply_date": st.column_config.DatetimeColumn("Reply date", format="MMM D, YYYY"),
        },
    )
