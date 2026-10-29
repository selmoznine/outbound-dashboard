"""Outbound dashboard: live campaign stats from the EmailBison API.

Secrets (in .streamlit/secrets.toml locally, or the Streamlit Cloud secrets UI):
    EMAILBISON_API_KEY  - API token from Settings -> Developer API in EmailBison
    EMAILBISON_BASE_URL - optional; your instance URL (defaults to dedi.emailbison.com)
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import altair as alt
import pandas as pd
import requests
import streamlit as st

DEFAULT_BASE_URL = "https://dedi.emailbison.com"
CACHE_TTL_SECONDS = 300
STATS_WORKERS = 5

st.set_page_config(page_title="Outbound Dashboard", page_icon="📬", layout="wide")


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


def rate(numerator, denominator) -> float:
    return numerator / denominator if denominator else 0.0


# --- Page --------------------------------------------------------------------

api_key, base_url = get_config()

st.title("Outbound Dashboard")
st.caption(f"Live campaign stats from EmailBison · cached for {CACHE_TTL_SECONDS // 60} minutes")

try:
    campaigns = fetch_campaigns(api_key, base_url)
except requests.HTTPError as err:
    st.error(f"EmailBison returned {err.response.status_code} when listing campaigns. "
             "Check the API key and base URL.")
    st.stop()
except requests.RequestException as err:
    st.error(f"Could not reach EmailBison: {err}")
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
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()

# The date picker returns a 1-tuple while the user is mid-selection.
if not isinstance(date_range, tuple) or len(date_range) != 2:
    st.info("Pick an end date to load stats.")
    st.stop()
start_date, end_date = date_range

filtered = campaigns[campaigns["name"].isin(selected)] if selected else campaigns

try:
    with st.spinner(f"Loading stats for {len(filtered)} campaign(s)…"):
        stats = fetch_all_stats(api_key, base_url, filtered, start_date, end_date)
except requests.HTTPError as err:
    st.error(f"EmailBison returned {err.response.status_code} when loading campaign stats.")
    st.stop()
except requests.RequestException as err:
    st.error(f"Could not reach EmailBison: {err}")
    st.stop()

stats["reply_rate"] = [rate(r, c) for r, c in zip(stats["replies"], stats["leads_contacted"])]
stats["positive_reply_rate"] = [
    rate(i, c) for i, c in zip(stats["interested"], stats["leads_contacted"])
]

# KPI cards. Rates are recomputed from summed counts so they aren't averages of averages.
total_sent = int(stats["emails_sent"].sum())
total_contacted = int(stats["leads_contacted"].sum())
total_replies = int(stats["replies"].sum())
total_interested = int(stats["interested"].sum())

k1, k2, k3, k4 = st.columns(4)
k1.metric("Emails sent", f"{total_sent:,}", border=True)
k2.metric(
    "Reply rate", f"{rate(total_replies, total_contacted):.1%}", border=True,
    help="Unique replying leads ÷ leads contacted.",
)
k3.metric(
    "Positive reply rate", f"{rate(total_interested, total_contacted):.1%}", border=True,
    help="Leads marked Interested ÷ leads contacted.",
)
k4.metric("Interested leads", f"{total_interested:,}", border=True)

# Reply rate by campaign: horizontal bars, sorted, one hue (single series -> no legend).
st.subheader("Reply rate by campaign")
chart_data = stats[stats["leads_contacted"] > 0]
if chart_data.empty:
    st.info("No leads were contacted in this date range for the selected campaigns.")
else:
    is_dark = getattr(getattr(st.context, "theme", None), "type", None) == "dark"
    bar_color = "#3987e5" if is_dark else "#2a78d6"
    chart = (
        alt.Chart(chart_data)
        .mark_bar(color=bar_color, cornerRadiusEnd=4)
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
    st.altair_chart(chart, width="stretch")

# Table view of the same data.
with st.expander("Campaign table"):
    st.dataframe(
        stats.sort_values("reply_rate", ascending=False)[
            ["campaign", "status", "emails_sent", "leads_contacted", "replies",
             "reply_rate", "interested", "positive_reply_rate", "bounced"]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "campaign": "Campaign",
            "status": "Status",
            "emails_sent": st.column_config.NumberColumn("Emails sent", format="%d"),
            "leads_contacted": st.column_config.NumberColumn("Leads contacted", format="%d"),
            "replies": st.column_config.NumberColumn("Replies", format="%d"),
            "reply_rate": st.column_config.NumberColumn("Reply rate", format="percent"),
            "interested": st.column_config.NumberColumn("Interested", format="%d"),
            "positive_reply_rate": st.column_config.NumberColumn(
                "Positive reply rate", format="percent"
            ),
            "bounced": st.column_config.NumberColumn("Bounced", format="%d"),
        },
    )
