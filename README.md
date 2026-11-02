# outbound-dashboard

Streamlit dashboard with two pages, linked at the top of each:

- **Outbound Performance** (`app.py`): live campaign stats from the EmailBison API
- **Reply Intelligence** (`pages/reply_intelligence.py`): every reply classified by Claude, and
  what's working (see [Reply Intelligence](#reply-intelligence) below)

Outbound Performance shows:

- KPI cards (emails sent, reply rate, positive reply rate, interested leads) with week-over-week change
- Daily trend of emails sent, replies and interested leads
- Reply rate by campaign
- Campaign table with health flags and suggested fixes, downloadable as CSV
- Recent interested leads
- Campaign filter and a Quick range dropdown (Last 7/14/30/90 days, All time, or Custom start and end dates)

Styled with the Orcus GTM brand (light theme, Inter, purple `#875AF2` accent) via
`.streamlit/config.toml` and the shared CSS in `outbound/theme.py`. Streamlit's toolbar, menu, header and footer are hidden.

API errors are never shown on the page: viewers see a short "try again" message (or the section
is hidden), and the full error goes to the app's server logs.

## Setup

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then add your API key
streamlit run app.py
```

`EMAILBISON_API_KEY` comes from Settings → Developer API in EmailBison.
`EMAILBISON_BASE_URL` is the URL of your EmailBison instance (defaults to
`https://dedi.emailbison.com`). `ANTHROPIC_API_KEY` (from console.anthropic.com → API keys)
turns on Reply Intelligence; `ANTHROPIC_MODEL` optionally overrides the Claude model
(default `claude-sonnet-5-5`, the latest Sonnet). `.streamlit/secrets.toml` is gitignored.

## Code layout

- `app.py`: Outbound Performance page
- `pages/reply_intelligence.py`: Reply Intelligence page
- `outbound/theme.py`: brand palette, CSS, page header and chart styling shared by both pages
- `outbound/emailbison.py`: EmailBison API client (auth, retries, full pagination) and the reply data pull
- `outbound/classifier.py`: Claude classification, objection responses, AI summary and the on-disk cache
- `outbound/analysis.py`: title seniority buckets, joins and aggregations for Reply Intelligence
- `outbound/reloader.py`: reloads any `outbound` module whose file changed since it was loaded, so
  a deploy never runs new page code against old modules still in memory

## Metric definitions

- **Reply rate**: unique replying leads ÷ leads contacted
- **Positive reply rate**: leads marked Interested ÷ leads contacted

- **Bounce rate**: bounced ÷ emails sent
- **Health flag**: bounce rate over 5% or reply rate under 1%
- **Week-over-week**: the 7 days ending on the selected end date vs the 7 days before

Rates are calculated from summed counts across the selected campaigns over the selected date range.
All percentages are shown to two decimals (e.g. 0.03%).

## EmailBison endpoints used

- `GET /api/campaigns`: campaign list
- `POST /api/campaigns/{id}/stats`: per-campaign totals for a date range
- `GET /api/campaign-events/stats`: daily sent / replied / interested (falls back to
  `GET /api/campaigns/{id}/line-area-chart-stats` per campaign)
- `GET /api/replies?status=interested`: interested replies with lead name and company

## Reply Intelligence

How the page works:

1. **Data pull** (cached 15 minutes; **Refresh data** clears it). Every inbox reply comes from
   `GET /api/replies?folder=inbox`, paginated to the end with no cap. Up to EmailBison's
   1,000-page limit, pages are fetched in parallel; past that the client switches to cursor
   pagination. Each reply carries its lead (title, company, custom variables). Industry and
   company size are read from custom variables named like `industry` / `sector` / `vertical`
   and `company_size` / `employees` / `headcount`, since EmailBison has no native fields for
   them. The step and variant a lead replied to come from the reply's `scheduled_email_id` →
   `GET /api/scheduled-emails/{id}` → `sequence_step_id`, labelled using
   `GET /api/campaigns/v1.1/{id}/sequence-steps`.
2. **Classification.** New replies go to Claude in batches of 20, using structured JSON
   output. Each reply is classified as one of `interested`, `not_now`, `wrong_person`,
   `objection`, `unsubscribe`, `out_of_office` or `other`, with a confidence, a one-line
   reason, any referral name and any follow-up date. Follow-up dates are worked out from the
   day the reply arrived, so "reach out in Q1" in a June reply means the next January.
   Results are saved after each batch to `.cache/reply_classifications.json`, keyed by reply
   ID, so a reply is never classified twice. Each result records a version (a hash of the
   prompt, schema and model), and changing any of these (or setting a different
   `ANTHROPIC_MODEL`) reclassifies cached replies once on the next load. Old results stay
   on screen until their replacements arrive. If you change how replies are prepared
   without touching the prompt, bump `INPUT_REVISION` in `outbound/classifier.py`. API errors are logged and the affected replies are retried on the next refresh,
   so the page never crashes. On Streamlit Community Cloud this file lasts until the app
   reboots or redeploys, and after that replies are classified again once.
3. **Analysis.**
   - KPI cards: total replies, positive reply rate (leads with an `interested` reply ÷ leads
     contacted across all campaigns), the most common category, and referrals found.
   - A category bar chart.
   - "What's working" tables by campaign, sequence step, step variant, title seniority
     (C-level / VP / Director / Manager / IC), industry and company size. Groups with fewer
     than 5 replies are hidden, and rows are sorted best first. Campaign and step rows use the
     true positive reply rate, from EmailBison's leads-contacted counts. EmailBison doesn't
     report leads contacted per title or industry, so those rows are ranked by positive share
     of replies (interested ÷ leads who replied).
   - Three AI takeaways at the top. They are generated from the aggregated table only; raw
     replies are never sent for the summary.
4. **Action queue.** Lists `not_now` replies with their follow-up date (soonest first),
   `wrong_person` replies with the referral name, and `objection` replies with a two-sentence
   response drafted by Claude (also cached by reply ID). It can be downloaded as CSV.

With no replies, or fewer than 10 classified replies, the page shows a friendly empty state
instead of the summary and tables.

Cost: each reply is classified once, at about 20 replies per Claude call, and objection
drafts and the summary add a few calls per refresh.
