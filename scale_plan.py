#!/usr/bin/env python3
"""
scale_plan.py - how to run the Artemis web-data stage modules at 9-29 PB on Azure.

Inputs (all real, all fetched by code in this folder):
  plans/cc_measurement.json   <- cc_measure.py   (Common Crawl: exact file counts,
                                                  HEAD-measured sizes, extrapolated totals)
  plans/throughput_bench.json <- bench_throughput.py (one real WARC processed on this box)
  plans/azure_prices_<region>.json <- azure_prices.py (Azure Retail Prices API snapshot)
  gapfill_sources.py          <- published sizes of other open datasets (with links)

Every number in the output carries a label:
  MEASURED   - counted/measured directly (file counts, sampled sizes, bench timings, list prices)
  ESTIMATE   - extrapolated from measurements (totals = count x sample mean, costs, wall-clock)
  PUBLISHED  - a figure stated by a third party (linked)
  ASSUMPTION - a planning knob you can change on the CLI

Usage
  python scale_plan.py --target 29PB            # primary
  python scale_plan.py --target 9PB             # secondary
  python scale_plan.py --target 2.5e16 --days 60 --vm Standard_D32as_v5 --region eastus

Writes plans/plan_<label>.json, plans/plan_<label>.md and plans/main.parameters.<label>.json
(Bicep parameter file for bicep/main.bicep - NOT deployed).
Nothing here creates cloud resources or spends money.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys

import azure_prices as ap_
from gapfill_sources import GAPFILL

HERE = os.path.dirname(os.path.abspath(__file__))
PLANS = os.path.join(HERE, "plans")
PB, TB, GB = 10**15, 10**12, 10**9
PiB, GiB = 2**50, 2**30
ET = dt.timezone(dt.timedelta(hours=-4), "EDT")

# ---- published Azure limits (https://learn.microsoft.com/en-us/azure/storage/common/scalability-targets-standard-account)
AZ_LIMITS = {
    "source": "https://learn.microsoft.com/en-us/azure/storage/common/scalability-targets-standard-account",
    "account_capacity_bytes_default": 5 * PiB,          # "Default maximum storage account capacity: 5 PiB" (raise via support)
    "accounts_per_region_default": 250,                 # "250 by default, 500 by request"
    "accounts_per_region_max": 500,
    "ingress_gbps_per_account_default": 60,             # East US & other listed regions
    "egress_gbps_per_account_default": 200,
    "requests_per_sec_per_account_default": 40000,
}
VM_VCPUS = {"Standard_D32as_v5": 32, "Standard_D16as_v5": 16, "Standard_D64as_v5": 64,
            "Standard_D32ads_v5": 32, "Standard_F32s_v2": 32, "Standard_D96as_v5": 96}


def log(msg):
    line = f"{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S %Z} {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "PROGRESS.log"), "a") as f:
        f.write(line + "\n")


def parse_bytes(s: str) -> int:
    m = re.fullmatch(r"\s*([0-9.eE+]+)\s*(PB|TB|GB|PiB|TiB|B)?\s*", s)
    if not m:
        raise SystemExit(f"bad --target {s!r}; use e.g. 9PB, 29PB, 2.9e16")
    v = float(m.group(1)); u = m.group(2) or "B"
    return int(v * {"PB": PB, "TB": TB, "GB": GB, "PiB": PiB, "TiB": 2**40, "B": 1}[u])


def fmt(b):
    return f"{b/PB:,.3f} PB" if b >= 0.1 * PB else f"{b/TB:,.1f} TB"


def money(x):
    return "n/a" if x is None else f"${x:,.0f}"


def L(value, kind, **extra):
    d = {"value": value, "label": kind}; d.update(extra); return d


def load_cc():
    fn = os.path.join(PLANS, "cc_measurement.json")
    d = json.load(open(fn))
    rows = []   # one per crawl (newest first, collinfo order) + CC-NEWS as one pseudo-source
    for cid, rec in d["crawls"].items():
        ks = {k: m for k, m in rec["kinds"].items() if m.get("est_bytes")}
        if not ks:
            continue
        rows.append({"id": cid, "from": rec.get("from"), "kinds": ks})
    news = {k: m for k, m in d.get("cc_news", {}).items() if m.get("est_bytes")}
    return d, rows, news


def plan(a):
    target = parse_bytes(a.target)
    label = a.label or a.target.replace(".", "_")
    meas, crawls, news = load_cc()
    bench = json.load(open(os.path.join(PLANS, "throughput_bench.json")))
    prices = ap_.load_or_fetch(a.region, a.vm)
    kinds = a.kinds.split(",")
    errors = meas.get("errors", [])

    # ---------------- 1. what does ALL of Common Crawl add up to? ----------------
    def crawl_bytes(r, ks=kinds):
        return sum(r["kinds"][k]["est_bytes"] for k in ks if k in r["kinds"])

    def crawl_files(r, ks=kinds):
        return sum(r["kinds"][k]["files"] for k in ks if k in r["kinds"])

    def crawl_ci2(r, ks=kinds):
        return sum(r["kinds"][k]["est_ci95_bytes"] ** 2 for k in ks if k in r["kinds"])

    news_bytes = sum(m["est_bytes"] for m in news.values()) if a.include_news else 0
    news_files = sum(m["files"] for m in news.values()) if a.include_news else 0
    news_ci2 = sum(m["est_ci95_bytes"] ** 2 for m in news.values()) if a.include_news else 0
    all_cc = sum(crawl_bytes(r) for r in crawls) + news_bytes
    all_cc_ci = math.sqrt(sum(crawl_ci2(r) for r in crawls) + news_ci2)
    all_warc = sum(crawl_bytes(r, ["warc"]) for r in crawls) + news_bytes
    by_kind = {k: sum(r["kinds"][k]["est_bytes"] for r in crawls if k in r["kinds"]) for k in kinds}
    if a.include_news:
        by_kind["cc-news-warc"] = news_bytes

    # ---------------- 2. select crawls (newest first) until target ---------------
    sel, acc, files, ci2 = [], 0.0, 0, 0.0
    if a.include_news and news_bytes:
        sel.append("CC-NEWS (all months)"); acc += news_bytes; files += news_files; ci2 += news_ci2
    sel_warc = news_bytes
    for r in crawls:
        if acc >= target:
            break
        sel.append(r["id"]); acc += crawl_bytes(r); files += crawl_files(r); ci2 += crawl_ci2(r)
        sel_warc += crawl_bytes(r, ["warc"])
    reached = acc >= target
    shortfall = max(0.0, target - acc)

    # ---------------- 3. storage layout (Azure limits) ---------------------------
    acct_cap = AZ_LIMITS["account_capacity_bytes_default"] * a.fill
    land_bytes = min(acc, target)                     # strategy A: land raw CC bytes
    accounts_A = max(1, math.ceil(land_bytes / acct_cap))
    text_yield = bench["text_yield_fraction_of_compressed"]
    text_out = sel_warc * text_yield                  # strategy B: keep only extracted text
    accounts_B = max(1, math.ceil(text_out / acct_cap))

    # ---------------- 4. compute (measured per-core throughput) ------------------
    vcpus = VM_VCPUS.get(a.vm) or int(re.search(r"(\d+)", a.vm.split("_")[1]).group(1))
    rate = bench["fast_path_compressed_MBps_per_core"] * 1e6 if a.extractor == "fast" \
        else bench["quality_path_compressed_MBps_per_core"] * 1e6
    core_h = sel_warc / rate / 3600 * a.overhead
    vm_h = core_h / vcpus
    wall_h = a.days * 24
    nodes = math.ceil(vm_h / wall_h)
    files_per_task = a.files_per_task
    warc_files_sel = sum(r["kinds"]["warc"]["files"] for r in crawls if r["id"] in sel and "warc" in r["kinds"]) + news_files
    tasks = math.ceil(warc_files_sel / files_per_task)
    agg_gbps = sel_warc * 8 / (a.days * 86400) / 1e9          # download rate needed from Common Crawl
    per_node_mbps = agg_gbps * 1e3 / max(nodes, 1)
    ingress_accounts = math.ceil((land_bytes * 8 / (a.days * 86400) / 1e9) / AZ_LIMITS["ingress_gbps_per_account_default"])

    # ---------------- 5. prices (Azure Retail Prices API only) -------------------
    pick = ap_.pick
    vm_spot = pick(prices, "vm", sku=f"{a.vm} Spot", exclude_product="Windows")
    vm_spot = [m for m in vm_spot if "Cloud Services" not in m["productName"]]
    vm_od = [m for m in pick(prices, "vm", sku=a.vm, exclude_product="Windows") if "Cloud Services" not in m["productName"]]
    spot_ph = vm_spot[0]["retailPrice"] if vm_spot else None
    od_ph = vm_od[0]["retailPrice"] if vm_od else None
    compute_spot = vm_h * spot_ph if spot_ph else None
    # same plan with the other (measured) extractor, for comparison
    alt_rate = (bench["quality_path_compressed_MBps_per_core"] if a.extractor == "fast"
                else bench["fast_path_compressed_MBps_per_core"]) * 1e6
    alt_vm_h = sel_warc / alt_rate / 3600 * a.overhead / vcpus
    alt_nodes = math.ceil(alt_vm_h / wall_h)
    alt_spot = alt_vm_h * spot_ph if spot_ph else None
    # nodes needed if the network (not CPU) is the bottleneck: assume each node can sustain --node-gbps from CC
    net_nodes = math.ceil((sel_warc * 8 / (a.days * 86400) / 1e9) / a.node_gbps)
    compute_od = vm_h * od_ph if od_ph else None

    def store_month(tier, nbytes):
        m = pick(prices, "adls_storage", sku=f"{tier} LRS", meter=f"{tier} LRS Data Stored")
        c, parts = ap_.tiered_cost(m, nbytes / GiB)   # Azure bills GB = GiB (binary) per their docs
        return c

    stor = {t: store_month(t, land_bytes) for t in ("Hot", "Cool", "Cold", "Archive")}
    stor_text_hot = store_month("Hot", text_out)
    wr = pick(prices, "adls_storage", sku="Hot LRS", meter="Hot Write Operations")
    write_ops = land_bytes / (a.block_mib * 2**20)
    write_cost = write_ops / 1e4 * wr[0]["retailPrice"] if wr else None
    arch_ret = pick(prices, "adls_storage", sku="Archive LRS", meter="Archive Data Retrieval")
    arch_ret_cost = land_bytes / GiB * arch_ret[0]["retailPrice"] if arch_ret else None
    bw_in = [m for m in prices["groups"]["bandwidth"]["items"] if m["meterName"] == "Standard Data Transfer In"]
    bw_inter = [m for m in prices["groups"]["bandwidth"]["items"] if m["meterName"] == "Standard Inter-Region Data Transfer"]
    acr = pick(prices, "acr", sku="Premium", meter="Premium Registry Unit")
    acr_cost = acr[0]["retailPrice"] * a.days if acr else None

    # ---------------- 6. gap fill (only if Common Crawl falls short) -------------
    gap = []
    gap_math = None
    if not reached:
        for g in GAPFILL:
            gap.append({**g, "published_bytes_h": fmt(g["published_bytes"])})
        new = [g for g in GAPFILL if g["new_vs_cc"]]
        open_text = [g for g in new if g["text_like"]]
        open_bulk = [g for g in new if g["open_bulk_download"]]
        gap_math = {
            "shortfall": fmt(shortfall),
            "new_text_like_published_total": fmt(sum(g["published_bytes"] for g in open_text)),
            "new_openly_downloadable_published_total": fmt(sum(g["published_bytes"] for g in open_bulk)),
            "all_new_incl_partnership_published_total": fmt(sum(g["published_bytes"] for g in new)),
            "closes_gap_with_text_only": sum(g["published_bytes"] for g in open_text) >= shortfall,
            "closes_gap_with_open_downloads": sum(g["published_bytes"] for g in open_bulk) >= shortfall,
            "note": "PUBLISHED sizes, not measured; raw bytes are not training value - media/scientific bytes "
                    "yield little or no LM text.",
        }

    total_cc_files = sum(crawl_files(r) for r in crawls) + news_files
    out = {
        "label": label, "generated_et": dt.datetime.now(ET).isoformat(),
        "target_bytes": L(target, "ASSUMPTION", h=fmt(target)),
        "sources_scope": "public web data and open online datasets only (no private/user-owned data)",
        "common_crawl_all": {
            "crawls_measured": L(sum(1 for r in crawls if "warc" in r["kinds"]), "MEASURED",
                                 note=f"crawls with a published warc.paths.gz; collinfo lists {len(meas['crawls'])}; "
                                      "the 3 pre-2013 ARC-era crawls (2008-2009, 2009-2010, 2012) publish no WARC path lists "
                                      "(anonymous S3 listing is denied), so only their cc-index files are counted"),
            "files": L(total_cc_files, "MEASURED", note="exact counts from *.paths.gz"),
            "bytes": L(all_cc, "ESTIMATE", h=fmt(all_cc), ci95=fmt(all_cc_ci),
                       method="per (crawl,kind): file count x mean of HEAD-measured sample sizes"),
            "bytes_by_kind": {k: L(v, "ESTIMATE", h=fmt(v)) for k, v in by_kind.items()},
            "warc_only_bytes": L(all_warc, "ESTIMATE", h=fmt(all_warc),
                                 note="WARC = the raw pages; WAT/WET are derived from it (metadata / plain text), so "
                                      "they add bytes but little new content"),
            "kinds_counted": kinds, "cc_news_included": a.include_news,
            "measurement_errors": errors[:20], "measurement_errors_count": len(errors),
            "measured_at_utc": meas["generated_utc"],
        },
        "reaches_target": reached,
        "selection": {
            "crawls_needed": L(len([s for s in sel if s.startswith("CC-MAIN")]), "ESTIMATE",
                               note="newest first; CC-NEWS (all months) is added first when included"),
            "crawl_ids": sel,
            "files": L(files, "MEASURED"),
            "bytes": L(acc, "ESTIMATE", h=fmt(acc), ci95=fmt(math.sqrt(ci2))),
            "shortfall_bytes": L(shortfall, "ESTIMATE", h=fmt(shortfall)),
        },
        "gap_fill_candidates": gap,
        "gap_fill_math": gap_math,
        "storage": {
            "limits": AZ_LIMITS,
            "strategy_A_land_raw": {
                "bytes": L(land_bytes, "ESTIMATE", h=fmt(land_bytes)),
                "storage_accounts": L(accounts_A, "ESTIMATE",
                                      note=f"ceil(bytes / (5 PiB x {a.fill} fill)); default cap 5 PiB/account, raise via Azure Support"),
                "accounts_needed_for_ingress": L(ingress_accounts, "ESTIMATE", note=f"to land within {a.days} days at 60 Gbps/account"),
                "within_region_account_limit": accounts_A <= AZ_LIMITS["accounts_per_region_default"],
                "monthly_storage_cost_usd": {t: L(v, "ESTIMATE", h=money(v)) for t, v in stor.items()},
                "write_ops_cost_usd": L(write_cost, "ESTIMATE", h=money(write_cost), note=f"{a.block_mib} MiB blocks"),
                "archive_rehydrate_cost_usd": L(arch_ret_cost, "ESTIMATE", h=money(arch_ret_cost),
                                                note="one full read-back from Archive (standard priority)"),
            },
            "strategy_B_stream_keep_text": {
                "text_bytes_upper_bound": L(text_out, "ESTIMATE", h=fmt(text_out),
                                            note=f"WARC bytes x measured text yield {text_yield:.3%} (one real WARC, "
                                                 "before filter/dedup/PII; final will be smaller)"),
                "storage_accounts": L(accounts_B, "ESTIMATE"),
                "monthly_hot_storage_cost_usd": L(stor_text_hot, "ESTIMATE", h=money(stor_text_hot)),
                "note": "recommended: stream WARCs from Common Crawl, keep only extracted text + token shards",
            },
            "containers": ["raw", "text", "filtered", "dedup", "scrubbed", "tokens", "manifests", "logs"],
        },
        "compute": {
            "vm": a.vm, "vcpus_per_vm": vcpus, "extractor": a.extractor,
            "per_core_MBps": L(rate / 1e6, "MEASURED", src="plans/throughput_bench.json (this box, 1 core, 1 real WARC)"),
            "overhead_factor": L(a.overhead, "ASSUMPTION", note="I/O, retries, spot evictions, filter/dedup/tokenize beyond extraction"),
            "warc_bytes_to_process": L(sel_warc, "ESTIMATE", h=fmt(sel_warc)),
            "core_hours": L(core_h, "ESTIMATE"), "vm_hours": L(vm_h, "ESTIMATE"),
            "wall_clock_days": L(a.days, "ASSUMPTION"),
            "spot_nodes_needed": L(nodes, "ESTIMATE"),
            "total_vcpus": L(nodes * vcpus, "ESTIMATE", note="needs a Batch/Spot vCPU quota increase - defaults are far lower"),
            "batch_tasks": L(tasks, "ESTIMATE", note=f"{files_per_task} WARC files per task"),
            "spot_price_per_hour": L(spot_ph, "MEASURED", src="Azure Retail Prices API", meter=vm_spot[0] if vm_spot else None),
            "ondemand_price_per_hour": L(od_ph, "MEASURED", src="Azure Retail Prices API"),
            "compute_cost_spot_usd": L(compute_spot, "ESTIMATE", h=money(compute_spot)),
            "compute_cost_ondemand_usd": L(compute_od, "ESTIMATE", h=money(compute_od)),
            "alt_extractor": {"name": "quality" if a.extractor == "fast" else "fast",
                              "per_core_MBps": L(alt_rate / 1e6, "MEASURED"),
                              "spot_nodes_needed": L(alt_nodes, "ESTIMATE"),
                              "compute_cost_spot_usd": L(alt_spot, "ESTIMATE", h=money(alt_spot))},
            "nodes_if_network_bound": L(net_nodes, "ESTIMATE", note=f"at {a.node_gbps} Gbps sustained per node from Common Crawl (ASSUMPTION)"),
            "acr_premium_cost_usd": L(acr_cost, "ESTIMATE", h=money(acr_cost), note=f"{a.days} days x Premium Registry Unit/day"),
        },
        "network": {
            "aggregate_download_gbps_needed": L(agg_gbps, "ESTIMATE", note=f"WARC bytes over {a.days} days"),
            "per_node_mbps": L(per_node_mbps, "ESTIMATE"),
            "single_stream_download_MBps_this_box": L(bench["download_MBps_this_box"], "MEASURED"),
            "azure_data_transfer_in_price": L(bw_in[0]["retailPrice"] if bw_in else None, "MEASURED", unit="USD/GB",
                                              src="Azure Retail Prices API (Bandwidth)"),
            "azure_inter_region_price": L(bw_inter[0]["retailPrice"] if bw_inter else None, "MEASURED", unit="USD/GB"),
            "common_crawl_location": "AWS S3 bucket s3://commoncrawl in us-east-1 (N. Virginia), "
                                     "HTTPS mirror https://data.commoncrawl.org/ (source: https://commoncrawl.org/get-started)",
            "egress_notes": [
                "Common Crawl is hosted by the AWS Open Data Sponsorship program; HTTPS download is free to the requester "
                "(commoncrawl.org/get-started). Azure charges $0 for data transfer IN (price API above).",
                "Cross-cloud pulls at this scale are throttled: our own measurement run got HTTP 403/503 rate-limit "
                "responses at only 4-8 concurrent HEAD requests. Multi-Gbps sustained pulls need coordination with Common Crawl.",
                "Common Crawl recommends processing IN AWS us-east-1 next to the data. Doing extraction there and shipping only "
                f"the text (~{text_yield:.1%} of WARC bytes) to Azure cuts transfer ~{1/text_yield:.0f}x; AWS egress on that text "
                "is billed by AWS (AWS price NOT fetched here - check https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer).",
                "Azure East US is also in Virginia, so latency to us-east-1 is low, but it is still internet/cross-cloud traffic.",
            ],
        },
        "price_snapshot": {"source": prices["source"], "fetched_utc": prices["fetched_utc"], "region": prices["region"],
                           "file": f"plans/azure_prices_{a.region}.json"},
        "blockers": [
            "Default Azure limits: 5 PiB/storage account, 250 accounts/region, 60 Gbps ingress/account, low default "
            "Batch/Spot vCPU quota -> support requests needed before any real run.",
            "Common Crawl rate limiting (observed 403/503) caps cross-cloud throughput; plan for AWS-side extraction or a CC agreement.",
            "Spot capacity for thousands of vCPUs in one region is not guaranteed; spread across regions/VM sizes.",
            "Licensing: Common Crawl ToU allows use, but page copyrights remain with owners - licence/robots/opt-out filtering is required.",
        ],
    }
    if not reached:
        out["blockers"].insert(0, f"All measured Common Crawl data ({fmt(all_cc)}) is BELOW the {fmt(target)} target; "
                                  f"shortfall {fmt(shortfall)}. Open TEXT data cannot close this gap; only media/scientific "
                                  "archives or partnership data (Internet Archive) are that large.")
    if errors:
        out["blockers"].append(f"{len(errors)} Common Crawl sections could not be measured (rate limits) - totals exclude them.")

    # Bicep parameter file for this plan (NOT deployed)
    bicep_params = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"value": a.region}, "namePrefix": {"value": "artemis"},
            "storageAccountCount": {"value": accounts_A if a.strategy == "A" else accounts_B},
            "vmSize": {"value": a.vm}, "maxSpotNodes": {"value": nodes},
            "coolAfterDays": {"value": 30}, "archiveAfterDays": {"value": 90},
        },
    }
    os.makedirs(PLANS, exist_ok=True)
    json.dump(out, open(os.path.join(PLANS, f"plan_{label}.json"), "w"), indent=1, default=str)
    json.dump(bicep_params, open(os.path.join(PLANS, f"main.parameters.{label}.json"), "w"), indent=1)
    open(os.path.join(PLANS, f"plan_{label}.md"), "w").write(render_md(out))
    return out


def render_md(o):
    c, s, st, cp, nw = o["common_crawl_all"], o["selection"], o["storage"], o["compute"], o["network"]
    A, B = st["strategy_A_land_raw"], st["strategy_B_stream_keep_text"]
    lines = [
        f"# Artemis scale plan - target {o['target_bytes']['h']}  (generated {o['generated_et']})",
        "", f"Scope: {o['sources_scope']}", "",
        "## Does all of Common Crawl reach the target?",
        f"- Crawls measured: **{c['crawls_measured']['value']}** (MEASURED) - {c['crawls_measured']['note']}",
        f"- Files: **{c['files']['value']:,}** (MEASURED, exact from paths.gz)",
        f"- All of Common Crawl ({', '.join(c['kinds_counted'])}{', + CC-NEWS' if c['cc_news_included'] else ''}): "
        f"**{c['bytes']['h']}** +/- {c['bytes']['ci95']} (ESTIMATE)",
        f"- Raw WARC only: {c['warc_only_bytes']['h']} (ESTIMATE)",
        *[f"  - {k}: {v['h']}" for k, v in c["bytes_by_kind"].items()],
        f"- **Reaches target: {'YES' if o['reaches_target'] else 'NO'}**; shortfall {s['shortfall_bytes']['h']}",
        f"- Measurement errors excluded: {c['measurement_errors_count']}",
        "", "## Selection (newest first)",
        f"- Crawls needed: {s['crawls_needed']['value']} ; files {s['files']['value']:,} ; bytes {s['bytes']['h']} +/- {s['bytes']['ci95']}",
        f"- First/last: {s['crawl_ids'][:3]} ... {s['crawl_ids'][-2:]}",
    ]
    if o["gap_fill_candidates"]:
        lines += ["", "## Gap-fill candidates (PUBLISHED sizes, not measured)",
                  "| dataset | published size | new vs CC | text? | open bulk? | access | source |", "|---|---|---|---|---|---|---|"]
        for g in o["gap_fill_candidates"]:
            lines.append(f"| {g['name']} | {g['published_as']} | {g['new_vs_cc']} | {g['text_like']} | {g['open_bulk_download']} | {g['access']} | {g['source']} |")
        gm = o["gap_fill_math"]
        lines += ["", f"- Shortfall {gm['shortfall']}. New open TEXT/code sources total {gm['new_text_like_published_total']} "
                  f"(closes gap: {gm['closes_gap_with_text_only']}). All openly downloadable new sources total "
                  f"{gm['new_openly_downloadable_published_total']} (closes gap: {gm['closes_gap_with_open_downloads']}). "
                  f"Including partnership-only Internet Archive: {gm['all_new_incl_partnership_published_total']}. {gm['note']}"]
    lines += [
        "", "## Storage (Azure ADLS Gen2, LRS)",
        f"- A) land raw: {A['bytes']['h']} -> **{A['storage_accounts']['value']} storage accounts** ({A['storage_accounts']['note']}); "
        f"{A['accounts_needed_for_ingress']['value']} needed for ingress in {cp['wall_clock_days']['value']} days",
        f"  - monthly: Hot {A['monthly_storage_cost_usd']['Hot']['h']}, Cool {A['monthly_storage_cost_usd']['Cool']['h']}, "
        f"Cold {A['monthly_storage_cost_usd']['Cold']['h']}, Archive {A['monthly_storage_cost_usd']['Archive']['h']} (ESTIMATE from list prices)",
        f"  - write ops one-time {A['write_ops_cost_usd']['h']}; archive read-back {A['archive_rehydrate_cost_usd']['h']}",
        f"- B) stream, keep text: {B['text_bytes_upper_bound']['h']} upper bound -> {B['storage_accounts']['value']} account(s); "
        f"Hot {B['monthly_hot_storage_cost_usd']['h']}/month",
        "", "## Compute (Azure Batch, Spot)",
        f"- {cp['vm']} ({cp['vcpus_per_vm']} vCPU), extractor={cp['extractor']}, {cp['per_core_MBps']['value']:.2f} MB/s/core (MEASURED), overhead x{cp['overhead_factor']['value']} (ASSUMPTION)",
        f"- WARC to process {cp['warc_bytes_to_process']['h']}; core-hours {cp['core_hours']['value']:,.0f}; VM-hours {cp['vm_hours']['value']:,.0f}",
        f"- **{cp['spot_nodes_needed']['value']:,} spot nodes ({cp['total_vcpus']['value']:,} vCPU)** for {cp['wall_clock_days']['value']} days; {cp['batch_tasks']['value']:,} Batch tasks",
        f"- Alt extractor ({cp['alt_extractor']['name']}, {cp['alt_extractor']['per_core_MBps']['value']:.2f} MB/s/core MEASURED): "
        f"{cp['alt_extractor']['spot_nodes_needed']['value']:,} nodes, spot {cp['alt_extractor']['compute_cost_spot_usd']['h']}",
        f"- If network-bound: {cp['nodes_if_network_bound']['value']:,} nodes ({cp['nodes_if_network_bound']['note']})",
        f"- Spot ${cp['spot_price_per_hour']['value']}/h -> **{cp['compute_cost_spot_usd']['h']}**; on-demand ${cp['ondemand_price_per_hour']['value']}/h -> {cp['compute_cost_ondemand_usd']['h']}; ACR {cp['acr_premium_cost_usd']['h']}",
        "", "## Network / egress",
        f"- Need ~{nw['aggregate_download_gbps_needed']['value']:.1f} Gbps sustained from Common Crawl ({nw['per_node_mbps']['value']:.0f} Mbps/node); measured single stream here {nw['single_stream_download_MBps_this_box']['value']:.0f} MB/s",
        f"- Azure transfer-in ${nw['azure_data_transfer_in_price']['value']}/GB (MEASURED list price)",
        *[f"- {n}" for n in nw["egress_notes"]],
        "", f"Prices: {o['price_snapshot']['source']} fetched {o['price_snapshot']['fetched_utc']} (UTC), region {o['price_snapshot']['region']}",
        "", "## Blockers", *[f"- {b}" for b in o["blockers"]], "",
    ]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default="9PB", help="9PB | 29PB | any bytes (e.g. 2.9e16)")
    p.add_argument("--label")
    p.add_argument("--kinds", default="warc,wat,wet,robotstxt,non200responses,cc-index",
                   help="which Common Crawl file kinds count toward the target")
    p.add_argument("--no-news", dest="include_news", action="store_false", help="exclude CC-NEWS")
    p.add_argument("--region", default="eastus"); p.add_argument("--vm", default="Standard_D32as_v5")
    p.add_argument("--days", type=float, default=30, help="target wall-clock for processing")
    p.add_argument("--extractor", choices=["fast", "quality"], default="quality",
                   help="fast = FastWARC+Resiliparse, quality = trafilatura (both measured)")
    p.add_argument("--overhead", type=float, default=2.0)
    p.add_argument("--fill", type=float, default=0.9, help="fraction of 5 PiB to fill per storage account")
    p.add_argument("--block-mib", type=int, default=8)
    p.add_argument("--files-per-task", type=int, default=10)
    p.add_argument("--node-gbps", type=float, default=1.0, help="sustained download per node from Common Crawl (ASSUMPTION)")
    p.add_argument("--strategy", choices=["A", "B"], default="B", help="which storage strategy feeds the Bicep params")
    a = p.parse_args(argv)
    o = plan(a)
    log(f"scale_plan {o['label']}: all CC {o['common_crawl_all']['bytes']['h']}, reaches={o['reaches_target']}, "
        f"shortfall {o['selection']['shortfall_bytes']['h']}, {o['compute']['spot_nodes_needed']['value']} spot nodes/"
        f"{a.days:g}d, spot compute {o['compute']['compute_cost_spot_usd']['h']} -> plans/plan_{o['label']}.md")


if __name__ == "__main__":
    sys.exit(main())
