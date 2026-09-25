# Vera: Grounded Merchant Assistant

Team: Ansh Dwivedi (solo)
Contact: dwivediansh0@gmail.com
Source code: https://github.com/dwivediansh7/vera-bot

## 1. What Vera Does

Vera is an HTTP bot for the magicpin AI challenge. It decides whether a merchant (or a merchant's customer) should get a message right now, picks the single most useful signal, writes a short WhatsApp style message using only facts it can prove from the given context, and then handles the conversation that follows.

## 2. How It Works

| Step | Module | What it does |
|---|---|---|
| Select | `app/decide.py` | Validates the trigger, detects contradictions, checks consent and suppression, scores urgency |
| Facts | `app/evidence.py` | Builds an evidence ledger where every fact has a source and the exact numbers it allows |
| Write | `app/playbooks.py` | One playbook per trigger type: one hook, one call to action, history aware |
| Validate | `app/validator.py` | Every number, price and percentage must trace to the ledger; no URLs, jargon, taboo words or repeats |
| Send | `app/main.py` | Sends, tries another wording, or stays silent with a logged reason |

Replies go through `app/replies.py`: a rule based classifier picks the move, a template writes it, and Gemini may reword it. The reworded text is validated again and discarded if it changes any fact.

## 3. Key Behaviours

1. **Honest with bad triggers.** When a trigger contradicts the data, it is reframed honestly or skipped with a logged reason. A refill trigger for a dentist is skipped.
2. **Time aware.** Dates are stated as dates. A day count is used only when it matches the current date.
3. **Remembers the conversation.** If a merchant already said yes, Vera delivers instead of asking again, and picks up open requests when relevant.
4. **Handles replies.** Auto replies get one nudge, then a wait, then an exit. "Let's do it" switches straight to action. Price objections get one respectful answer based on the merchant's own numbers. Opt outs end the conversation.
5. **Ranks triggers.** Highest urgency first, then evidence strength; reframed, repeated or expired triggers rank lower. At most one message per merchant per tick and 20 per tick.
6. **Unknown trigger kinds.** Vera writes about the strongest verified gap in the merchant's own data, or stays silent if there is none.

## 4. Example (test pair T25)

**Input:** a `perf_dip` trigger with no details, for a salon whose data shows views up 8% and calls up 2% week on week.

**Vera's message:**
> Sushma, quick look at your numbers: views up 8% and calls up 2% week-on-week, so nothing is dipping right now. The gap worth closing is 22 calls in 30 days vs ~28 for similar salons. 'Haircut @ ₹99' aapke profile par live kar doon? Bas YES reply kijiye.

**Rationale:** Perf dip event with no details; anchored on verified account data instead: honest status (no dip in data) + one real gap. Grounded in: 22 calls in 30 days vs ~28 for similar salons; views up 8% week-on-week; calls up 2% week-on-week. Data check: dip trigger but no metric is down in the current snapshot.

A naive bot would have told Sushma her numbers dropped. Vera checks the data first.

## 5. Model Choice

| Part | Engine | Reason |
|---|---|---|
| Outbound messages | Deterministic templates | No fabrication, millisecond latency, identical output for identical input |
| Reply wording | `gemini-3.5-flash-lite` (Gemini free tier), temperature 0 | Natural phrasing in conversations |
| Fallback | Templates | Used on any error, timeout, rate limit, or changed fact |

## 6. Results

| Check | Result |
|---|---|
| Official simulator, `all` (warmup, auto reply, intent, hostile) | 4 of 4 pass, locally and on the live URL |
| Official simulator, `phase2_short` | 43 to 47 / 50 across runs (the LLM judge varies by about ±3 on identical text) |
| Official simulator, `full_evaluation` (13 LLM scored messages) | 41.8 / 50 average |
| Automated tests (contract, 30 pairs, traps, mutations, fuzz, replies) | 76 pass |
| Live deployment harness (warmup, adaptation, replay, 10 requests per second) | All pass, zero errors |

## 7. Endpoints, Setup and Deployment

Endpoints: `GET /` (this page), `POST /v1/context` (409 on stale version), `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata`, `POST /v1/teardown`.

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
PYTHONUTF8=1 python -m pytest -q
```

Settings are environment variables (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_OUTBOUND`); see `.env.example`. Deployed with Docker on Render (free plan) with a 5 minute keep alive ping to `/v1/healthz`; steps in `DEPLOY.md`. Run exactly one worker, because state is kept in memory.

## 8. Tradeoffs

Templates guarantee grounded facts but have less stylistic range than a free form LLM. More context would help most: a real date anchor for the dataset, appointment slot data for customers, and each merchant's service or order mix.
