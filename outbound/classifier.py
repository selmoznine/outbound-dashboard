"""Reply classification, objection responses and the AI summary, using the Claude API.

Each reply is classified into exactly one category:

    interested, not_now, wrong_person, objection, unsubscribe, out_of_office, other

with a confidence (0-1), a one-line reason, the name of anyone the lead refers us to, and a
follow-up date when the lead asks to be contacted later ("reach out in Q1" -> 2027-01-01).

Replies are sent in batches using structured JSON output (`output_config.format`), so the
response always parses. Results are written to a local JSON cache keyed by reply ID after
every batch, so the same reply is never classified twice, and a crash or API outage only
costs the batch in flight. API errors are logged and counted, never raised to the page,
except a bad API key (`ClassifierAuthError`), which the page shows as a setup message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import anthropic

log = logging.getLogger("outbound_dashboard")

DEFAULT_MODEL = "claude-sonnet-5-5"
CATEGORIES = (
    "interested", "not_now", "wrong_person", "objection",
    "unsubscribe", "out_of_office", "other",
)
BATCH_SIZE = 20
WORKERS = 4
MAX_BODY_CHARS = 2000
# Server-side refusal fallback: if the model declines a request, the API re-runs it on
# Anthropic's recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "reply_classifications.json"


class ClassifierAuthError(Exception):
    """The Anthropic API key is missing, invalid, or lacks permission."""


class _BadOutput(Exception):
    """Claude answered, but the answer was truncated, refused, or didn't parse."""


@dataclass
class RunStats:
    """What happened during one classification or drafting run."""
    requested: int = 0
    from_cache: int = 0
    newly_done: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


_CLASSIFY_SYSTEM = """You classify replies to B2B cold outbound emails for a sales team.

Pick exactly one category per reply:
- interested: wants to talk, asks for a call/demo/pricing/more info, or says yes.
- not_now: open to it but the timing is wrong ("circle back next quarter", "busy until March").
- wrong_person: not the right contact, or points to someone else who is.
- objection: pushes back on need, price, competitor, trust, or relevance, without asking to stop.
- unsubscribe: asks to stop emailing, be removed, or is hostile about being contacted.
- out_of_office: automatic away/vacation/parental-leave/left-the-company auto-reply.
- other: anything else (bounces, unclear one-word replies, questions with no buying signal).

Fields:
- confidence: 0 to 1, how sure you are of the category.
- reason: one short line explaining the call, quoting the deciding phrase where possible.
- referral_name: the person they point us to (name, plus email or role if given), else "".
- follow_up_date: if they say when to reach out again, the earliest date that matches it as
  YYYY-MM-DD ("Q1" -> first day of the next Q1, "after the summer" -> September 1, an
  out-of-office return date -> that date), else "". Today is {today}.

Return one result per reply, using the reply_id you were given."""

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reply_id": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "referral_name": {"type": "string"},
                    "follow_up_date": {"type": "string"},
                },
                "required": [
                    "reply_id", "category", "confidence", "reason",
                    "referral_name", "follow_up_date",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_RESPONSE_SYSTEM = """You write replies for a B2B sales rep answering objections to a cold email.
For each objection, draft exactly two sentences: acknowledge their point specifically, then
offer one low-friction next step or reframe. Plain, warm, no hype, no exclamation marks,
no placeholders like [Name]. Return one draft per reply_id you were given."""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reply_id": {"type": "integer"},
                    "response": {"type": "string"},
                },
                "required": ["reply_id", "response"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["drafts"],
    "additionalProperties": False,
}

_SUMMARY_SYSTEM = """You are a GTM analyst reviewing cold outbound performance. You get an
aggregated table of reply outcomes by campaign, sequence step/variant, title seniority and
industry. Write exactly 3 takeaways, each one sentence, action-first ("Double down on X…",
"Cut Y…", "Rewrite Z…"), each citing the numbers behind it. Only use what is in the table;
if the sample is small, say so in the takeaway."""

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"takeaways": {"type": "array", "items": {"type": "string"}}},
    "required": ["takeaways"],
    "additionalProperties": False,
}


