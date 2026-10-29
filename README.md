# outbound-dashboard

Streamlit dashboard showing live campaign stats from the EmailBison API:

- KPI cards (emails sent, reply rate, positive reply rate, interested leads) with week-over-week change
- Daily trend of emails sent, replies and interested leads
- Reply rate by campaign
- Campaign table with health flags and suggested fixes, downloadable as CSV
- Recent interested leads
- Campaign filter and date range

Styled with the Orcus GTM brand theme (`.streamlit/config.toml`).

## Setup

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then add your API key
streamlit run app.py
```

`EMAILBISON_API_KEY` comes from Settings → Developer API in EmailBison.
`EMAILBISON_BASE_URL` is the URL of your EmailBison instance (defaults to
`https://dedi.emailbison.com`). `.streamlit/secrets.toml` is gitignored.

## Metric definitions

- **Reply rate**: unique replying leads ÷ leads contacted
- **Positive reply rate**: leads marked Interested ÷ leads contacted

- **Bounce rate**: bounced ÷ emails sent
- **Health flag**: bounce rate over 5% or reply rate under 1%
- **Week-over-week**: the 7 days ending on the selected end date vs the 7 days before

Rates are calculated from summed counts across the selected campaigns over the selected date range.

## EmailBison endpoints used

- `GET /api/campaigns`: campaign list
- `POST /api/campaigns/{id}/stats`: per-campaign totals for a date range
- `GET /api/campaign-events/stats`: daily sent / replied / interested
- `GET /api/replies?status=interested`: interested replies with lead name and company
