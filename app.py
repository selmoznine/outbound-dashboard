"""Outbound dashboard: live campaign stats from the EmailBison API.

Secrets (in .streamlit/secrets.toml locally, or the Streamlit Cloud secrets UI):
    EMAILBISON_API_KEY  - API token from Settings -> Developer API in EmailBison
    EMAILBISON_BASE_URL - optional; your instance URL (defaults to dedi.emailbison.com)
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

DEFAULT_BASE_URL = "https://dedi.emailbison.com"
CACHE_TTL_SECONDS = 300
STATS_WORKERS = 5
CAMPAIGN_IDS_PER_REQUEST = 50  # keeps daily-stats query strings a sane length

log = logging.getLogger("outbound_dashboard")

# Health thresholds and suggested fixes.
MAX_BOUNCE_RATE = 0.05
MIN_REPLY_RATE = 0.01
FIX_HIGH_BOUNCE = "Re-verify the list; drop invalid and catch-all emails."
FIX_LOW_REPLY = "Tighten the ICP and rewrite the opener/offer."

# Date range presets: label -> number of days (inclusive of today). None = special case.
QUICK_RANGES = {
    "Last 7 days": 7,
    "Last 14 days": 14,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "All time": None,
    "Custom": None,
}
DEFAULT_QUICK_RANGE = "Last 30 days"
ALL_TIME_FALLBACK_DAYS = 365 * 5  # used if no campaign reports a created_at

# Recent interested leads: how many reply pages to scan, and how many rows to show.
MAX_REPLY_PAGES = 20
RECENT_LEADS_LIMIT = 25

# Brand palette (orcusgtm.com).
ACCENT = "#875AF2"
TEXT = "#000000"
TEXT_MUTED = "#6B7280"
CARD_BG = "#F5F5F7"
BORDER = "#E5E7EB"
FONT = "Inter, system-ui, -apple-system, sans-serif"

st.set_page_config(
    page_title="Outbound Performance | Orcus",
    page_icon=str(Path(__file__).parent / "assets" / "favicon.png"),
    layout="wide",
)

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* Hide all Streamlit chrome: header bar, toolbar, menu, deploy button,
       running indicator, decoration strip, footer and hosting badges. */
    header, header[data-testid="stHeader"], footer, #MainMenu,
    [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    [data-testid="stAppDeployButton"], [class*="viewerBadge"] {{
        display: none !important;
    }}

    .stApp {{ font-family: {FONT}; }}
    h1, h2, h3 {{ font-family: {FONT}; font-weight: 700 !important; color: {TEXT}; }}
    [data-testid="stMainBlockContainer"], .block-container {{ padding-top: 2rem; }}
    [data-testid="stCaptionContainer"], .stCaption {{ color: {TEXT_MUTED}; }}

    /* Header: pill badge (same style as orcusgtm.com's hero badge), title, subtitle. */
    .pill {{
        display: inline-block;
        padding: 8px 16px;
        border-radius: 9999px;
        background: rgba(135, 90, 242, 0.1);
        border: 1px solid rgba(135, 90, 242, 0.2);
        color: {ACCENT};
        font-size: 14px;
        font-weight: 600;
        line-height: 1.25;
    }}
    .dash-title {{
        font-family: {FONT};
        font-size: 36px;
        font-weight: 700;
        line-height: 1.15;
        letter-spacing: -0.02em;
        color: {TEXT};
        margin: 14px 0 4px;
    }}
    .subtitle {{ color: {TEXT_MUTED}; font-size: 16px; margin: 0 0 8px; }}

    /* KPI cards. */
    [data-testid="stMetric"] {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 16px;
        padding: 18px 20px;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06);
    }}
    [data-testid="stMetricLabel"] p {{ color: {TEXT_MUTED}; font-weight: 500; }}
    [data-testid="stMetricValue"] {{ color: {ACCENT}; font-weight: 700; }}
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
        log.error("EMAILBISON_API_KEY is missing from Streamlit secrets "
                  "(see .streamlit/secrets.toml.example).")
        st.info("This dashboard isn't connected to EmailBison yet.")
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
    df = pd.DataFrame(
        [{"id": c["id"], "name": c["name"], "status": c.get("status"),
          "created_at": c.get("created_at")} for c in rows],
        columns=["id", "name", "status", "created_at"],
    )
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    return df


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


def parse_daily_series(payload: dict) -> pd.DataFrame:
    """Flatten [{label, dates: [[YYYY-MM-DD, count], ...]}, ...] into date/event/count rows."""
    rows = [
        {"date": day, "event": series["label"], "count": int(float(count or 0))}
        for series in payload.get("data", [])
        for day, count in series.get("dates", [])
    ]
    df = pd.DataFrame(rows, columns=["date", "event", "count"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def sum_daily(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=["date", "event", "count"])
    df = pd.concat(frames, ignore_index=True)
    return df.groupby(["date", "event"], as_index=False)["count"].sum()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_daily_events(
    api_key: str, base_url: str, campaign_ids: tuple[int, ...], start: date, end: date
) -> pd.DataFrame:
    """GET /api/campaign-events/stats with YYYY-MM-DD dates and campaign_ids[]=… per the docs.

    campaign_ids is always sent (the endpoint drills down by campaign and/or sender), in
    batches so the query string stays short; batch results are summed per day.
    """
    frames = []
    for i in range(0, len(campaign_ids), CAMPAIGN_IDS_PER_REQUEST):
        params = [("start_date", start.isoformat()), ("end_date", end.isoformat())]
        params += [("campaign_ids[]", cid) for cid in campaign_ids[i:i + CAMPAIGN_IDS_PER_REQUEST]]
        frames.append(parse_daily_series(
            api_request("GET", "/api/campaign-events/stats", api_key, base_url, params=params)
        ))
    return sum_daily(frames)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_campaign_daily(
    api_key: str, base_url: str, campaign_id: int, start: date, end: date
) -> pd.DataFrame:
    """GET /api/campaigns/{id}/line-area-chart-stats: per-day event counts for one campaign."""
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    return parse_daily_series(api_request(
        "GET", f"/api/campaigns/{campaign_id}/line-area-chart-stats", api_key, base_url,
        params=params,
    ))


def load_daily(api_key, base_url, campaign_ids: tuple[int, ...], start, end) -> pd.DataFrame | None:
    """Daily events from campaign-events/stats, falling back to per-campaign chart stats.

    Returns None if both fail, so the caller can hide the section.
    """
    try:
        return fetch_daily_events(api_key, base_url, campaign_ids, start, end)
    except requests.RequestException as err:
        log_api_error(err, "GET /api/campaign-events/stats")
    try:
        with ThreadPoolExecutor(max_workers=STATS_WORKERS) as pool:
            frames = list(pool.map(
                lambda cid: fetch_campaign_daily(api_key, base_url, cid, start, end), campaign_ids
            ))
        return sum_daily(frames)
    except requests.RequestException as err:
        log_api_error(err, "GET /api/campaigns/{id}/line-area-chart-stats")
    return None


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
        chart.configure(background="transparent", font="Inter")
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=TEXT_MUTED, titleColor=TEXT_MUTED, gridColor=BORDER,
            domainColor=BORDER, tickColor=BORDER, labelFontSize=12,
        )
        .configure_title(color=TEXT, anchor="start", fontSize=14, fontWeight=600)
    )


def log_api_error(err: requests.RequestException, what: str):
    """Log full API error details for the app owner (server logs only, never the page)."""
    resp = getattr(err, "response", None)
    if resp is not None:
        log.warning("EmailBison %s failed: HTTP %s %s", what, resp.status_code, resp.text[:500])
    else:
        log.warning("EmailBison %s failed: %r", what, err)


UNAVAILABLE = "We couldn't load data from EmailBison right now. Please try again in a few minutes."


# --- Page --------------------------------------------------------------------

api_key, base_url = get_config()

st.markdown(
    """
    <div class="hero">
      <span class="pill">Live from EmailBison</span>
      <div class="dash-title" role="heading" aria-level="1">Outbound Performance</div>
      <p class="subtitle">Slipstream Engineering</p>
    </div>
    """,
    unsafe_allow_html=True,
)

try:
    campaigns = fetch_campaigns(api_key, base_url)
except requests.RequestException as err:
    log_api_error(err, "GET /api/campaigns")
    st.info(UNAVAILABLE)
    st.stop()

if campaigns.empty:
    st.info("No campaigns found in this workspace.")
    st.stop()

# Filters: one row above the charts.
today = date.today()
col_campaigns, col_range, col_refresh = st.columns([3, 2, 1], vertical_alignment="bottom")
with col_campaigns:
    selected = st.multiselect(
        "Campaigns",
        options=campaigns["name"].tolist(),
        placeholder="All campaigns",
        help="Leave empty to include every campaign.",
    )
with col_range:
    quick_range = st.selectbox(
        "Quick range", options=list(QUICK_RANGES),
        index=list(QUICK_RANGES).index(DEFAULT_QUICK_RANGE),
    )
with col_refresh:
    if st.button("Refresh data", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

if quick_range == "Custom":
    # Two separate pickers, shown only for Custom, under the Quick range dropdown.
    st.session_state.setdefault("custom_start", today - timedelta(days=29))
    st.session_state.setdefault("custom_end", today)
    _, col_custom, _ = st.columns([3, 2, 1])
    with col_custom:
        col_start, col_end = st.columns(2)
        with col_start:
            start_date = st.date_input("Start date", key="custom_start", max_value=today)
        # Keep the end date on or after the start date if the start moves past it.
        if st.session_state["custom_end"] < start_date:
            st.session_state["custom_end"] = start_date
        with col_end:
            end_date = st.date_input(
                "End date", key="custom_end", min_value=start_date, max_value=today
            )
elif quick_range == "All time":
    earliest = campaigns["created_at"].min()
    start_date = (
        earliest.date() if pd.notna(earliest) else today - timedelta(days=ALL_TIME_FALLBACK_DAYS)
    )
    end_date = today
else:
    start_date, end_date = today - timedelta(days=QUICK_RANGES[quick_range] - 1), today

st.caption(f"Showing {start_date:%b %d, %Y} – {end_date:%b %d, %Y}")

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
    log_api_error(err, "POST /api/campaigns/{id}/stats")
    st.info(UNAVAILABLE)
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
# If EmailBison can't serve daily stats, the whole section is hidden.
daily = load_daily(
    api_key, base_url, tuple(int(i) for i in filtered["id"]), start_date, end_date
)
trend_events = [("Sent", "Emails sent"), ("Replied", "Replies"), ("Interested", "Interested leads")]
show_trend = daily is not None and daily["event"].isin([e for e, _ in trend_events]).any()
if show_trend:
    st.subheader("Daily trend")
for col, (event, title) in zip(st.columns(3) if show_trend else [], trend_events):
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

# Recent interested leads, from replies marked Interested. Hidden if EmailBison fails.
try:
    interested = fetch_interested_replies(api_key, base_url, start_date)
except requests.RequestException as err:
    log_api_error(err, "GET /api/replies")
    st.stop()
st.subheader("Recent interested leads")

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
