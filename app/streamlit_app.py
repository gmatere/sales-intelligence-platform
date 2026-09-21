#!/usr/bin/env python3
"""Sales intelligence app — prospect list for a cybersecurity vendor.

Reads a precomputed Parquet and nothing else. No database, no API key, no
inference at request time: every classification, score and finding was decided
in the pipeline. The app filters and explains.

It is built around the three questions a rep actually needs answered before
picking up the phone — why this account, why now, and what do I say — rather
than around the shape of the underlying data.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pandas as pd
import streamlit as st


class Finding(NamedTuple):
    """One observable finding, in the three forms the UI needs.

    `label` and `detail` fill a table cell, where the column header supplies
    context. `phrase` is for prose, where it cannot — "1 host" is meaningful
    under a "Detail" column and meaningless mid-sentence.
    """

    label: str
    detail: str
    evidence: str   # observed | tested | inferred
    phrase: str

DATA = Path(__file__).parent / "data" / "accounts.parquet"

TIER_HELP = {
    "A - call now": "Strong ICP fit and an urgent, current finding.",
    "B - nurture": "Good fit, nothing urgent. Sequence, don't call.",
    "C - opportunistic": "Urgent finding, weaker fit. Worth a look.",
    "D - deprioritise": "Neither fit nor urgency.",
    "R - needs review": "Classified as a company, but below the confidence "
                        "threshold. A human should confirm before outreach.",
}

st.set_page_config(page_title="Sales Intelligence", page_icon="◆", layout="wide")


@st.cache_data
def load() -> pd.DataFrame:
    return pd.read_parquet(DATA)


def count(n: int, singular: str, plural: str | None = None) -> str:
    """'1 host' / '4 hosts'. The '1 host(s)' construction reads as unfinished
    text, and an opener that looks unfinished does not get sent."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def signal_rows(row) -> list[Finding]:
    """Observable findings for one account, worst first.

    Each finding carries three forms: a short label and a detail for the table,
    and a prose phrase for the outreach opener. They differ because a table cell
    can rely on its column header for context and a sentence cannot — "1 host"
    means nothing mid-sentence without the finding it belongs to.

    `evidence` separates what was directly observed from what was inferred from
    a version banner. The two carry very different weight on a call, and
    collapsing them is how a rep ends up asserting a vulnerability that was
    never confirmed.
    """
    out: list[Finding] = []
    add = out.append

    if row.n_exposed_datastores:
        n = int(row.n_exposed_datastores)
        add(Finding("Exposed database",
                    f"{count(n, 'database')} reachable from the public internet",
                    "observed",
                    f"{count(n, 'database')} reachable from the public internet"))
    if row.n_remote_access:
        n = int(row.n_remote_access)
        add(Finding("Remote access exposed",
                    f"{count(n, 'service')} — telnet, RDP, VNC, FTP or SMB",
                    "observed",
                    f"{count(n, 'remote-access service')} open to the internet"))
    if row.heartbleed_vulnerable:
        add(Finding("Heartbleed", "Failed a direct vulnerability probe", "tested",
                    "a host failing a direct Heartbleed probe"))
    if row.n_exposed_cameras:
        n = int(row.n_exposed_cameras)
        add(Finding("Exposed cameras", f"{count(n, 'camera interface')} reachable",
                    "observed", f"{count(n, 'IP camera interface')} publicly reachable"))
    if row.n_open_directories:
        n = int(row.n_open_directories)
        add(Finding("Open directory listing", count(n, "host"), "observed",
                    f"directory listings exposed on {count(n, 'host')}"))
    if row.n_expired_certs:
        n = int(row.n_expired_certs)
        add(Finding("Expired certificate", f"{count(n, 'host')} serving an expired cert",
                    "observed", f"{count(n, 'host')} serving an expired certificate"))
    if pd.notna(row.soonest_cert_expiry_days) and 0 <= row.soonest_cert_expiry_days <= 30:
        d = int(row.soonest_cert_expiry_days)
        add(Finding("Certificate expiring", f"In {count(d, 'day')}", "observed",
                    f"a certificate expiring in {count(d, 'day')}"))
    if row.n_eol_services:
        n = int(row.n_eol_services)
        add(Finding("End-of-life software", f"{count(n, 'service')} past vendor support",
                    "observed", f"{count(n, 'service')} running software past vendor support"))
    if row.n_dead_ssl:
        n = int(row.n_dead_ssl)
        add(Finding("SSLv2/SSLv3 accepted", count(n, "host"), "observed",
                    f"{count(n, 'host')} still accepting SSLv2 or SSLv3"))
    if row.n_deprecated_tls:
        n = int(row.n_deprecated_tls)
        add(Finding("TLS 1.0/1.1 accepted", f"{count(n, 'host')} — a PCI-DSS finding",
                    "observed",
                    f"{count(n, 'host')} still accepting TLS 1.0 or 1.1, which is a "
                    "PCI-DSS finding"))
    if row.n_self_signed:
        n = int(row.n_self_signed)
        add(Finding("Self-signed certificate", count(n, "host"), "observed",
                    f"{count(n, 'host')} serving a self-signed certificate"))
    if row.n_weak_cert_sig:
        n = int(row.n_weak_cert_sig)
        add(Finding("SHA-1 certificate signature", count(n, "host"), "observed",
                    f"{count(n, 'certificate')} still signed with SHA-1"))
    if pd.notna(row.max_epss) and row.max_epss > 0:
        cvss = (f" — CVSS {row.headline_cve_cvss:.1f}"
                if pd.notna(row.headline_cve_cvss) else "")
        add(Finding(str(row.headline_cve),
                    f"{row.max_epss:.1%} chance of exploitation within 30 days{cvss}",
                    "inferred",
                    f"software associated with {row.headline_cve}, which carries a "
                    f"{row.max_epss:.0%} probability of exploitation in the next 30 days"))
    return out


