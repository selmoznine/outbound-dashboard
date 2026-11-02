"""Join reply categories back to lead and campaign attributes, and aggregate them.

Definitions used on the Reply Intelligence page:

- Positive reply rate (headline KPI): leads with at least one reply classified `interested`
  ÷ leads contacted across all campaigns.
- What's working, by campaign and by sequence step / variant: the same positive reply rate,
  using EmailBison's leads-contacted counts for each campaign and each step.
- What's working, by title seniority, industry and company size: EmailBison doesn't report
  how many leads were contacted per title or industry, so these groups are ranked by
  positive share of replies (interested leads ÷ leads who replied) instead.
- Groups with fewer than `MIN_GROUP_REPLIES` replies are hidden as too small to read.
"""

from __future__ import annotations

import re

import pandas as pd

MIN_GROUP_REPLIES = 5

CATEGORY_LABELS = {
    "interested": "Interested",
    "not_now": "Not now",
    "wrong_person": "Wrong person",
    "objection": "Objection",
    "unsubscribe": "Unsubscribe",
    "out_of_office": "Out of office",
    "other": "Other",
}

SENIORITY_ORDER = ["C-level", "VP", "Director", "Manager", "IC", "Unknown"]

_VP = re.compile(r"\b(vp|svp|evp|avp|vice[\s-]*president)\b", re.I)
_C_LEVEL = re.compile(
    r"\b(chief|c[a-z]{1,3}o|founder|co[\s-]?founder|owner|president|managing director|"
    r"general partner|managing partner)\b",
    re.I,
)
_DIRECTOR = re.compile(r"\b(director|head of|head,|dir\.)\b|\bhead\b", re.I)
_MANAGER = re.compile(r"\b(manager|mgr|lead|supervisor|team lead)\b", re.I)


def title_seniority(title: str) -> str:
    """Bucket a job title into C-level, VP, Director, Manager or IC ("Unknown" if blank).

    VP is checked before C-level so "Vice President" isn't read as "President".
    """
    title = (title or "").strip()
    if not title:
        return "Unknown"
    if _VP.search(title):
        return "VP"
    if _C_LEVEL.search(title):
        return "C-level"
    if _DIRECTOR.search(title):
        return "Director"
    if _MANAGER.search(title):
        return "Manager"
    return "IC"


def company_size_bucket(value: str) -> str:
    """Normalise a company size custom variable ("51-200", "1,200", "500+") to a band."""
    value = (value or "").strip()
    if not value:
        return ""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", value)]
    if not numbers:
        return value
    n = numbers[0]
    for upper, label in [(10, "1-10"), (50, "11-50"), (200, "51-200"), (1000, "201-1,000"),
                         (5000, "1,001-5,000")]:
        if n <= upper:
            return label
    return "5,000+"


def join_categories(replies: pd.DataFrame, classifications: dict[str, dict]) -> pd.DataFrame:
    """Attach classifier output and derived lead attributes to each reply.

    Replies the classifier hasn't handled yet get category "unclassified" and are left out
    of the rates.
    """
    df = replies.copy()
    keys = df["reply_id"].astype(str)
    for field in ("category", "confidence", "reason", "referral_name", "follow_up_date"):
        df[field] = keys.map(lambda k, f=field: (classifications.get(k) or {}).get(f))
    df["category"] = df["category"].fillna("unclassified")
    df["seniority"] = df["title"].map(title_seniority)
    df["industry"] = df["industry"].replace("", pd.NA).fillna("Unknown")
    df["company_size"] = df["company_size"].map(company_size_bucket).replace("", "Unknown")
    df["lead_key"] = df["lead_id"].fillna(df["email"]).astype(str)
    return df


def kpis(df: pd.DataFrame, campaigns: pd.DataFrame) -> dict:
    """Headline numbers: replies, positive reply rate, top category, referrals found."""
    classified = df[df["category"] != "unclassified"]
    contacted = int(campaigns["leads_contacted"].sum()) if not campaigns.empty else 0
    interested_leads = classified.loc[classified["category"] == "interested", "lead_key"].nunique()
    top = classified["category"].value_counts()
    referrals = classified["referral_name"].fillna("").str.strip().ne("").sum()
    return {
        "total_replies": len(df),
        "classified": len(classified),
        "leads_contacted": contacted,
        "interested_leads": int(interested_leads),
        "positive_reply_rate": interested_leads / contacted if contacted else None,
        "top_category": top.index[0] if len(top) else None,
        "top_category_share": top.iloc[0] / len(classified) if len(top) else None,
        "referrals": int(referrals),
    }