class ReplyClassifier:
    """Classifies replies with Claude and remembers every result on disk.

    Args:
        api_key: Anthropic API key.
        model: Claude model ID (defaults to the latest Sonnet).
        cache_path: JSON file holding classifications and drafted responses by reply ID.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, cache_path: Path = DEFAULT_CACHE_PATH):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120.0)
        self.model = model
        self.cache = ClassificationCache(cache_path)

    # --- Classification -----------------------------------------------------

    def classify(
        self, replies: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> tuple[dict[str, dict], RunStats]:
        """Classify replies not already in the cache; return all results by reply ID.

        Each reply dict needs `reply_id`, `body`, and optionally `subject`, `title`, `company`.
        `on_progress(done, total)` is called after each batch of new classifications.
        """
        stats = RunStats(requested=len(replies))
        todo = [r for r in replies if self.cache.classification(r["reply_id"]) is None]
        stats.from_cache = stats.requested - len(todo)
        batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
        lock = threading.Lock()

        def run(batch: list[dict]) -> None:
            results = self._classify_batch(batch, stats)
            self.cache.put_classifications(results)
            with lock:
                stats.newly_done += len(results)
                stats.failed += len(batch) - len(results)

        done = 0
        for batch in _run_parallel(run, batches):  # progress is reported on the caller's thread
            done += len(batch)
            if on_progress:
                on_progress(done, len(todo))
        wanted = {str(r["reply_id"]) for r in replies}
        return {k: v for k, v in self.cache.classifications().items() if k in wanted}, stats

    def _classify_batch(self, batch: list[dict], stats: RunStats) -> dict[str, dict]:
        """Classify one batch; on an unparseable or truncated answer, split and retry."""
        payload = [{
            "reply_id": int(r["reply_id"]),
            "subject": (r.get("subject") or "")[:200],
            "lead_title": r.get("title") or "",
            "lead_company": r.get("company") or "",
            "reply": clean_reply_text(r.get("body") or ""),
        } for r in batch]
        try:
            data = self._call(
                system=_CLASSIFY_SYSTEM.replace("{today}", date.today().isoformat()),
                user="Classify these replies:\n" + json.dumps(payload, ensure_ascii=False),
                schema=_CLASSIFY_SCHEMA,
                max_tokens=8000,
                stats=stats,
                raise_bad_output=True,
            )
        except _BadOutput:
            if len(batch) > 1:  # try halves, so one odd reply can't sink the whole batch
                mid = len(batch) // 2
                return {**self._classify_batch(batch[:mid], stats),
                        **self._classify_batch(batch[mid:], stats)}
            return {}
        if data is None:  # API error, already logged; retried on the next run
            return {}
        wanted = {str(r["reply_id"]) for r in batch}
        out = {}
        for item in data.get("results", []):
            rid = str(item.get("reply_id"))
            if rid in wanted:
                out[rid] = _normalise(item, self.model)
        return out

    # --- Objection responses -----------------------------------------------

    def draft_objection_responses(self, replies: list[dict]) -> tuple[dict[str, str], RunStats]:
        """Draft a 2-sentence answer for each objection reply not already drafted."""
        stats = RunStats(requested=len(replies))
        todo = [r for r in replies if self.cache.response(r["reply_id"]) is None]
        stats.from_cache = stats.requested - len(todo)
        lock = threading.Lock()

        def run(batch: list[dict]) -> None:
            payload = [{
                "reply_id": int(r["reply_id"]),
                "lead_title": r.get("title") or "",
                "lead_company": r.get("company") or "",
                "our_subject": (r.get("subject") or "")[:200],
                "their_reply": clean_reply_text(r.get("body") or ""),
            } for r in batch]
            data = self._call(
                system=_RESPONSE_SYSTEM,
                user="Draft responses to these objections:\n" + json.dumps(payload, ensure_ascii=False),
                schema=_RESPONSE_SCHEMA,
                max_tokens=8000,
                stats=stats,
            )
            drafts = {
                str(d["reply_id"]): d["response"].strip()
                for d in (data or {}).get("drafts", []) if d.get("response")
            }
            self.cache.put_responses(drafts)
            with lock:
                stats.newly_done += len(drafts)
                stats.failed += len(batch) - len(drafts)

        for _ in _run_parallel(run, [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]):
            pass
        wanted = {str(r["reply_id"]) for r in replies}
        return {k: v for k, v in self.cache.responses().items() if k in wanted}, stats

    # --- Summary --------------------------------------------------------------

    def summarize(self, table_csv: str, context: str) -> list[str]:
        """Three action-first takeaways from the aggregated table (never raw replies).

        Returns [] if the call fails.
        """
        data = self._call(
            system=_SUMMARY_SYSTEM,
            user=f"{context}\n\nAggregated table (CSV):\n{table_csv}",
            schema=_SUMMARY_SCHEMA,
            max_tokens=4000,
            stats=RunStats(),
        )
        return [t.strip() for t in (data or {}).get("takeaways", []) if t.strip()][:3]

    # --- Claude call ------------------------------------------------------------

    def _call(
        self, system: str, user: str, schema: dict, max_tokens: int, stats: RunStats,
        raise_bad_output: bool = False,
    ) -> dict | None:
        """One structured-output request. Returns parsed JSON, or None on any failure.

        With `raise_bad_output`, a truncated, refused or unparseable answer raises
        `_BadOutput` instead, so the caller can retry with a smaller batch.
        """
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as err:
            raise ClassifierAuthError(str(err)) from err
        except anthropic.NotFoundError as err:
            _record(stats, f"Model {self.model!r} not found: {err}")
            return None
        except anthropic.RateLimitError as err:
            _record(stats, f"Rate limited by the Anthropic API: {err}")
            return None
        except anthropic.APIStatusError as err:
            _record(stats, f"Anthropic API error {err.status_code}: {err}")
            return None
        except anthropic.APIConnectionError as err:
            _record(stats, f"Could not reach the Anthropic API: {err}")
            return None

        problem = None
        if response.stop_reason in ("refusal", "max_tokens"):
            problem = f"Claude stopped with stop_reason={response.stop_reason}"
        else:
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                problem = "Claude returned JSON that did not parse"
        _record(stats, problem)
        if raise_bad_output:
            raise _BadOutput(problem)
        return None


class ClassificationCache:
    """Thread-safe JSON file of {"classifications": {id: {...}}, "responses": {id: str}}.

    Writes go to a temp file that atomically replaces the cache, so a crash mid-write
    can't corrupt it. Note: on Streamlit Community Cloud the file lives on the app's
    container disk, so it survives reruns and sessions but not a reboot or redeploy.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError) as err:
            log.warning("Ignoring unreadable classification cache %s: %r", self.path, err)
            data = {}
        data.setdefault("classifications", {})
        data.setdefault("responses", {})
        return data

    def classification(self, reply_id) -> dict | None:
        return self._data["classifications"].get(str(reply_id))

    def classifications(self) -> dict[str, dict]:
        return dict(self._data["classifications"])

    def response(self, reply_id) -> str | None:
        return self._data["responses"].get(str(reply_id))

    def responses(self) -> dict[str, str]:
        return dict(self._data["responses"])

    def put_classifications(self, items: dict[str, dict]) -> None:
        if items:
            with self._lock:
                self._data["classifications"].update(items)
                self._save()

    def put_responses(self, items: dict[str, str]) -> None:
        if items:
            with self._lock:
                self._data["responses"].update(items)
                self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("Could not write classification cache %s: %r", self.path, err)