def opener(row, signals) -> str:
    """A first line grounded in specific, checkable findings.

    Uses the prose form of each finding rather than the table form, and never
    lowercases it — the text carries acronyms (RDP, SMB, TLS, SHA-1) that read
    as typos in lower case, and an opener that looks sloppy does not get sent.

    Version-inferred findings are hedged explicitly. The product and version
    are observed facts; the vulnerability is a possibility. Leading with "you
    have 99 vulnerabilities" is usually wrong and costs the rep the call.
    """
    name = row.company
    if not signals:
        return f"No specific finding to lead with for {name}."

    observed = [f for f in signals if f.evidence in ("observed", "tested")]
    inferred = [f for f in signals if f.evidence == "inferred"]

    lines = [f"Hi — we track internet-facing exposure across "
             f"{row.primary_country_name}."]

    if observed:
        first = observed[0].phrase
        rest = f", and {observed[1].phrase}" if len(observed) > 1 else ""
        lines.append(f"On {name}'s public infrastructure we can currently see "
                     f"{first}{rest}. That's visible to anyone scanning, not "
                     f"just us.")

    # The product name is deliberately not asserted alongside the CVE. Both are
    # aggregated per entity by highest EPSS, but from potentially different
    # services on the host — so "you are running Apache, which is vulnerable to
    # <CVE>" can pair a web server with a glibc bug. Observed once in testing:
    # Apache httpd against CVE-2015-0235 (GHOST). A wrong technical claim in a
    # first email is worse than a vaguer true one, so the opener cites the
    # finding and asks, rather than diagnosing.
    if inferred:
        lines.append(
            f"Separately, one of your public hosts is running a software version "
            f"associated with {row.headline_cve} — currently around a "
            f"{row.max_epss:.0%} chance of being exploited in the next 30 days. "
            f"That one is inferred from a version banner rather than tested, so "
            f"it's worth confirming whether it actually applies to you.")

    lines.append("Happy to walk through the detail on a short call if useful.")
    return " ".join(lines)


df = load()

# ---------------------------------------------------------------- sidebar
st.sidebar.title("Filters")
st.sidebar.caption("Territory and segment, the way a rep works a list.")

tiers = sorted(t for t in df.tier.unique() if not t.startswith(("X", "U")))
tier_sel = st.sidebar.multiselect("Tier", tiers,
                                  default=[t for t in tiers if t.startswith("A")] or tiers)

markets = sorted(df.primary_country_name.dropna().unique())
market_sel = st.sidebar.multiselect("Market", markets, default=[])

# Public sector dominates the ranking — universities and research institutes
# run large, old, heterogeneous estates, so they accumulate findings. They are
# genuine buyers but a different motion: procurement and tenders, not a cold
# call. Separated so a rep can work one or the other, not a mixed list.
segment = st.sidebar.radio(
    "Segment", ["All", "Commercial only", "Public sector only"], index=1,
    help="Public sector means universities, government bodies and schools.")

size_bands = {"Any": (0, 10**9), "1 host": (1, 1), "2–10": (2, 10),
              "11–100": (11, 100), "101–500": (101, 500), "500+": (501, 10**9)}
size_sel = st.sidebar.selectbox("Estate size", list(size_bands), index=0)

min_intent = st.sidebar.slider("Minimum urgency", 0, 100, 0, 5)
whitespace_only = st.sidebar.checkbox(
    "Whitespace only", help="No WAF anywhere — no incumbent vendor to displace.")
actively_exploited = st.sidebar.checkbox(
    "Actively exploited only", help="A vulnerability with EPSS above 10%.")

view = df[df.tier.isin(tier_sel)] if tier_sel else df
if segment == "Commercial only":
    view = view[view.entity_class == "end_customer_company"]
