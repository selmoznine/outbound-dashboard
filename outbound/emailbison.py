"""EmailBison API client and the reply data pull for Reply Intelligence.

Endpoints used (see https://docs.emailbison.com and your instance's /api/reference):

    GET  /api/campaigns                              campaigns, incl. total_leads_contacted
    GET  /api/replies?folder=inbox                   every reply, with the full lead embedded
    GET  /api/scheduled-emails/{id}                  the email a reply answered -> sequence_step_id
    GET  /api/campaigns/v1.1/{id}/sequence-steps     step order and variant info
    POST /api/campaigns/{id}/stats                   per-step leads contacted (sequence_step_stats)

Pagination: index routes return 15 rows per page. Page-based pagination stops at 1,000 pages,
so `paginate` reads page 1, then fetches the remaining pages in parallel when the total fits
under that cap, and switches to cursor pagination (`pagination_type=cursor`, following
`meta.next_cursor`) when it doesn't. There is no artificial cap on the number of rows.

Everything here is plain Python (no Streamlit), so it can be tested or reused elsewhere;
the page wraps the top-level loaders in `st.cache_data`.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import requests

log = logging.getLogger("outbound_dashboard")

DEFAULT_BASE_URL = "https://dedi.emailbison.com"
REQUEST_TIMEOUT = 30
MAX_PAGE_PAGINATION_PAGES = 1000  # EmailBison's hard limit for ?page=N
WORKERS = 8
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4

# Custom variable names (lower-cased, spaces/dashes as underscores) we treat as
# industry / company size. EmailBison has no native field for either.
INDUSTRY_KEYS = ("industry", "sector", "vertical", "company_industry")
COMPANY_SIZE_KEYS = (
    "company_size", "employees", "employee_count", "headcount", "num_employees",
    "number_of_employees", "company_headcount", "size", "employee_range",
)


def api_request(method: str, path: str, api_key: str, base_url: str, **kwargs) -> dict:
    """Send one authenticated request to EmailBison and return the decoded JSON.

    Retries rate limits (429) and transient 5xx errors with exponential backoff, honouring
    Retry-After when present. Raises `requests.RequestException` on any other failure.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = requests.request(
            method, f"{base_url}{path}", headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
        )
        if resp.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
            resp.raise_for_status()
            return resp.json()
        retry_after = resp.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
        time.sleep(min(delay, 30))
    raise AssertionError("unreachable")


def log_api_error(err: requests.RequestException, what: str) -> None:
    """Log full API error details for the app owner (server logs only, never the page)."""
    resp = getattr(err, "response", None)
    if resp is not None:
        log.warning("EmailBison %s failed: HTTP %s %s", what, resp.status_code, resp.text[:500])
    else:
        log.warning("EmailBison %s failed: %r", what, err)


