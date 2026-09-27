# Vera Message Engine — magicpin AI Challenge

**Team**: Shourya Kumawat
**Bot URL**: `https://<your-deployed-url>`
**Contact**: <shouryakumawat_23en061@dtu.ac.in>

---

## Approach

A **deterministic composer** rather than a single LLM prompt. Every message is
assembled in four stages:

1. **Route** — the trigger's `kind` selects one of 24 playbooks (`research_digest`,
   `perf_dip`, `recall_due`, `competitor_opened`, …). Each playbook knows which
   single signal should drive that message.
2. **Fact pack** — a dict of verified values read straight from the pushed contexts:
   the merchant's own performance numbers, the category peer benchmark, their active
   offer catalogue, the digest item named by the trigger payload, review themes.
3. **Compose** — the playbook writes the body from the fact pack only. Salutation and
   tone come from the category's own `voice.salutation_examples` and `voice.tone`;
   Hindi-English code-mix fires when `identity.languages` contains `hi` and the
   category's `voice.code_mix` allows it.
4. **Validate** — the body is rejected and re-composed if it is empty, contains a
   `voice.vocab_taboo` term, or stacks more than one CTA.

Because no text is generated, **no number can appear that is not already in the
pushed context**. Hallucination penalties are structurally impossible rather than
prompt-suppressed.

## Key decisions

**Grounding over fluency.** An LLM writes better sentences, but a template that cites
`2,100-patient trial` and `JIDA Oct 2026, p.14` because it read them out of the
category digest will never invent a citation. Given the rubric's -2 fabrication
penalty, I took the trade.

**Peer benchmarks everywhere.** The brief notes social proof barely fires in
production Vera. Messages contrast the merchant's own figure against
`peer_stats` — "720 views over 30 days against a category average of 1,400" — which
serves specificity and social proof at once.

**Restraint.** `/v1/tick` returns `{"actions": []}` when every available trigger is
suppressed. Suppression keys are tracked for the life of the run and reset only on a
version-1 category push, which is the judge's own "new test" signal.

**Conversation handling.** Auto-reply detection keys on `(merchant_id, normalized
message)` rather than `conversation_id`, because canned replies arrive under fresh
conversation ids. The bot makes one graceful human-check attempt, then exits.
Explicit commitment ("ok let's do it") routes straight to delivery — no further
qualifying question. Opt-out language ends the conversation and suppresses that
merchant permanently.

## Tradeoffs

- **Coverage vs. depth.** 24 playbooks cover every trigger kind in the seed set. An
  unrecognised kind falls back to the merchant's strongest available signal rather
  than a generic nudge — safe, but less sharp than a purpose-built playbook.
- **No LLM means less prose variety.** Two merchants hitting the same trigger kind get
  structurally similar messages with different numbers. A rewrite pass over the
  composed body (phrasing only, numbers frozen to the fact pack) was the planned next
  layer.
- **In-memory state.** Fine for a 60-minute window, as the brief allows, but the
  process must not restart mid-test.

## What extra context would have helped most

1. **Merchant reply history with outcomes** — `conversation_history` carries an
   `engagement` tag, but not what the merchant actually did afterwards. Knowing which
   past messages converted would let the composer rank levers per merchant instead of
   per category.
2. **Open slot / inventory availability** — customer-facing recall and booking
   messages are strongest when they name two real slots. Only some triggers carry
   them; a merchant-level availability feed would make every booking message concrete.
3. **Time of day and merchant working hours.** "Why now" is half the rubric's decision
   quality, and a restaurant nudge at 2pm lunch rush is a different message from the
   same nudge at 11am.

## Running locally

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Regenerate `submission.jsonl`:

```bash
python3 dataset/generate_dataset.py --seed-dir dataset --out expanded
python3 make_submission.py
```

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI server, 5 endpoints |
| `composer.py` | Playbooks, fact pack, validation, reply routing |
| `make_submission.py` | Renders the 30 canonical pairs |
| `submission.jsonl` | 30 composed messages |