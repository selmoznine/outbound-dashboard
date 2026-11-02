"""Orcus GTM brand styling shared by every page.

The light theme itself (white background, light grey secondary background, Inter, purple
primary) lives in `.streamlit/config.toml`; this module adds the CSS on top of it: hidden
Streamlit chrome, the pill/title header, KPI cards, and the page navigation links.
"""

from pathlib import Path

import altair as alt
import streamlit as st

# Brand palette (orcusgtm.com).
ACCENT = "#875AF2"
ACCENT_LIGHT = "#C9B6FA"
TEXT = "#000000"
TEXT_MUTED = "#6B7280"
CARD_BG = "#F5F5F7"
BORDER = "#E5E7EB"
FONT = "Inter, system-ui, -apple-system, sans-serif"

FAVICON = str(Path(__file__).resolve().parent.parent / "assets" / "favicon.png")

# Every page, in nav order: (script path relative to the app root, label).
PAGES = [
    ("app.py", "Outbound Performance"),
    ("pages/reply_intelligence.py", "Reply Intelligence"),
]


def setup_page(title: str) -> None:
    """Set the page config and inject the brand CSS. Call first on every page."""
    st.set_page_config(page_title=f"{title} | Orcus", page_icon=FAVICON, layout="wide")
    st.markdown(_CSS, unsafe_allow_html=True)


def hero(pill: str, title: str, subtitle: str) -> None:
    """Page header: nav links (current page in the accent), pill badge, title and subtitle."""
    with st.container(horizontal=True, gap="medium"):
        for path, label in PAGES:
            if label == title:
                st.markdown(f'<span class="nav-current">{label}</span>', width="content",
                            unsafe_allow_html=True)
            else:
                st.page_link(path, label=label, width="content")
    st.markdown(
        f"""
        <div class="hero">
          <span class="pill">{pill}</span>
          <div class="dash-title" role="heading" aria-level="1">{title}</div>
          <p class="subtitle">{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def card(html: str) -> None:
    """A light grey card matching the KPI cards, for free-form HTML content."""
    st.markdown(f'<div class="card">{html}</div>', unsafe_allow_html=True)


# Every percentage in the app is shown to two decimals (e.g. 0.03%).
PCT = ".2%"                # Python f-strings and Altair/d3 formats
PCT_COLUMN = "%.2f%%"      # st.column_config.NumberColumn, on values already x100


def percent_columns(df, columns):
    """Copy of `df` with fractional rate columns scaled to 0-100 for `PCT_COLUMN`.

    st.column_config's built-in "percent" format drops trailing zeros (9.3% vs 9.30%),
    so tables scale the values and use a fixed two-decimal format instead.
    """
    out = df.copy()
    for col in columns:
        if col in out:
            out[col] = out[col].astype("Float64") * 100
    return out


def style_chart(chart: alt.Chart) -> alt.Chart:
    """Apply the brand's Altair styling (transparent background, Inter, muted axes)."""
    return (
        chart.configure(background="transparent", font="Inter")
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=TEXT_MUTED, titleColor=TEXT_MUTED, gridColor=BORDER,
            domainColor=BORDER, tickColor=BORDER, labelFontSize=12,
        )
        .configure_title(color=TEXT, anchor="start", fontSize=14, fontWeight=600)
    )


_CSS = f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* Hide all Streamlit chrome: header bar, toolbar, menu, deploy button,
       running indicator, decoration strip, footer, sidebar and hosting badges. */
    header, header[data-testid="stHeader"], footer, #MainMenu,
    [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    [data-testid="stAppDeployButton"], [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"], [class*="viewerBadge"] {{
        display: none !important;
    }}

    .stApp {{ font-family: {FONT}; }}
    h1, h2, h3 {{ font-family: {FONT}; font-weight: 700 !important; color: {TEXT}; }}
    [data-testid="stMainBlockContainer"], .block-container {{ padding-top: 2rem; }}
    [data-testid="stCaptionContainer"], .stCaption {{ color: {TEXT_MUTED}; }}

    /* Page navigation links: muted text, accent when active or hovered. */
    [data-testid="stPageLink"] a {{
        padding: 4px 0;
        background: transparent !important;
    }}
    [data-testid="stPageLink"] a p {{ color: {TEXT_MUTED}; font-weight: 500; font-size: 14px; }}
    [data-testid="stPageLink"] a:hover p {{ color: {ACCENT}; }}
    .nav-current {{
        display: inline-block; padding: 4px 0; line-height: 1.6;
        color: {ACCENT}; font-weight: 600; font-size: 14px;
    }}

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

    /* Free-form cards (AI summary, empty states). */
    .card {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 16px;
        padding: 18px 22px;
        margin: 4px 0 12px;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06);
        color: {TEXT};
    }}
    .card .card-label {{
        color: {ACCENT}; font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
        text-transform: uppercase; margin-bottom: 8px;
    }}
    .card ul {{ margin: 0; padding-left: 1.2rem; }}
    .card li {{ margin: 4px 0; line-height: 1.5; }}
    .card p {{ margin: 0; color: {TEXT_MUTED}; }}
    .card .card-title {{ color: {TEXT}; font-weight: 600; font-size: 16px; margin-bottom: 4px; }}
    </style>
"""
