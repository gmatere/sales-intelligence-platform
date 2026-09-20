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

import pandas as pd
import streamlit as st

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


def signal_rows(row) -> list[tuple[str, str, str]]:
    """Observable findings for one account, worst first.

    Separated into what was directly observed and what was inferred from a
    version banner, because the two carry very different weight on a call.
    """
    out: list[tuple[str, str, str]] = []

    if row.n_exposed_datastores:
        out.append(("Exposed database", f"{int(row.n_exposed_datastores)} service(s) "
                    "reachable from the public internet", "observed"))
    if row.n_remote_access:
        out.append(("Remote access exposed", f"{int(row.n_remote_access)} service(s) — "
                    "telnet, RDP, VNC, FTP or SMB", "observed"))
    if row.heartbleed_vulnerable:
        out.append(("Heartbleed", "Failed a direct vulnerability probe", "tested"))
    if row.n_exposed_cameras:
        out.append(("Exposed cameras", f"{int(row.n_exposed_cameras)} IP camera "
                    "interface(s) reachable", "observed"))
    if row.n_open_directories:
        out.append(("Open directory listing", f"{int(row.n_open_directories)} host(s)",
                    "observed"))
    if row.n_expired_certs:
        out.append(("Expired certificate", f"{int(row.n_expired_certs)} host(s) serving "
                    "an expired certificate", "observed"))
    if pd.notna(row.soonest_cert_expiry_days) and 0 <= row.soonest_cert_expiry_days <= 30:
        out.append(("Certificate expiring", f"In {int(row.soonest_cert_expiry_days)} days",
                    "observed"))
    if row.n_eol_services:
        out.append(("End-of-life software", f"{int(row.n_eol_services)} service(s) past "
                    "vendor support", "observed"))
    if row.n_dead_ssl:
        out.append(("SSLv2/SSLv3 accepted", f"{int(row.n_dead_ssl)} host(s)", "observed"))
    if row.n_deprecated_tls:
        out.append(("TLS 1.0/1.1 accepted", f"{int(row.n_deprecated_tls)} host(s) — a "
                    "PCI-DSS finding", "observed"))
    if row.n_self_signed:
        out.append(("Self-signed certificate", f"{int(row.n_self_signed)} host(s)",
                    "observed"))
    if row.n_weak_cert_sig:
        out.append(("SHA-1 certificate signature", f"{int(row.n_weak_cert_sig)} host(s)",
                    "observed"))
    if pd.notna(row.max_epss) and row.max_epss > 0:
        out.append((f"{row.headline_cve}",
                    f"{row.max_epss:.1%} chance of exploitation within 30 days"
                    f"{f' — CVSS {row.headline_cve_cvss:.1f}' if pd.notna(row.headline_cve_cvss) else ''}",
                    "inferred"))
    return out


def opener(row, signals) -> str:
    """A first line grounded in a specific finding.

    Deliberately hedged on anything version-inferred: the product and version
    are observed facts, the vulnerability is a possibility. Leading with
    'you have 99 vulnerabilities' is usually wrong and loses the call.
    """
    name = row.company
    if not signals:
        return f"No specific finding to lead with for {name}."

    headline, detail, kind = signals[0]

    if kind == "inferred" and pd.notna(row.headline_cve_product):
        version = f" {row.headline_cve_version}" if pd.notna(row.headline_cve_version) else ""
        return (
            f"Hi — we track internet-facing exposure across {row.primary_country_name}. "
            f"{name} appears to be running {row.headline_cve_product}{version} on a "
            f"public-facing host. That version is associated with {row.headline_cve}, "
            f"which currently carries a {row.max_epss:.0%} probability of exploitation "
            f"in the next 30 days. Worth fifteen minutes to confirm whether you're "
            f"affected?"
        )

    return (
        f"Hi — we track internet-facing exposure across {row.primary_country_name}. "
        f"We can see {headline.lower()} on {name}'s public infrastructure "
        f"({detail.lower()}). That's visible to anyone scanning, not just us. "
        f"Worth a short call?"
    )


df = load()

# ---------------------------------------------------------------- sidebar
st.sidebar.title("Filters")
st.sidebar.caption("Territory and segment, the way a rep works a list.")

tiers = sorted(t for t in df.tier.unique() if not t.startswith(("X", "U")))
tier_sel = st.sidebar.multiselect("Tier", tiers,
                                  default=[t for t in tiers if t.startswith("A")] or tiers)

markets = sorted(df.primary_country_name.dropna().unique())
market_sel = st.sidebar.multiselect("Market", markets, default=[])

size_bands = {"Any": (0, 10**9), "1 host": (1, 1), "2–10": (2, 10),
              "11–100": (11, 100), "101–500": (101, 500), "500+": (501, 10**9)}
size_sel = st.sidebar.selectbox("Estate size", list(size_bands), index=0)

min_intent = st.sidebar.slider("Minimum urgency", 0, 100, 0, 5)
whitespace_only = st.sidebar.checkbox(
    "Whitespace only", help="No WAF anywhere — no incumbent vendor to displace.")
actively_exploited = st.sidebar.checkbox(
    "Actively exploited only", help="A vulnerability with EPSS above 10%.")

view = df[df.tier.isin(tier_sel)] if tier_sel else df
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
        st.dataframe(pd.DataFrame(signals, columns=["Finding", "Detail", "Evidence"]),
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
