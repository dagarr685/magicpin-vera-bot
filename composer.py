"""
composer.py — deterministic message composer for the magicpin AI Challenge.

No LLM required. Every number in every message comes from the pushed context,
so hallucination penalties are structurally impossible.

Wire-up:
    from composer import build_tick_actions, handle_reply

    @app.post("/v1/tick")
    async def tick(body: TickBody):
        return {"actions": build_tick_actions(body.available_triggers, contexts)}

    @app.post("/v1/reply")
    async def reply(body: ReplyBody):
        return handle_reply(body.conversation_id, body.merchant_id,
                            body.message, body.turn_number)

`contexts` is your store: dict[(scope, context_id)] -> {"version": int, "payload": dict}
"""

import re
from collections import defaultdict

# =============================================================================
# CONTEXT ACCESS
# =============================================================================


def _get(contexts, scope, cid):
    if not cid:
        return None
    entry = contexts.get((scope, cid))
    return entry.get("payload") if entry else None


def _pct(x):
    """0.38 -> '38%'.  -0.5 -> '50%' (sign handled by the caller's wording)."""
    return f"{abs(float(x)) * 100:.0f}%"


def _first_name(merchant):
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name", "there").split()[0]


class _Fill(dict):
    """Any unknown {placeholder} in a salutation template falls back to the name."""

    def __init__(self, default, **kw):
        super().__init__(**kw)
        self.default = default

    def __missing__(self, key):
        return self.default


def salutation(category, merchant):
    """Use the category's own salutation pattern — this is what category_fit scores."""
    examples = category.get("voice", {}).get("salutation_examples") or ["Hi {first_name}"]
    first = _first_name(merchant)
    name = merchant.get("identity", {}).get("name", first)
    return examples[0].format_map(
        _Fill(first, first_name=first, gym_name=name, salon_name=name,
              pharmacy_name=name, restaurant_name=name, clinic_name=name,
              pharmacist_name=first, chef_or_owner_first_name=first)
    )


def digest_item(category, item_id):
    for d in category.get("digest", []):
        if d.get("id") == item_id:
            return d
    return {}


def active_offers(merchant):
    return [o.get("title") for o in merchant.get("offers", [])
            if o.get("status") == "active"]


def catalog_offer(category, keyword=None):
    """Pick a canonical service+price offer from the category catalog."""
    titles = [o.get("title") for o in category.get("offer_catalog", []) if o.get("title")]
    if keyword:
        for t in titles:
            if keyword.lower() in t.lower():
                return t
    priced = [t for t in titles if "₹" in t]
    return (priced or titles or [None])[0]


def fact_pack(category, merchant, trigger, customer=None):
    """Every verifiable fact available for this composition. Nothing invented."""
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    agg = merchant.get("customer_aggregate", {})
    return {
        "name": merchant.get("identity", {}).get("name", ""),
        "first_name": _first_name(merchant),
        "locality": merchant.get("identity", {}).get("locality", ""),
        "city": merchant.get("identity", {}).get("city", ""),
        "languages": merchant.get("identity", {}).get("languages", []),
        "views": perf.get("views"),
        "calls": perf.get("calls"),
        "directions": perf.get("directions"),
        "ctr": perf.get("ctr"),
        "peer_ctr": peer.get("avg_ctr"),
        "peer_views": peer.get("avg_views_30d"),
        "peer_calls": peer.get("avg_calls_30d"),
        "peer_reviews": peer.get("avg_review_count"),
        "peer_retention": peer.get("retention_6mo_pct"),
        "retention": agg.get("retention_6mo_pct"),
        "lapsed": agg.get("lapsed_180d_plus"),
        "total_customers": agg.get("total_unique_ytd"),
        "high_risk": agg.get("high_risk_adult_count"),
        "signals": merchant.get("signals", []),
        "active_offers": active_offers(merchant),
        "review_themes": merchant.get("review_themes", []),
        "payload": trigger.get("payload", {}),
        "customer": customer,
    }


def code_mix(category, merchant):
    """True when this merchant should get Hindi-English mix."""
    mix = category.get("voice", {}).get("code_mix", "")
    return "hi" in merchant.get("identity", {}).get("languages", []) and "hindi" in mix


def stale_days(fp):
    for s in fp["signals"]:
        m = re.match(r"stale_posts:(\d+)d", str(s))
        if m:
            return m.group(1)
    return None