class EmailBisonClient:
    """Thin client over `api_request` that knows how to paginate fully."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, params=None) -> dict:
        return api_request("GET", path, self.api_key, self.base_url, params=params)

    def post(self, path: str, body: dict) -> dict:
        return api_request("POST", path, self.api_key, self.base_url, json=body)

    def paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Return every row of a paginated index route, with no row cap.

        Reads page 1 to learn `meta.last_page`. Up to 1,000 pages, the rest are fetched in
        parallel; beyond that (where page-based pagination stops) it restarts with cursor
        pagination and walks `meta.next_cursor` to the end. Rows are de-duplicated by `id`,
        since new rows arriving mid-pull can shift page boundaries.
        """
        params = dict(params or {})
        first = self.get(path, {**params, "page": 1})
        rows = list(first.get("data") or [])
        last_page = int((first.get("meta") or {}).get("last_page") or 1)

        if last_page > MAX_PAGE_PAGINATION_PAGES:
            rows = self._paginate_cursor(path, params)
        elif last_page > 1:
            def page(n: int) -> list[dict]:
                return self.get(path, {**params, "page": n}).get("data") or []

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for chunk in pool.map(page, range(2, last_page + 1)):
                    rows.extend(chunk)
        return _dedupe(rows)

    def _paginate_cursor(self, path: str, params: dict) -> list[dict]:
        rows, cursor = [], None
        while True:
            query = {**params, "pagination_type": "cursor"}
            if cursor:
                query["cursor"] = cursor
            payload = self.get(path, query)
            rows.extend(payload.get("data") or [])
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor or not payload.get("data"):
                return rows

    # --- Typed fetchers -------------------------------------------------------

    def campaigns(self) -> list[dict]:
        """All campaigns (GET /api/campaigns)."""
        return self.paginate("/api/campaigns")

    def replies(self) -> list[dict]:
        """Every inbox reply across all campaigns (GET /api/replies?folder=inbox)."""
        return self.paginate("/api/replies", {"folder": "inbox"})

    def scheduled_email(self, scheduled_email_id: int) -> dict:
        """One sent/scheduled email (GET /api/scheduled-emails/{id})."""
        return self.get(f"/api/scheduled-emails/{scheduled_email_id}").get("data") or {}

    def sequence_steps(self, campaign_id: int) -> list[dict]:
        """Steps and variants of a campaign's sequence (v1.1 endpoint)."""
        data = self.get(f"/api/campaigns/v1.1/{campaign_id}/sequence-steps").get("data") or {}
        if isinstance(data, dict):
            return data.get("sequence_steps") or []
        return data

    def step_stats(self, campaign_id: int, start: date, end: date) -> list[dict]:
        """Per-step sent/contacted/interested counts (POST /api/campaigns/{id}/stats)."""
        payload = self.post(
            f"/api/campaigns/{campaign_id}/stats",
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        return (payload.get("data") or {}).get("sequence_step_stats") or []


# --- Data pull -----------------------------------------------------------------


def load_reply_dataset(api_key: str, base_url: str) -> dict[str, pd.DataFrame]:
    """Pull everything Reply Intelligence needs and return three DataFrames.

    - `replies`: one row per inbox reply, joined to its lead's fields (title, company,
      industry, company size, custom variables as `cv_*` columns), its campaign, and the
      sequence step / variant it answered.
    - `campaigns`: id, name, status, leads_contacted.
    - `steps`: one row per sequence step or variant with its label and leads contacted.

    Raises `requests.RequestException` if the campaign or reply lists can't be loaded.
    Step lookups that fail are logged and leave the step as "Unknown".
    """
    client = EmailBisonClient(api_key, base_url)
    campaigns_raw = client.campaigns()
    replies_raw = client.replies()

    campaigns = pd.DataFrame(
        [{
            "campaign_id": c["id"],
            "campaign": c.get("name") or f"Campaign {c['id']}",
            "status": c.get("status"),
            "leads_contacted": _to_int(c.get("total_leads_contacted")),
            "created_at": c.get("created_at"),
        } for c in campaigns_raw],
        columns=["campaign_id", "campaign", "status", "leads_contacted", "created_at"],
    )

    step_of_email = _resolve_steps(client, replies_raw)
    campaign_ids = sorted({int(c) for c in campaigns["campaign_id"]})
    steps = _load_steps(client, campaigns, campaign_ids)

    replies = pd.DataFrame([_flatten_reply(r) for r in replies_raw])
    if replies.empty:
        return {"replies": replies, "campaigns": campaigns, "steps": steps}

    replies["sequence_step_id"] = replies["scheduled_email_id"].map(step_of_email)
    names = dict(zip(campaigns["campaign_id"], campaigns["campaign"]))
    replies["campaign"] = replies["campaign_id"].map(names).fillna("No campaign (untracked)")
    labels = dict(zip(steps["sequence_step_id"], steps["step_label"])) if not steps.empty else {}
    replies["step"] = replies["sequence_step_id"].map(labels).fillna("Unknown step")
    replies["date_received"] = pd.to_datetime(replies["date_received"], utc=True, errors="coerce")
    return {"replies": replies, "campaigns": campaigns, "steps": steps}


def _flatten_reply(r: dict) -> dict:
    lead = r.get("lead") or {}
    custom = {
        _norm(cv.get("name")): cv.get("value")
        for cv in (lead.get("custom_variables") or []) if cv.get("name")
    }
    name = " ".join(filter(None, [lead.get("first_name"), lead.get("last_name")]))
    row = {
        "reply_id": r.get("id"),
        "lead_id": r.get("lead_id") or lead.get("id"),
        "campaign_id": r.get("campaign_id"),
        "scheduled_email_id": r.get("scheduled_email_id"),
        "date_received": r.get("date_received"),
        "subject": r.get("subject") or "",
        "body": r.get("text_body") or "",
        "esp_interested": bool(r.get("interested")),
        "automated_reply": bool(r.get("automated_reply")),
        "name": name or r.get("from_name") or "",
        "email": lead.get("email") or r.get("from_email_address") or "",
        "title": lead.get("title") or "",
        "company": lead.get("company") or "",
        "industry": _first(custom, INDUSTRY_KEYS),
        "company_size": _first(custom, COMPANY_SIZE_KEYS),
    }
    row.update({f"cv_{k}": v for k, v in custom.items()})
    return row


def _resolve_steps(client: EmailBisonClient, replies_raw: list[dict]) -> dict[int, int]:
    """Map scheduled_email_id -> sequence_step_id, one lookup per distinct email."""
    ids = sorted({r["scheduled_email_id"] for r in replies_raw if r.get("scheduled_email_id")})

    def one(sid: int):
        try:
            return sid, client.scheduled_email(sid).get("sequence_step_id")
        except requests.RequestException as err:
            log_api_error(err, f"GET /api/scheduled-emails/{sid}")
            return sid, None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return {sid: step for sid, step in pool.map(one, ids) if step is not None}


def _load_steps(client: EmailBisonClient, campaigns: pd.DataFrame, ids: list[int]) -> pd.DataFrame:
    """Sequence steps for every campaign, labelled "Step N" / "Step N · Variant B",
    with leads contacted and interested per step from the campaign stats endpoint."""
    created = dict(zip(campaigns["campaign_id"], campaigns["created_at"]))
    today = date.today()

    def one(cid: int) -> list[dict]:
        try:
            steps = client.sequence_steps(cid)
        except requests.RequestException as err:
            log_api_error(err, f"GET /api/campaigns/v1.1/{cid}/sequence-steps")
            return []
        start = pd.to_datetime(created.get(cid), utc=True, errors="coerce")
        start = start.date() if pd.notna(start) else date(2020, 1, 1)
        try:
            stats = {s.get("sequence_step_id"): s for s in client.step_stats(cid, start, today)}
        except requests.RequestException as err:
            log_api_error(err, f"POST /api/campaigns/{cid}/stats")
            stats = {}
        return _label_steps(cid, steps, stats)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = [row for chunk in pool.map(one, ids) for row in chunk]
    return pd.DataFrame(rows, columns=[
        "sequence_step_id", "campaign_id", "step_number", "variant", "step_label",
        "email_subject", "step_leads_contacted", "step_interested",
    ])


def _label_steps(campaign_id: int, steps: list[dict], stats: dict) -> list[dict]:
    """Number the base steps by `order`, and letter their variants A, B, C… by id."""
    by_id = {s["id"]: s for s in steps if s.get("id") is not None}

    def parent(s: dict) -> int:
        return s.get("variant_from_step_id") or s.get("variant_from_step") or s["id"]

    bases = sorted(
        {parent(s) for s in by_id.values()},
        key=lambda i: (_to_int((by_id.get(i) or {}).get("order")), i),
    )
    number = {base: n for n, base in enumerate(bases, start=1)}
    family: dict[int, list[int]] = {}
    for s in sorted(by_id.values(), key=lambda s: s["id"]):
        family.setdefault(parent(s), []).append(s["id"])

    rows = []
    for sid, s in by_id.items():
        members = family[parent(s)]
        letter = chr(ord("A") + members.index(sid)) if len(members) > 1 else ""
        n = number.get(parent(s), 0)
        st_ = stats.get(sid) or {}
        rows.append({
            "sequence_step_id": sid,
            "campaign_id": campaign_id,
            "step_number": n,
            "variant": letter,
            "step_label": f"Step {n}" + (f" · Variant {letter}" if letter else ""),
            "email_subject": s.get("email_subject") or "",
            "step_leads_contacted": _to_int(st_.get("leads_contacted")) if st_ else None,
            "step_interested": _to_int(st_.get("interested")) if st_ else None,
        })
    return rows


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        key = row.get("id")
        if key is None or key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _norm(name) -> str:
    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _first(values: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        if values.get(key) not in (None, ""):
            return str(values[key]).strip()
    return ""


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
