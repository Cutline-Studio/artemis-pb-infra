"""pilot_wet.py - size a 1-crawl, WET-only (text route) pilot from MEASURED inputs.

scale_plan.py models compute on WARC bytes + HTML-extraction throughput. The WET route
skips HTML extraction, so its compute is sized here from the real 20-WET-file run of the
stage modules (artemis-9pb-web-data/logs/make_sample_data.log + data/stats/RUN_SUMMARY.json).
Reads only local files; creates nothing in the cloud.  Writes plans/pilot_<crawl>_WET.json.
"""
import argparse, json, math, os, re, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); PLANS = os.path.join(HERE, "plans")
WD = "/workspace/artemis-9pb-web-data"; GiB = 2**30

def stage_seconds(log):
    ts = [(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2).split()[0])
          for m in re.finditer(r"^\[(\S+ \S+) ET\] (.+)$", open(log).read(), re.M)]
    return {ts[i][1]: (ts[i + 1][0] - ts[i][0]).total_seconds() for i in range(len(ts) - 1)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--crawl", default="CC-MAIN-2026-39"); p.add_argument("--nodes", type=int, default=3)
    p.add_argument("--vm", default="Standard_D32as_v5"); p.add_argument("--vcpus", type=int, default=32)
    p.add_argument("--overhead", type=float, default=2.0); p.add_argument("--box-cores", type=int, default=8)
    a = p.parse_args()
    meas = json.load(open(os.path.join(PLANS, "cc_measurement.json")))
    wet = meas["crawls"][a.crawl]["kinds"]["wet"]
    run = json.load(open(os.path.join(WD, "data/stats/RUN_SUMMARY.json")))
    secs = stage_seconds(os.path.join(WD, "logs/make_sample_data.log"))
    raw = run["disk_bytes"]["raw"]; scale = wet["est_bytes"] / raw
    proc_s = sum(v for k, v in secs.items() if k in ("s3", "s4", "s5", "s6", "s7", "s8"))
    core_s = proc_s * a.box_cores                     # whole box billed while s5/s7 run single-threaded (conservative)
    rate = raw / core_s
    core_h = wet["est_bytes"] / rate / 3600 * a.overhead
    vm_h = core_h / a.vcpus
    pr = json.load(open(os.path.join(PLANS, "azure_prices_eastus.json")))["groups"]
    vm = {m["meterName"]: m["retailPrice"] for m in pr["vm"]["items"]
          if m["productName"] == "Virtual Machines Dasv5 Series" and m["armSkuName"] == a.vm}
    st = {m["skuName"]: m["retailPrice"] for m in pr["adls_storage"]["items"]
          if m["meterName"].endswith("Data Stored") and m["tierMinimumUnits"] == 0}
    wr = [m["retailPrice"] for m in pr["adls_storage"]["items"] if m["meterName"] == "Hot Write Operations"][0]
    acr_basic = [m["retailPrice"] for m in pr["acr"]["items"] if m["meterName"] == "Basic Registry Unit"][0]
    tr = run.get("s7_tokenize") or next(v for v in run.values() if isinstance(v, dict) and v.get("stage") == "s7_tokenize")
    text_b = run["disk_bytes"]["s6_pii"] * scale; tok_b = run["disk_bytes"]["final"] * scale
    out_gib = (text_b + tok_b) / GiB
    wall_h = vm_h / a.nodes
    o = {
      "label": "ESTIMATE unless marked", "generated_et": dt.datetime.now().isoformat(timespec="seconds"),
      "crawl": a.crawl, "wet_files_MEASURED": wet["files"], "wet_bytes_ESTIMATE": wet["est_bytes"],
      "basis_MEASURED": {"run_raw_wet_bytes": raw, "box_cores": a.box_cores, "stage_wall_seconds": secs,
                         "rate_MBps_per_core_conservative": rate / 1e6},
      "overhead_ASSUMPTION": a.overhead, "core_hours": core_h, "vm_hours": vm_h, "vm": a.vm,
      "nodes": a.nodes, "vcpus_total": a.nodes * a.vcpus, "wall_clock_hours": wall_h,
      "spot_price_h_MEASURED": vm.get("D32as v5 Spot"), "lowpri_price_h_MEASURED": vm.get("D32as v5 Low Priority"),
      "ondemand_price_h_MEASURED": vm.get("D32as v5"),
      "compute_spot_usd": vm_h * vm["D32as v5 Spot"], "compute_ondemand_usd": vm_h * vm["D32as v5"],
      "output": {"scrubbed_text_bytes": text_b, "token_shard_bytes": tok_b,
                 "docs_upper_bound": tr["total_docs"] * scale, "tokens_upper_bound": tr["total_tokens_incl_eot"] * scale,
                 "note": "scaled from 20 files; crawl-wide dedup will remove more, so these are upper bounds"},
      "storage_month_usd": {t: out_gib * st[f"{t} LRS"] for t in ("Hot", "Cool", "Cold", "Archive")},
      "storage_month_text_only_hot_usd": text_b / GiB * st["Hot LRS"],
      "write_ops_usd": (text_b + tok_b) / (8 * 2**20) / 1e4 * wr,
      "acr_basic_usd": acr_basic * max(1, math.ceil(wall_h / 24) + 1),
      "download_gbps_avg": wet["est_bytes"] * 8 / (wall_h * 3600) / 1e9,
    }
    o["one_time_total_spot_usd"] = o["compute_spot_usd"] + o["write_ops_usd"] + o["acr_basic_usd"]
    fn = os.path.join(PLANS, f"pilot_{a.crawl}_WET_{a.nodes}nodes.json"); json.dump(o, open(fn, "w"), indent=1)
    print(json.dumps(o, indent=1)); print("->", fn)

if __name__ == "__main__":
    main()