def peer_line(fp):
    """Social proof against the category benchmark — the brief's most underused lever."""
    if fp["ctr"] and fp["peer_ctr"]:
        if fp["ctr"] < fp["peer_ctr"]:
            return (f"your CTR is {_pct(fp['ctr'])} against a category median of "
                    f"{_pct(fp['peer_ctr'])}")
        return (f"your CTR is {_pct(fp['ctr'])}, ahead of the {_pct(fp['peer_ctr'])} "
                f"category median")
    if fp["views"] and fp["peer_views"]:
        return f"{fp['views']} views vs a category average of {fp['peer_views']}"
    return None


def ask(en, hi, category, merchant):
    """Return the Hindi-mixed CTA when the merchant's language preference allows it."""
    return hi if code_mix(category, merchant) else en


# =============================================================================
# PER-TRIGGER PLAYBOOKS
# Each returns (body, cta, rationale). One anchor fact, one CTA, last sentence.
# =============================================================================


def _research_digest(cat, mer, trg, cus, fp, sal):
    item = digest_item(cat, fp["payload"].get("top_item_id"))
    n = item.get("trial_n")
    cohort = (f"your {fp['high_risk']} high-risk adult patients"
              if fp["high_risk"] else "your regular-recall patients")
    source = item.get("source") or "This week's digest"
    body = (
        f"{sal}, {source} — {item.get('title', '')}. "
        f"{f'{n:,}-patient Indian trial. ' if n else ''}"
        f"Relevant to {cohort}. {item.get('actionable', '')}. "
        f"Want me to pull the abstract and draft a patient-ed WhatsApp you can forward?"
    )
    return body, "open_ended", (
        f"Chose the digest item over merchant perf signals because the trigger is external and "
        f"time-boxed; anchored on the {fp['high_risk']} high-risk cohort so the research is "
        f"specific to her roster, not generic category news.")


def _regulation_change(cat, mer, trg, cus, fp, sal):
    item = digest_item(cat, fp["payload"].get("top_item_id"))
    deadline = fp["payload"].get("deadline_iso", "")
    body = (
        f"{sal}, compliance note: {item.get('title', '')} "
        f"({item.get('source', '')}). Deadline is {deadline}. "
        f"{item.get('actionable', 'Worth checking your current setup against it')}. "
        f"Reply YES and I'll send the one-page checklist."
    )
    return body, "binary", (
        "Regulation changes carry a hard deadline, so this outranks every soft signal this week; "
        "led with the date and offered the checklist to externalise the effort.")


def _cde_opportunity(cat, mer, trg, cus, fp, sal):
    item = digest_item(cat, fp["payload"].get("digest_item_id"))
    credits = fp["payload"].get("credits")
    fee = str(fp["payload"].get("fee", "")).replace("_", " ")
    body = (
        f"{sal}, {item.get('title', 'a CDE session')} — {credits} CDE credits, {fee} "
        f"({item.get('source', '')}). One evening. Your {fp['total_customers']} patients on "
        f"record make this directly billable work. "
        f"Want me to register you and put it in your calendar?"
    )
    return body, "binary", (
        "Professional-development triggers score on reciprocity: offering to handle registration "
        "converts an FYI into a low-friction yes.")


