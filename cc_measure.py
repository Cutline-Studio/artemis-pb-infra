#!/usr/bin/env python3
"""
cc_measure.py - measure how big ALL of Common Crawl really is (public data only).

Sources (public, no credentials):
  * crawl list ............ https://index.commoncrawl.org/collinfo.json
  * per-crawl file lists .. https://data.commoncrawl.org/crawl-data/<ID>/<kind>.paths.gz
  * CC-NEWS file lists .... https://data.commoncrawl.org/crawl-data/CC-NEWS/<YYYY>/<MM>/warc.paths.gz
  * file sizes ............ HTTP HEAD (Content-Length) on a seeded random sample

What is MEASURED vs ESTIMATED
  measured : crawl ids, exact file COUNT per (crawl, kind) (from paths.gz),
             exact byte size of every HEAD-sampled file.
  estimated: section bytes = file_count x mean(sampled sizes), with a 95% CI
             (normal approx; t-ish widening is not applied -> treat as rough).

Output: plans/cc_measurement.json (+ cache/ for downloaded path lists).
Polite: limited concurrency, retries with backoff on 503 "SlowDown".
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import math
import os
import random
import statistics
import sys
import time

import requests

COLLINFO = "https://index.commoncrawl.org/collinfo.json"
BASE = "https://data.commoncrawl.org/"
KINDS = ["warc", "wat", "wet", "robotstxt", "non200responses", "cc-index"]
UA = "artemis-pb-infra/1.0 (+scale planning; polite HEAD sampling)"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "paths")
ET = dt.timezone(dt.timedelta(hours=-4), "EDT")


def log(msg: str) -> None:
    line = f"{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S %Z} {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "PROGRESS.log"), "a") as f:
        f.write(line + "\n")


_S = requests.Session()
_S.headers["User-Agent"] = UA
_S.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))


def req(method, url, tries=10, **kw):
    delay = 2.0
    for i in range(tries):
        try:
            r = _S.request(method, url, timeout=60, **kw)
            # 403/429/5xx from data.commoncrawl.org are rate-limit signals -> back off
            if r.status_code in (403, 429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} for {url}")
            return r
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(delay + random.random())
            delay = min(delay * 2, 120)


def paths(prefix: str, kind: str):
    """Return list of file paths for <prefix>/<kind>.paths.gz, or None if 404."""
    os.makedirs(CACHE, exist_ok=True)
    fn = os.path.join(CACHE, prefix.replace("/", "_") + f"__{kind}.paths.gz")
    if not os.path.exists(fn):
        r = req("GET", f"{BASE}{prefix}/{kind}.paths.gz")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with open(fn + ".tmp", "wb") as f:
            f.write(r.content)
        os.replace(fn + ".tmp", fn)
    with gzip.open(fn, "rt") as f:
        return [l.strip() for l in f if l.strip()]


_SIZES: dict = {}
_SIZES_FN = os.path.join(HERE, "cache", "head_sizes.json")
if os.path.exists(_SIZES_FN):
    _SIZES.update(json.load(open(_SIZES_FN)))


def head_size(path: str):
    """Real Content-Length via HTTP HEAD (cached in cache/head_sizes.json)."""
    if path in _SIZES:
        return _SIZES[path]
    time.sleep(0.05)  # politeness
    r = req("HEAD", BASE + path)
    if r.status_code != 200:
        return None
    _SIZES[path] = int(r.headers.get("Content-Length", 0))
    return _SIZES[path]


def save_sizes():
    os.makedirs(os.path.dirname(_SIZES_FN), exist_ok=True)
    json.dump(_SIZES, open(_SIZES_FN + ".tmp", "w"))
    os.replace(_SIZES_FN + ".tmp", _SIZES_FN)


def measure_section(prefix, kind, k, seed, pool):
    p = paths(prefix, kind)
    if p is None:
        return None
    rnd = random.Random(f"{seed}:{prefix}:{kind}")
    sample = p if len(p) <= k else rnd.sample(p, k)
    sizes = [s for s in pool.map(head_size, sample) if s is not None]
    n = len(p)
    if not sizes:
        return {"files": n, "sampled": 0, "est_bytes": None}
    mean = statistics.fmean(sizes)
    sd = statistics.stdev(sizes) if len(sizes) > 1 else 0.0
    exact = len(sizes) == n
    ci = 0.0 if exact else 1.96 * sd / math.sqrt(len(sizes)) * n
    return {
        "files": n,                      # MEASURED (exact, from paths.gz)
        "sampled": len(sizes),           # MEASURED
        "sample_mean_bytes": mean,       # MEASURED
        "sample_min_bytes": min(sizes),
        "sample_max_bytes": max(sizes),
        "est_bytes": mean * n,           # ESTIMATE (exact if every file sampled)
        "est_ci95_bytes": ci,            # ESTIMATE
        "exact": exact,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--warc-sample", type=int, default=12, help="HEAD samples per crawl for WARC (dominant bytes)")
    ap.add_argument("--other-sample", type=int, default=5, help="HEAD samples per crawl for other kinds")
    ap.add_argument("--news-sample", type=int, default=4, help="HEAD samples per CC-NEWS month")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", default="artemis-2026")
    ap.add_argument("--limit-crawls", type=int, default=0, help="debug: only newest N crawls")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "plans", "cc_measurement.json"))
    a = ap.parse_args(argv)

    started = dt.datetime.now(dt.timezone.utc)
    crawls = req("GET", COLLINFO).json()
    if a.limit_crawls:
        crawls = crawls[: a.limit_crawls]
    log(f"cc_measure: {len(crawls)} crawls listed in collinfo.json; measuring {KINDS}")

    out = {"generated_utc": started.isoformat(), "sources": {"collinfo": COLLINFO, "data": BASE},
           "method": "files counted exactly from *.paths.gz; bytes = count x mean(HEAD Content-Length sample)",
           "crawls": {}, "cc_news": {}}
    with cf.ThreadPoolExecutor(a.workers) as pool:
        for i, c in enumerate(crawls):
            cid = c["id"]
            rec = {"name": c.get("name"), "from": c.get("from"), "to": c.get("to"), "kinds": {}}
            for kind in KINDS:
                k = a.warc_sample if kind == "warc" else a.other_sample
                try:
                    m = measure_section(f"crawl-data/{cid}", kind, k, a.seed, pool)
                except Exception as e:  # noqa: BLE001
                    m = {"error": str(e)}
                if m is not None:
                    rec["kinds"][kind] = m
            if not rec["kinds"].get("warc"):
                rec["note"] = ("no warc.paths.gz published (pre-2013 ARC-era crawl stored under "
                               "crawl-001/crawl-002/parse-output); NOT measured, excluded from totals")
            out["crawls"][cid] = rec
            save_sizes()
            tb = sum((v.get("est_bytes") or 0) for v in rec["kinds"].values()) / 1e12
            if (i + 1) % 10 == 0 or i == len(crawls) - 1:
                log(f"cc_measure: {i+1}/{len(crawls)} crawls done (last {cid} ~{tb:,.0f} TB est)")
            print(f"  {cid}: ~{tb:,.1f} TB est", flush=True)

        if not a.no_news:
            now = dt.date.today()
            y, mth = 2016, 8
            while (y, mth) <= (now.year, now.month):
                key = f"{y}/{mth:02d}"
                try:
                    m = measure_section(f"crawl-data/CC-NEWS/{key}", "warc", a.news_sample, a.seed, pool)
                except Exception as e:  # noqa: BLE001
                    m = {"error": str(e)}
                if m:
                    out["cc_news"][key] = m
                mth += 1
                if mth == 13:
                    y, mth = y + 1, 1
            save_sizes()
            log(f"cc_news: {len(out['cc_news'])} monthly file lists measured")

    # totals
    tot = {}
    for rec in out["crawls"].values():
        for kind, m in rec["kinds"].items():
            if m.get("est_bytes"):
                t = tot.setdefault(kind, {"files": 0, "est_bytes": 0.0, "ci95_var": 0.0, "crawls": 0})
                t["files"] += m["files"]; t["est_bytes"] += m["est_bytes"]
                t["ci95_var"] += m["est_ci95_bytes"] ** 2; t["crawls"] += 1
    news = [m for m in out["cc_news"].values() if m.get("est_bytes")]
    if news:
        tot["cc-news-warc"] = {"files": sum(m["files"] for m in news),
                               "est_bytes": sum(m["est_bytes"] for m in news),
                               "ci95_var": sum(m["est_ci95_bytes"] ** 2 for m in news), "months": len(news)}
    for t in tot.values():
        t["est_ci95_bytes"] = math.sqrt(t.pop("ci95_var"))
    errors = [f"{c}/{k}: {m['error']}" for c, r in out["crawls"].items() for k, m in r["kinds"].items() if "error" in m]
    errors += [f"CC-NEWS/{k}: {m['error']}" for k, m in out["cc_news"].items() if "error" in m]
    out["errors"] = errors
    if errors:
        log(f"cc_measure: WARNING {len(errors)} sections failed (excluded from totals); rerun to fill from cache")
    grand = sum(t["est_bytes"] for t in tot.values())
    grand_ci = math.sqrt(sum(t["est_ci95_bytes"] ** 2 for t in tot.values()))
    out["totals"] = {"by_kind": tot, "all_est_bytes": grand, "all_est_ci95_bytes": grand_ci,
                     "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    log(f"cc_measure: DONE all-of-Common-Crawl est {grand/1e15:.3f} PB (+/-{grand_ci/1e15:.3f} PB 95% CI) -> {a.out}")


if __name__ == "__main__":
    sys.exit(main())