def category_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Reply count and share per category, largest first."""
    classified = df[df["category"] != "unclassified"]
    counts = classified["category"].value_counts().rename_axis("category").reset_index(name="replies")
    counts["share"] = counts["replies"] / counts["replies"].sum() if len(counts) else 0.0
    counts["label"] = counts["category"].map(CATEGORY_LABELS)
    return counts


def whats_working(df: pd.DataFrame, campaigns: pd.DataFrame, steps: pd.DataFrame) -> pd.DataFrame:
    """Positive reply performance by campaign, step, variant, seniority, industry and size.

    Returns one row per (dimension, group) with: replies, replying leads, interested leads,
    leads contacted (campaign/step/variant only), positive reply rate (interested ÷
    contacted, where known) and positive share of replies (interested ÷ replying leads).
    Sorted best first within each dimension; groups under MIN_GROUP_REPLIES replies dropped.
    """
    classified = df[df["category"] != "unclassified"].copy()
    if classified.empty:
        return _empty_table()

    steps = steps if steps is not None else pd.DataFrame()
    step_info = (steps.set_index("sequence_step_id") if not steps.empty else pd.DataFrame())
    classified["step_group"] = classified["sequence_step_id"].map(
        step_info["step_number"] if not step_info.empty else {}
    ).map(lambda n: f"Step {int(n)}" if pd.notna(n) else "Unknown step")
    classified["variant_group"] = classified["campaign"] + " · " + classified["step"]

    # Leads contacted per group, where EmailBison reports it.
    contacted = {
        "Campaign": dict(zip(campaigns["campaign"], campaigns["leads_contacted"])),
    }
    if not steps.empty:
        names = dict(zip(campaigns["campaign_id"], campaigns["campaign"]))
        s = steps.dropna(subset=["step_leads_contacted"]).copy()
        s["campaign"] = s["campaign_id"].map(names)
        s["variant_group"] = s["campaign"] + " · " + s["step_label"]
        s["step_group"] = s["step_number"].map(lambda n: f"Step {int(n)}")
        contacted["Sequence step"] = s.groupby("step_group")["step_leads_contacted"].sum().to_dict()
        contacted["Step variant"] = s.groupby("variant_group")["step_leads_contacted"].sum().to_dict()

    dimensions = [
        ("Campaign", "campaign"),
        ("Sequence step", "step_group"),
        ("Step variant", "variant_group"),
        ("Title seniority", "seniority"),
        ("Industry", "industry"),
        ("Company size", "company_size"),
    ]
    frames = []
    for dimension, column in dimensions:
        if column in ("industry", "company_size") and (classified[column] == "Unknown").all():
            continue  # no custom variable for it in this workspace
        g = classified.groupby(column)
        table = pd.DataFrame({
            "replies": g.size(),
            "replying_leads": g["lead_key"].nunique(),
            "interested_leads": g.apply(
                lambda x: x.loc[x["category"] == "interested", "lead_key"].nunique(),
                include_groups=False,
            ),
        }).reset_index().rename(columns={column: "group"})
        table.insert(0, "dimension", dimension)
        lookup = contacted.get(dimension, {})
        table["leads_contacted"] = table["group"].map(lookup).astype("Float64")
        table.loc[table["leads_contacted"] <= 0, "leads_contacted"] = pd.NA
        table["positive_reply_rate"] = table["interested_leads"] / table["leads_contacted"]
        table["positive_share_of_replies"] = table["interested_leads"] / table["replying_leads"]
        table = table[table["replies"] >= MIN_GROUP_REPLIES]
        sort_by = "positive_reply_rate" if table["positive_reply_rate"].notna().any() else \
            "positive_share_of_replies"
        frames.append(table.sort_values(
            [sort_by, "positive_share_of_replies", "replies"], ascending=False, na_position="last"
        ))
    return pd.concat(frames, ignore_index=True) if frames else _empty_table()


def action_queue(df: pd.DataFrame, responses: dict[str, str]) -> pd.DataFrame:
    """Replies that need a human: follow-ups, referrals and objections.

    not_now replies carry their follow-up date (soonest first), wrong_person replies the
    referral name, objection replies a drafted 2-sentence response.
    """
    actions = {"not_now": "Follow up later", "wrong_person": "Contact referral",
               "objection": "Answer objection"}
    q = df[df["category"].isin(actions)].copy()
    if q.empty:
        return pd.DataFrame(columns=[
            "action", "name", "email", "title", "company", "campaign", "step", "date_received",
            "follow_up_date", "referral_name", "suggested_response", "reply", "reason", "reply_id",
        ])
    q["action"] = q["category"].map(actions)
    q["suggested_response"] = q["reply_id"].astype(str).map(responses).where(
        q["category"] == "objection"
    )
    q["follow_up_date"] = pd.to_datetime(q["follow_up_date"], errors="coerce").dt.date
    q["referral_name"] = q["referral_name"].fillna("")
    q["suggested_response"] = q["suggested_response"].fillna("")
    q["reply"] = q["body"].str.replace(r"\s+", " ", regex=True).str.slice(0, 300)
    order = {"not_now": 0, "wrong_person": 1, "objection": 2}
    q["_order"] = q["category"].map(order)
    q = q.sort_values(["_order", "follow_up_date", "date_received"],
                      ascending=[True, True, False], na_position="last")
    return q[[
        "action", "name", "email", "title", "company", "campaign", "step", "date_received",
        "follow_up_date", "referral_name", "suggested_response", "reply", "reason", "reply_id",
    ]].reset_index(drop=True)


def summary_context(k: dict) -> str:
    """Headline numbers handed to the AI summary alongside the aggregated table."""
    parts = [
        f"Total replies: {k['total_replies']}.",
        f"Leads contacted: {k['leads_contacted']}.",
        f"Interested leads: {k['interested_leads']}.",
    ]
    if k["positive_reply_rate"] is not None:
        parts.append(f"Overall positive reply rate: {k['positive_reply_rate']:.2%}.")
    parts.append(
        "Columns: positive_reply_rate = interested leads ÷ leads contacted (campaign and step"
        " rows only); positive_share_of_replies = interested leads ÷ leads who replied."
    )
    return " ".join(parts)


def _empty_table() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "dimension", "group", "replies", "replying_leads", "interested_leads",
        "leads_contacted", "positive_reply_rate", "positive_share_of_replies",
    ])