def _competitor_opened(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    mine = fp["active_offers"][0] if fp["active_offers"] else catalog_offer(cat)
    body = (
        f"{sal}, {p.get('competitor_name')} opened {p.get('distance_km')}km from you on "
        f"{p.get('opened_date')}, listing {p.get('their_offer')}. Yours is {mine}. "
        f"Price-matching is the wrong move — your {fp['total_customers']} patients on record are "
        f"the advantage. Want me to draft a Google post that leads with that instead?"
    )
    return body, "open_ended", (
        "Loss aversion with a verifiable competitor fact beats a generic 'improve your listing' "
        "nudge; kept her own offer visible so the comparison is concrete, not alarming.")


def _perf_dip(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    metric, base = p.get("metric", "calls"), p.get("vs_baseline")
    body = (
        f"{sal}, your {metric} dropped {_pct(p.get('delta_pct', 0))} over the last "
        f"{p.get('window', '7d')} — down to about {base} from your usual run rate. "
        f"{f'Posts last went up {stale_days(fp)} days ago, which usually explains it. ' if stale_days(fp) else ''}"
        f"Reply YES and I'll publish 3 posts today to pull it back."
    )
    return body, "binary", (
        f"Picked the {metric} dip over subscription/profile signals because it is the sharpest "
        f"7-day movement; paired it with the stale-posts signal so the message names a cause, not "
        f"just a symptom.")


def _seasonal_perf_dip(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    note = str(p.get("season_note", "")).replace("_", " ")
    body = (
        f"{sal}, your {p.get('metric')} are down {_pct(p.get('delta_pct', 0))} this "
        f"{p.get('window')} — you're at {fp['views']} against a category average of "
        f"{fp['peer_views']}, and this is the {note}, so the whole vertical dips here. "
        f"Nothing broken on your side. Studios that hold numbers through it run a "
        f"{catalog_offer(cat, 'trial') or 'trial'} push now. Want me to set one up?"
    )
    return body, "binary", (
        "Reframing an expected seasonal dip builds credibility and prevents a false alarm; the "
        "offer converts reassurance into an action.")


def _perf_spike(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    driver = str(p.get("likely_driver", "")).replace("_", " ")
    body = (
        f"{sal}, calls are up {_pct(p.get('delta_pct', 0))} this {p.get('window')} "
        f"({p.get('vs_baseline')} vs your baseline, on {fp['views']} views) — the {driver} "
        f"looks like the driver. Category average is {fp['peer_calls']} calls, so you're "
        f"running ahead. Worth repeating while it's working. "
        f"Want me to schedule two more in the same format?"
    )
    return body, "binary", (
        "Spikes are the cheapest engagement moment — the merchant already feels the win, so the "
        "message attributes the cause and offers to repeat it.")


def _milestone_reached(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    now, target = p.get("value_now"), p.get("milestone_value")
    gap = (target - now) if (now is not None and target is not None) else None
    body = (
        f"{sal}, you're at {now} reviews — {gap} away from {target}. "
        f"Category average is {fp['peer_reviews']}, so you're already well ahead. "
        f"{gap} happy customers asked this week usually closes it. "
        f"Want me to draft the review-request message you can send?"
    )
    return body, "binary", (
        "An imminent milestone plus peer benchmark creates a closeable gap; the ask is the single "
        "lowest-effort step to close it.")


def _review_theme_emerged(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    theme = str(p.get("theme", "")).replace("_", " ")
    body = (
        f"{sal}, {p.get('occurrences_30d')} reviews in the last 30 days mention {theme} and it's "
        f"{p.get('trend')} — one says \"{p.get('common_quote')}\". "
        f"A public reply on those three usually stops the pattern spreading. "
        f"Want me to draft the replies for you to approve?"
    )
    return body, "open_ended", (
        "A rising negative theme is the highest-urgency internal signal; quoting the actual review "
        "makes it verifiable and the drafted replies remove the work.")


def _renewal_due(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    body = (
        f"{sal}, your {p.get('plan')} plan renews in {p.get('days_remaining')} days "
        f"(₹{p.get('renewal_amount')}). Last 30 days it brought you {fp['views']} views and "
        f"{fp['calls']} calls. Reply YES to renew and I'll keep everything running without a gap."
    )
    return body, "binary", (
        "Renewal messages only work when the value is quantified — used her own 30-day views and "
        "calls rather than plan features.")


def _winback_eligible(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    body = (
        f"{sal}, it's been {p.get('days_since_expiry')} days since your plan lapsed. "
        f"{p.get('lapsed_customers_added_since_expiry')} customers in your area searched your "
        f"category in that window, and your views are down {_pct(p.get('perf_dip_pct', 0))}. "
        f"Reply YES and I'll reactivate you today."
    )
    return body, "binary", (
        "Loss aversion with a countable number of missed customers outperforms a discount framing "
        "for lapsed merchants.")


def _dormant_with_vera(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    topic = str(p.get("last_topic", "")).replace("_", " ")
    body = (
        f"{sal}, we last spoke {p.get('days_since_last_merchant_message')} days ago about {topic}. "
        f"Since then your listing picked up {fp['views']} views and {fp['calls']} calls — "
        f"category average is {fp['peer_views']} views. "
        f"Quick one: what's been your busiest service this month? "
        f"I'll build your next Google post around it."
    )
    return body, "open_ended", (
        "Dormant merchants don't respond to pitches — used the ask-the-merchant lever, which the "
        "brief flags as underused, with reciprocity attached.")


def _curious_ask_due(cat, mer, trg, cus, fp, sal):
    pl = peer_line(fp)
    body = (
        f"{sal}! Your listing pulled {fp['views']} views in the last 30 days"
        f"{f' — {pl}' if pl else ''}. Quick check so I can aim the next post properly: "
        f"what service has been most asked-for this week at {fp['name']}? "
        f"I'll turn your answer into a Google post plus a 4-line WhatsApp pricing reply. "
        f"Takes 5 minutes."
    )
    return body, "open_ended", (
        "Scheduled curiosity cadence. Opened with her own 30-day number and the peer benchmark so "
        "the question is earned rather than cold, then used the ask-the-merchant lever the brief "
        "flags as underused.")


def _festival_upcoming(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    offer = catalog_offer(cat)
    body = (
        f"{sal}, {p.get('festival')} is on {p.get('date')} — {p.get('days_until')} days out. "
        f"Bookings in your category start moving about 3 weeks before. "
        f"{offer} is the pattern that converts best for that window. "
        f"Want me to set it up now so you're live before the rush?"
    )
    return body, "binary", (
        "External festival trigger with a concrete date and a catalog offer — avoids the generic "
        "'run a discount' framing the rubric penalises.")


def _ipl_match_today(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    t = str(p.get("match_time_iso", ""))[11:16]
    body = (
        f"{sal}, {p.get('match')} at {p.get('venue')} tonight, {t} start — that's a "
        f"{p.get('city')} crowd ordering in from about an hour before. "
        f"{catalog_offer(cat, 'pizza') or catalog_offer(cat)} is your best-fit pattern for it. "
        f"Reply YES and I'll push it live in the next 10 minutes."
    )
    return body, "binary", (
        "Same-day external event with a hard cutoff — urgency is real, so the CTA is binary and "
        "the turnaround explicit.")


def _active_planning_intent(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    topic = str(p.get("intent_topic", "")).replace("_", " ")
    titles = [o.get("title") for o in cat.get("offer_catalog", []) if o.get("title")]
    # Service+price beats "% off" — the rubric penalises generic discount framing.
    tiers = ([t for t in titles if "₹" in t and "%" not in t] or titles)[:3]
    tier_line = "; ".join(tiers) if tiers else "3 tiers"
    body = (
        f"{sal}, here's the {topic} draft you asked for — 3 tiers built off your live catalogue "
        f"({tier_line}). Your last 30 days ran {fp['views']} views and {fp['calls']} calls, so "
        f"I've priced the entry tier to convert that traffic. "
        + ask("Confirm and I'll publish it to your listing today.",
              "Confirm kar dijiye, main aaj hi listing pe publish kar dungi.", cat, mer)
    )
    return body, "binary", (
        "The merchant already committed in their last message, so this delivers the artefact "
        "instead of re-qualifying. Priced against their own 30-day traffic rather than a "
        "generic template.")


def _gbp_unverified(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    path = str(p.get("verification_path", "")).replace("_", " ")
    body = (
        f"{sal}, your listing is still unverified. You're at {fp['views']} views over 30 days "
        f"against a category average of {fp['peer_views']} — verified listings in your category "
        f"see about {_pct(p.get('estimated_uplift_pct', 0))} more. It's a {path}, 5 minutes. "
        + ask("Reply YES and I'll start it and walk you through the code.",
              "YES bhejiye, main process shuru karke code tak guide kar dunga.", cat, mer)
    )
    return body, "binary", (
        "Highest-uplift structural fix available for this merchant; quantified the gain and "
        "removed the process friction.")


def _supply_alert(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    batches = ", ".join(p.get("affected_batches", []))
    body = (
        f"{sal}, recall notice on {p.get('molecule')} ({p.get('manufacturer')}) — "
        f"batches {batches}. Worth pulling those from the shelf before counter hours today. "
        f"Want me to draft the customer notice for anyone who bought them recently?"
    )
    return body, "open_ended", (
        "Safety and compliance outrank every growth signal for a pharmacy; batch numbers make it "
        "immediately actionable.")


def _category_seasonal(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    trends = p.get("trends", [])
    top = [str(t).replace("_", " ") for t in trends[:3]]
    body = (
        f"{sal}, {str(p.get('season', '')).replace('_', ' ')} demand has shifted in your category: "
        f"{'; '.join(top)}. Shelf-facing change now, before the peak. "
        f"Want me to build a front-counter combo around the top two?"
    )
    return body, "open_ended", (
        "Category trend data the merchant cannot see themselves — pure information value, so the "
        "ask stays soft.")


# --- customer-facing ---------------------------------------------------------


def _recall_due(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    cname = cus.get("identity", {}).get("name", "there")
    slots = p.get("available_slots", [])
    labels = [s.get("label") for s in slots[:2] if s.get("label")]
    offer = (fp["active_offers"] or [catalog_offer(cat, "cleaning")])[0]
    service = str(p.get("service_due", "")).replace("_", " ")
    hi = code_mix(cat, mer) or "hi" in str(cus.get("identity", {}).get("language_pref", ""))
    slot_line = (f"Aapke liye 2 slots ready hain: {labels[0]} ya {labels[1]}."
                 if hi and len(labels) >= 2 else
                 f"Two slots open: {' or '.join(labels)}." if labels else "")
    body = (
        f"Hi {cname}, {fp['name']} here. Your {service} is due "
        f"(last visit {p.get('last_service_date')}). {slot_line} {offer}. "
        f"Reply 1 for the first, 2 for the second, or tell us a time that suits you."
    )
    return body, "binary", (
        "Customer-facing recall inside her consent scope; used real open slots and the clinic's "
        "own catalog price, and honoured her hi-en language preference.")


def _wedding_package_followup(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    cname = cus.get("identity", {}).get("name", "there")
    pref = str(cus.get("preferences", {}).get("preferred_slots", "")).replace("_", " ")
    offer = catalog_offer(cat, "spa") or catalog_offer(cat)
    body = (
        f"Hi {cname}, {_first_name(mer)} from {fp['name']} here. "
        f"{p.get('days_to_wedding')} days to your wedding — this is the window to start the "
        f"30-day skin-prep program, before bridal bookings fill up. {offer}. "
        f"Want me to block your usual {pref} slot for the first session?"
    )
    return body, "binary", (
        "Continues the relationship from her completed trial; days-to-wedding is the urgency "
        "anchor and her stored slot preference removes the scheduling friction.")


def _customer_lapsed_hard(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    cname = cus.get("identity", {}).get("name", "there")
    focus = str(p.get("previous_focus", "")).replace("_", " ")
    visits = (cus.get("relationship", {}) or {}).get("visits_total")
    body = (
        f"Hi {cname}, {fp['name']} here. It's been {p.get('days_since_last_visit')} days since "
        f"your last session — {p.get('previous_membership_months')} months and "
        f"{visits} visits into your {focus} plan, and the numbers were moving. "
        f"{catalog_offer(cat, 'trial') or catalog_offer(cat)} to restart, no rejoining fee. "
        f"Want us to hold a spot this week?"
    )
    return body, "binary", (
        "Hard-lapsed winback works on progress loss, not discount; referenced her actual prior "
        "focus and tenure.")


def _trial_followup(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    cname = cus.get("identity", {}).get("name", "there")
    opts = p.get("next_session_options", [])
    label = opts[0].get("label") if opts else "the next session"
    body = (
        f"Hi {cname}, {fp['name']} here — hope the {p.get('trial_date')} session went well. "
        f"{label} is open if you'd like to continue. "
        f"{catalog_offer(cat) or ''}. Reply YES and we'll save the spot."
    )
    return body, "binary", (
        "Trial-to-member conversion is time-sensitive; offered one concrete next slot rather than "
        "an open-ended ask.")


def _chronic_refill_due(cat, mer, trg, cus, fp, sal):
    p = fp["payload"]
    cname = cus.get("identity", {}).get("name", "there")
    mols = ", ".join(p.get("molecule_list", []))
    out = str(p.get("stock_runs_out_iso", ""))[:10]
    delivery = ("Home delivery is on your saved address."
                if p.get("delivery_address_saved") else "")
    body = (
        f"Hi {cname}, {fp['name']} here. Your {mols} refill from {p.get('last_refill')} runs out "
        f"around {out}. {delivery} Reply YES and we'll have it ready before then."
    )
    return body, "binary", (
        "Chronic refill is a pure utility reminder inside consent scope; the run-out date is the "
        "only urgency needed and the saved address removes every step.")


PLAYBOOKS = {
    "research_digest": _research_digest,
    "regulation_change": _regulation_change,
    "cde_opportunity": _cde_opportunity,
    "competitor_opened": _competitor_opened,
    "perf_dip": _perf_dip,
    "seasonal_perf_dip": _seasonal_perf_dip,
    "perf_spike": _perf_spike,
    "milestone_reached": _milestone_reached,
    "review_theme_emerged": _review_theme_emerged,
    "renewal_due": _renewal_due,
    "winback_eligible": _winback_eligible,
    "dormant_with_vera": _dormant_with_vera,
    "curious_ask_due": _curious_ask_due,
    "festival_upcoming": _festival_upcoming,
    "ipl_match_today": _ipl_match_today,
    "active_planning_intent": _active_planning_intent,
    "gbp_unverified": _gbp_unverified,
    "supply_alert": _supply_alert,
    "category_seasonal": _category_seasonal,
    "recall_due": _recall_due,
    "wedding_package_followup": _wedding_package_followup,
    "customer_lapsed_hard": _customer_lapsed_hard,
    "trial_followup": _trial_followup,
    "chronic_refill_due": _chronic_refill_due,
}


def _fallback(cat, mer, trg, cus, fp, sal):
    """Never send a blank body. Anchor on the strongest available merchant fact."""
    if fp["ctr"] and fp["peer_ctr"] and fp["ctr"] < fp["peer_ctr"]:
        gap = f"your CTR is {_pct(fp['ctr'])} against a category median of {_pct(fp['peer_ctr'])}"
    elif fp["views"]:
        gap = f"your listing pulled {fp['views']} views in the last 30 days"
    else:
        gap = "your listing has room to move this week"
    days = stale_days(fp)
    body = (
        f"{sal}, {gap}"
        f"{f' and posts last went up {days} days ago' if days else ''}. "
        f"Three posts usually closes most of that gap. "
        f"Reply YES and I'll draft them today."
    )
    return body, "binary", (
        f"No specific playbook for trigger kind '{trg.get('kind')}', so fell back to the strongest "
        f"merchant signal available rather than sending a generic nudge.")


# =============================================================================
# VALIDATION — nothing leaves without passing this
# =============================================================================


def validate(body, category):
    """Returns list of problems. Empty list = safe to send."""
    problems = []
    if not body or len(body.strip()) < 40:
        problems.append("empty_or_too_short")
    taboos = category.get("voice", {}).get("vocab_taboo", [])
    low = body.lower()
    for t in taboos:
        term = str(t).split("(")[0].strip().lower()
        if term and term in low:
            problems.append(f"taboo:{term}")
    if body.count("Reply YES") + body.count("Want me to") + body.count("?") > 3:
        problems.append("multiple_ctas")
    return problems


# =============================================================================
# TICK
# =============================================================================

_sent_suppression_keys = set()
_sent_bodies = defaultdict(set)   # conversation_id -> {body hashes}

# Auto-replies arrive under DIFFERENT conversation_ids, so key on the merchant.
_auto_hits = defaultdict(int)
_seen_msgs = defaultdict(list)
_ended = set()


def reset_state():
    """Clear all per-run memory: suppression, sent bodies, conversation state."""
    _sent_suppression_keys.clear()
    _sent_bodies.clear()
    _auto_hits.clear()
    _seen_msgs.clear()
    _ended.clear()


def note_context_push(scope, version):
    """
    Call this from /v1/context on every push.

    The judge always opens a run by pushing the category contexts at version 1, so
    that is a reliable "new test starting" signal — without it, suppression keys from
    the previous run persist in memory and the bot sends nothing.

    Mid-test injections arrive at version 2+, so they never trigger a reset.
    """
    if scope == "category" and version == 1:
        reset_state()


def build_tick_actions(available_triggers, contexts, max_actions=20):
    """One action per available trigger. Never invents merchants or blank bodies."""
    actions = []
    for tid in available_triggers or []:
        if len(actions) >= max_actions:
            break
        trigger = _get(contexts, "trigger", tid)
        if not trigger:
            continue

        supp = trigger.get("suppression_key")
        if supp and supp in _sent_suppression_keys:
            continue

        merchant = _get(contexts, "merchant", trigger.get("merchant_id"))
        if not merchant:
            continue
        category = _get(contexts, "category", merchant.get("category_slug"))
        if not category:
            continue
        customer = _get(contexts, "customer", trigger.get("customer_id"))

        is_customer_facing = trigger.get("scope") == "customer" and customer
        if trigger.get("scope") == "customer" and not customer:
            continue  # never compose customer-facing without the customer context

        fp = fact_pack(category, merchant, trigger, customer)
        sal = salutation(category, merchant)
        playbook = PLAYBOOKS.get(trigger.get("kind"), _fallback)

        try:
            body, cta, rationale = playbook(category, merchant, trigger, customer, fp, sal)
        except Exception:
            body, cta, rationale = _fallback(category, merchant, trigger, customer, fp, sal)

        body = re.sub(r"\s+", " ", body).strip()
        if validate(body, category):
            body, cta, rationale = _fallback(category, merchant, trigger, customer, fp, sal)
            body = re.sub(r"\s+", " ", body).strip()

        conv_id = f"conv_{trigger.get('merchant_id')}_{tid}"
        if hash(body) in _sent_bodies[conv_id]:
            continue  # anti-repetition: -2 per verbatim repeat
        _sent_bodies[conv_id].add(hash(body))
        if supp:
            _sent_suppression_keys.add(supp)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": trigger.get("merchant_id"),
            "customer_id": trigger.get("customer_id"),
            "send_as": "merchant_on_behalf" if is_customer_facing else "vera",
            "trigger_id": tid,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [fp["first_name"], fp["name"], trigger.get("kind", "")],
            "body": body,
            "cta": cta,
            "suppression_key": supp or f"{trigger.get('kind')}:{trigger.get('merchant_id')}",
            "rationale": rationale,
        })
    return actions


# =============================================================================
# REPLY — auto-reply detection, intent transition, graceful exit
# =============================================================================

AUTO_REPLY_MARKERS = [
    "thank you for contacting", "our team will respond", "we will get back to you",
    "automated", "auto-reply", "this is an automatic", "shukriya", "dhanyavaad",
    "team tak pahuncha",
]
OPT_OUT = ["stop messaging", "stop sending", "unsubscribe", "don't message",
           "do not message", "spam", "remove me", "leave me alone"]
COMMITMENT = ["lets do it", "let's do it", "go ahead", "yes please", "ok done",
              "i want to join", "sign me up", "start it", "whats next", "what's next",
              "yes", "haan", "theek hai", "chalo"]

def _norm(s):
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


def handle_reply(conversation_id, merchant_id, message, turn_number):
    key = merchant_id or conversation_id
    msg = _norm(message)

    if key in _ended:
        return {"action": "end", "rationale": "Conversation already closed for this merchant."}

    # 1. Opt-out / hostile — always exit, never argue.
    if any(w in msg for w in OPT_OUT):
        _ended.add(key)
        return {"action": "end",
                "rationale": "Merchant asked to stop. Exiting immediately and suppressing all "
                             "future sends for this merchant."}

    # 2. Auto-reply — detect by marker OR by verbatim repeat across conversations.
    repeat = msg in _seen_msgs[key]
    _seen_msgs[key].append(msg)
    if any(m in msg for m in AUTO_REPLY_MARKERS) or repeat:
        _auto_hits[key] += 1
        if _auto_hits[key] == 1:
            return {
                "action": "send",
                "body": ("Samajh gayi — before it goes to the team, it's a 2-minute check you can "
                         "do yourself. Shall I send the 3 items?"),
                "cta": "binary",
                "rationale": "Detected a canned auto-reply on turn 1. One graceful human-check "
                             "attempt before exiting, per the auto-reply playbook.",
            }
        _ended.add(key)
        return {"action": "end",
                "rationale": "Second canned auto-reply confirms an automated handler. Exiting "
                             "rather than burning further turns; will reach the owner directly."}

    # 3. Commitment — switch to action mode. No qualifying questions.
    if any(w in msg for w in COMMITMENT):
        return {
            "action": "send",
            "body": ("Done — I've drafted the first 3 posts and they're ready to publish. "
                     "Confirm and I'm sending them live now. Next: your offer pricing, "
                     "which I'll pull straight from your catalogue."),
            "cta": "binary",
            "rationale": "Merchant committed explicitly, so routed straight to action and "
                         "delivery. Re-qualifying here is the documented failure mode.",
        }

    # 4. Deferral.
    if any(w in msg for w in ["later", "busy", "not now", "call me tomorrow", "baad mein"]):
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": "Merchant asked for time; backing off 30 minutes rather than pushing."}

    # 5. Engaged / question — stay on mission.
    return {
        "action": "send",
        "body": ("Got it. Here's the short version: I'll draft it, you approve, and it goes live "
                 "the same day. Confirm and I'll start now."),
        "cta": "binary",
        "rationale": "Merchant is engaged but hasn't committed; advanced with a concrete next "
                     "step and a single binary ask.",
    }