# --- Helpers -----------------------------------------------------------------

_QUOTE_MARKERS = re.compile(
    r"^\s*(On .{0,200}wrote:|-{2,}\s*Original Message|From:\s.+|Sent from my )",
    re.IGNORECASE | re.MULTILINE,
)


def clean_reply_text(text: str) -> str:
    """Drop the quoted thread below a reply and trim it to a sane length."""
    match = _QUOTE_MARKERS.search(text)
    if match and match.start() > 0:
        text = text[:match.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:MAX_BODY_CHARS]


def _normalise(item: dict, model: str) -> dict:
    category = item.get("category")
    try:
        confidence = min(max(float(item.get("confidence", 0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    follow_up = (item.get("follow_up_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", follow_up):
        follow_up = ""
    return {
        "category": category if category in CATEGORIES else "other",
        "confidence": round(confidence, 2),
        "reason": (item.get("reason") or "").strip(),
        "referral_name": (item.get("referral_name") or "").strip(),
        "follow_up_date": follow_up,
        "model": model,
        "classified_on": date.today().isoformat(),
    }


def _record(stats: RunStats, message: str) -> None:
    log.warning("Reply classifier: %s", message)
    if len(stats.errors) < 20:
        stats.errors.append(message)


def _run_parallel(fn: Callable[[list[dict]], None], batches: list[list[dict]]):
    """Run batches on a small thread pool, yielding each batch as it finishes.

    Yields on the caller's thread, so Streamlit progress widgets can be updated safely.
    A ClassifierAuthError from any batch cancels the queued ones and is re-raised.
    """
    if not batches:
        return
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fn, b): b for b in batches}
        try:
            for future in as_completed(futures):
                future.result()
                yield futures[future]
        except ClassifierAuthError:
            for f in futures:
                f.cancel()
            raise