elif segment == "Public sector only":
    view = view[view.entity_class == "government_or_education"]
if market_sel:
    view = view[view.primary_country_name.isin(market_sel)]
low, high = size_bands[size_sel]
view = view[(view.n_hosts >= low) & (view.n_hosts <= high)]
view = view[view.intent_score >= min_intent]
if whitespace_only:
    view = view[view.no_waf_anywhere]
if actively_exploited:
    view = view[view.max_epss.fillna(0) > 0.10]

view = view.sort_values(["intent_score", "fit_score"], ascending=False)

# ---------------------------------------------------------------- header
st.title("Sales intelligence")
st.caption("Companies with internet-facing security exposure, ranked by how "
           "urgently they need help and how well they fit the ICP.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Accounts in view", f"{len(view):,}")
c2.metric("Call now", f"{(view.tier == 'A - call now').sum():,}")
c3.metric("Whitespace", f"{view.no_waf_anywhere.sum():,}",
          help="No perimeter vendor detected anywhere in the estate")
c4.metric("Actively exploited", f"{(view.max_epss.fillna(0) > 0.10).sum():,}",
          help="Carrying a vulnerability with EPSS above 10%")

if view.empty:
    st.info("No accounts match these filters.")
    st.stop()

# ---------------------------------------------------------------- list
st.subheader("Prospect list")
st.dataframe(
    view[["company", "tier", "intent_score", "fit_score", "primary_country_name",
          "n_hosts", "max_epss", "headline_cve", "no_waf_anywhere"]]
    .rename(columns={
        "company": "Company", "tier": "Tier", "intent_score": "Urgency",
        "fit_score": "Fit", "primary_country_name": "Market", "n_hosts": "Hosts",
        "max_epss": "Exploit prob.", "headline_cve": "Top CVE",
        "no_waf_anywhere": "Whitespace"}),
    use_container_width=True, hide_index=True, height=340,
    column_config={
        "Exploit prob.": st.column_config.ProgressColumn(
            format="%.0f%%", min_value=0, max_value=1),
        "Urgency": st.column_config.NumberColumn(format="%d"),
        "Fit": st.column_config.NumberColumn(format="%d")},
)

# ---------------------------------------------------------------- detail
st.divider()
choice = st.selectbox("Account detail", view.entity_domain.tolist(),
                      format_func=lambda d: f"{view.set_index('entity_domain').loc[d, 'company']}  ·  {d}")
row = view.set_index("entity_domain").loc[choice]
signals = signal_rows(row)

st.header(row.company)
st.caption(f"{choice}  ·  {row.primary_city or ''} {row.primary_country_name or ''}  ·  "
           f"{int(row.n_hosts)} hosts, {int(row.n_ports)} open ports")

left, right = st.columns([2, 1])

with left:
    st.subheader("Why now")
    if signals:
        st.dataframe(pd.DataFrame([(f.label, f.detail, f.evidence) for f in signals],
                                  columns=["Finding", "Detail", "Evidence"]),
                     use_container_width=True, hide_index=True)
        st.caption("**observed** — seen directly in the scan.  "
                   "**tested** — confirmed by an active probe.  "
                   "**inferred** — implied by a version banner; the software is "
                   "confirmed, the vulnerability is not.")
    else:
        st.write("No specific findings.")

    st.subheader("What to say")
    st.text_area("Suggested opener", opener(row, signals), height=170,
                 label_visibility="collapsed")

with right:
    st.subheader("Why this account")
    st.metric("Urgency", int(row.intent_score))
    st.metric("Fit", int(row.fit_score))
    st.write(f"**{row.tier}**")
    st.caption(TIER_HELP.get(row.tier, ""))

    st.write("**Score drivers**")
    drivers = [
        ("Exploitation probability", row.pts_epss),
        ("Critical CVE", row.pts_critical_cve),
        ("Exposed datastore", row.pts_datastore),
        ("Remote access", row.pts_remote_access),
        ("End-of-life software", row.pts_eol),
        ("Certificate expired", row.pts_expired_cert),
        ("Estate size band", row.pts_size_band),
        ("No incumbent vendor", row.pts_whitespace),
        ("Multi-market", row.pts_multi_country),
    ]
    for label, points in drivers:
        if points:
            st.write(f"`+{int(points):>2}`  {label}")

    st.write("**Classification**")
    st.caption(f"{row.entity_class} · confidence {row.class_confidence:.2f}")
    st.caption(f"_{row.class_reasoning}_")
    if row.tier.startswith("R"):
        st.warning("Below the confidence threshold — confirm before outreach.")

st.divider()
st.caption(
    "Every classification, score and finding is precomputed. This app performs "
    "no inference and holds no credentials. Vulnerability findings are inferred "
    "from version banners unless marked observed or tested."
)
