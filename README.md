# outbound-dashboard

Streamlit dashboard showing live campaign stats from the EmailBison API: KPI cards
(emails sent, reply rate, positive reply rate, interested leads), reply rate by
campaign, a campaign filter and a date range.

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

Rates are calculated from summed counts across the selected campaigns over the selected date range.
