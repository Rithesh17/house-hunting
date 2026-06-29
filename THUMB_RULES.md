# THUMB_RULES.md — operating guardrails (DOs and DON'Ts)

Standing must-follow rules for **anyone** working on this project — humans and LLMs
alike. They are principles, not procedures: they say what to do and what never to do,
never *how*. The how-to lives in CLAUDE.md, MANUAL_SOURCES.md, and OUTREACH.md.

**When a rule and a convenient shortcut conflict, the rule wins.** Read these before
any fetch, sync, send, deploy, or other outside-touching action.

## 1. Don't abuse paid services — the money is real
- **Never opt into paid tiers, plans, or compute.** Stay on the free tiers we rely on.
- If an action could cost money and you're not certain it's free, **stop, say so, and
  offer the free or cheaper path first.**
- Prefer tools that cost nothing (a web search, a public lookup) over anything that bills.
- Cap the blast radius of anything that *could* charge: smallest scope first, one cheap
  probe before any batch, and report what was actually spent afterward.

## 2. Don't abuse the free stuff either
- **Be a polite guest on every site and service we touch.** Identify honestly, pace
  like a human, leave delays, don't hammer. Aggressive access gets us rate-limited or
  blocked — which loses us the source entirely.
- **When you automate a browser, drive it like a human, and NEVER inject or execute
  page scripts.** Use real input (mouse, scroll, keystrokes) to *interact*, and read
  the page only to *gather* information. Running scripts in the page is detectable,
  trips bot defenses, and is brittle — it's banned here, no exceptions.
- **Don't re-fetch what we already have.** Recency windows, dedup, and the blocklist
  exist so we only pull genuinely new things. Honor them.
- Free tiers and goodwill are finite. Avoid needless rebuilds, redeploys, and redundant
  work — do the expensive/outward step only when something actually changed.

## 3. Don't store more than you need
- Keep the local database and the cloud **minimal** — store only what the dashboard
  actually shows. Don't add data nothing renders.
- **Keep heavy or raw data out of the cloud:** link to the source instead of copying
  it, and reference images by URL instead of saving the bytes.
- **Purge aggressively.** Rejects, junk, and anything we won't surface should not linger.

## 4. Use your judgement — don't lean on scripts for the hard parts
- Scripts are for **mechanical bulk** (fetch, dedup, sync). The **judgement calls** —
  is this a scam, a shared room, a good fit, a safe neighborhood — are **yours**, made
  by actually looking, not by a keyword rule or a metadata tag.
- **Don't trust a source's own labels or a script's output at face value.** Verify with
  your eyes and independent cross-checks before acting on them.
- **Don't build or babysit a scraper for a target that's bot-walled and yields only a
  tiny list per day — do it by hand.** A brittle scraper breaks silently, disguises its
  failures as something else, and fakes coverage. For a small, awkward, high-judgement
  job that is worse than no script at all.
- **Judge fairly and asymmetrically: only real evidence of a scam may lower trust.**
  "Can't tell" is neutral, and the listing is still surfaced. Never manufacture a scam
  verdict out of amateurism, a missing license, sparse photos, or a low price — honest,
  cheap, amateur posts are exactly what we're hunting for.
- **A room is never a whole unit, whatever the listing's tag claims.** Read the actual
  post, not the label or the source's metadata.
- **Know the edge of your judgement: the deterministic area model owns "is this a safe
  area," and its call is final.** A great unit in an unsafe area is still excluded —
  never let how good a place looks override where it is.
- A missed item is cheap; a confident-but-wrong result is expensive. When unsure, **do
  less and say so.**

## 5. Outreach touches the real world — keep it careful and minimal
Emails go from a real account to real people; treat every send as irreversible.
- **Never contact a listing flagged as a likely scam.**
- **Contact a unit at most once, ever.** A relist is the same unit under a new id —
  don't reach out again.
- **Auto-send only to a genuine 1 bed / 1 bath in an OK-tier area, and cap how many go
  out per run.** Everything else — studios, 2+ bed, lesser areas — is contacted by hand,
  deliberately, never on autopilot.
- **Share no personal information beyond a first name** — never an address, employer,
  exact income, or other PII.
- **When in doubt, don't send.** A missed email is cheap; a wrong or duplicate one isn't.

## 6. Fail safe and be honest
- **Confirm before anything hard to reverse or outward-facing** — sending, deleting,
  publishing, spending. Approval for one thing isn't approval for the next.
- **Report what actually happened** — what ran, what you skipped, what failed and why.
  Never paper over a failure or claim coverage you didn't achieve.
