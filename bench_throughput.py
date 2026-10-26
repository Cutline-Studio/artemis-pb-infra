#!/usr/bin/env python3
"""
bench_throughput.py - MEASURE real per-core processing throughput on one real
Common Crawl WARC file, so scale_plan.py does not have to guess.

Steps (all timed separately, single process = single core):
  1. download one real WARC (.warc.gz, ~1 GB) from https://data.commoncrawl.org/
  2. gunzip + parse every record with FastWARC
  3. HTML -> text for every text/html response with Resiliparse (fast extractor)
  4. HTML -> text on a sample with trafilatura (higher quality, much slower)

Writes plans/throughput_bench.json. Numbers are measured on THIS box (8 vCPU
shared sandbox); cloud VMs will differ - treat as an order-of-magnitude anchor.
"""
import datetime as dt, gzip, json, os, random, sys, time
import requests
from fastwarc.warc import ArchiveIterator, WarcRecordType
from resiliparse.extract.html2text import extract_plain_text
from resiliparse.parse.encoding import detect_encoding, bytes_to_str
import trafilatura

HERE = os.path.dirname(os.path.abspath(__file__))
ET = dt.timezone(dt.timedelta(hours=-4), "EDT")

def log(m):
    line = f"{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S %Z} {m}"; print(line, flush=True)
    open(os.path.join(HERE, "PROGRESS.log"), "a").write(line + "\n")

def main():
    crawl = sys.argv[1] if len(sys.argv) > 1 else "CC-MAIN-2026-39"
    pf = os.path.join(HERE, "cache", "paths", f"crawl-data_{crawl}__warc.paths.gz")
    paths = [l.strip() for l in gzip.open(pf, "rt")]
    path = random.Random("bench").choice(paths)
    url = "https://data.commoncrawl.org/" + path
    dst = os.path.join(HERE, "cache", "bench.warc.gz")
    log(f"bench: downloading {url}")
    t = time.time(); n = 0
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "artemis-pb-infra/1.0"}) as r, open(dst, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            f.write(chunk); n += len(chunk)
    dl_s = time.time() - t
    size = os.path.getsize(dst)

    t = time.time(); recs = 0; html = []; unc = 0
    for rec in ArchiveIterator(open(dst, "rb"), record_types=WarcRecordType.response, parse_http=True):
        body = rec.reader.read(); recs += 1; unc += len(body)
        ct = (rec.http_headers.get("Content-Type") or "") if rec.http_headers else ""
        if "html" in ct.lower():
            html.append(body)
    parse_s = time.time() - t

    t = time.time(); txt_bytes = 0
    for b in html:
        s = bytes_to_str(b, detect_encoding(b))
        txt_bytes += len(extract_plain_text(s, main_content=True).encode())
    resil_s = time.time() - t

    sample = random.Random(1).sample(html, min(500, len(html)))
    t = time.time(); tb = 0
    for b in sample:
        out = trafilatura.extract(b.decode("utf-8", "ignore")) or ""; tb += len(out.encode())
    traf_s = (time.time() - t) / len(sample) * len(html)   # extrapolated to whole file

    res = {
        "measured_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "file": url,
        "compressed_bytes": size, "uncompressed_payload_bytes": unc,
        "response_records": recs, "html_records": len(html),
        "download_seconds": dl_s, "download_MBps_this_box": size / dl_s / 1e6,
        "parse_seconds_1core": parse_s, "resiliparse_extract_seconds_1core": resil_s,
        "extracted_text_bytes_resiliparse": txt_bytes,
        "text_yield_fraction_of_compressed": txt_bytes / size,
        "trafilatura_seconds_1core_extrapolated": traf_s, "trafilatura_sample_docs": len(sample),
        "fast_path_compressed_MBps_per_core": size / (parse_s + resil_s) / 1e6,
        "quality_path_compressed_MBps_per_core": size / (parse_s + traf_s) / 1e6,
        "note": "single-core, this sandbox; trafilatura time extrapolated from a 500-doc sample",
    }
    json.dump(res, open(os.path.join(HERE, "plans", "throughput_bench.json"), "w"), indent=1)
    log(f"bench: {size/1e9:.2f} GB WARC; fast path {res['fast_path_compressed_MBps_per_core']:.1f} MB/s/core, "
        f"trafilatura path {res['quality_path_compressed_MBps_per_core']:.2f} MB/s/core, text yield {res['text_yield_fraction_of_compressed']*100:.1f}% of compressed")
    os.remove(dst)

if __name__ == "__main__":
    main()
