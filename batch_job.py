#!/usr/bin/env python3
"""
batch_job.py - turn a scale plan into Azure Batch jobs/tasks that run the
Artemis stage modules (built separately in /workspace/artemis-9pb-web-data/stages).

Layout (one Batch job per crawl, so per-job task counts stay manageable):
  per-shard task  (one per --files-per-task WARC files = one manifest shard range)
      s2_acquire -> s3_extract -> s4_filter -> s6_pii        (streams WARCs, keeps text only)
      output -> https://<acct>.blob.core.windows.net/scrubbed/<crawl>/<range>/
  dedup task      (one per crawl partition, depends on all its shard tasks via taskIdRanges)
      s5_dedup over scrubbed/<crawl>/   -> dedup/<crawl>/
  tokenize task   (depends on dedup)   s7_tokenize -> tokens/<crawl>/
  validate task   (depends on tokenize) token counts, samples, licence fields -> manifests/<crawl>/

Known limits (be honest before running for real):
  * the per-crawl dedup task pulls a whole crawl's scrubbed text (~several TB at full scale) onto ONE node;
    production needs a split MinHash design (signature tasks -> LSH band buckets -> cluster resolve).
  * CC-NEWS is counted in the scale plan but not scheduled here (stage s2_acquire reads the CC-MAIN manifest).
  * cross-crawl dedup is not scheduled (only within-crawl).

DEFAULT IS DRY-RUN: prints the job/task list and writes plans/batch_tasks_<label>.sample.jsonl.
Nothing is submitted unless you pass BOTH --submit and --i-accept-azure-charges
(and have azure-batch + azure-identity installed and are logged in).

Usage
  python batch_job.py --plan plans/plan_29PB.json                 # dry run
  python batch_job.py --plan plans/plan_9PB.json --show 20        # dry run, show 20 tasks
  python batch_job.py --plan plans/plan_9PB.json --submit --i-accept-azure-charges \\
        --batch-url https://<acct>.<region>.batch.azure.com --accounts artemisd000xxxx,artemisd001xxxx \\
        --identity-id /subscriptions/.../userAssignedIdentities/artemis-pipeline-id --acr <name>.azurecr.io
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATHS = os.path.join(HERE, "cache", "paths")
ET = dt.timezone(dt.timedelta(hours=-4), "EDT")

# Command templates match the CLIs of the stage modules in artemis-9pb-web-data/stages
# (s2_acquire --crawl --kind --local --count --out-dir ; s3/s4/s6 <in> <out> ; s5_dedup <inputs> --out-dir ;
#  s7_tokenize <inputs> --out-dir).  Override with --templates <json> if those CLIs change.
TEMPLATES = {
    "shard": (
        "set -euo pipefail; W=$AZ_BATCH_TASK_WORKING_DIR; mkdir -p $W/raw $W/out; "
        "python -m stages.s2_acquire --crawl {crawl} --kind warc --local {start} --count {count} --out-dir $W/raw; "
        "for f in $W/raw/*; do b=$(basename $f); "
        "python -m stages.s3_extract $f $W/x_$b.jsonl && "
        "python -m stages.s4_filter $W/x_$b.jsonl $W/f_$b.jsonl --langs {langs} && "
        "python -m stages.s6_pii $W/f_$b.jsonl $W/out/$b.jsonl.gz && rm -f $f $W/x_$b.jsonl $W/f_$b.jsonl; done"
    ),
    "dedup": "set -euo pipefail; W=$AZ_BATCH_TASK_WORKING_DIR; python -m stages.s5_dedup $W/in/*.jsonl.gz --out-dir $W/out --threshold 0.8",
    "tokenize": "set -euo pipefail; W=$AZ_BATCH_TASK_WORKING_DIR; python -m stages.s7_tokenize $W/in/* --out-dir $W/out --encoding o200k_base",
    # NOTE: stages/ had no validate module yet (s1..s7 only, checked 2026-10-08 17:51 ET); override via --templates
    "validate": "set -euo pipefail; W=$AZ_BATCH_TASK_WORKING_DIR; python -m stages.s8_validate $W/in --out $W/out/validate.json",
}


def log(msg):
    line = f"{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S %Z} {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "PROGRESS.log"), "a") as f:
        f.write(line + "\n")


def warc_count(crawl: str) -> int:
    fn = os.path.join(PATHS, f"crawl-data_{crawl}__warc.paths.gz")
    if not os.path.exists(fn):
        raise SystemExit(f"missing {fn}; run cc_measure.py first")
    with gzip.open(fn, "rt") as f:
        return sum(1 for l in f if l.strip())


def blob_url(account, container, prefix=""):
    return f"https://{account}.blob.core.windows.net/{container}" + (f"/{prefix}" if prefix else "")


def split_blob_url(url):
    """https://acct.blob.core.windows.net/container/some/prefix/ -> (container_url, prefix)"""
    scheme, _, rest = url.partition("://")
    host, _, path = rest.partition("/")
    container, _, prefix = path.partition("/")
    return f"{scheme}://{host}/{container}", prefix


def build(a):
    plan = json.load(open(a.plan))
    label = plan["label"]
    crawls = [c for c in plan["selection"]["crawl_ids"] if c.startswith("CC-MAIN")]
    if a.limit_crawls:
        crawls = crawls[: a.limit_crawls]
    accounts = a.accounts.split(",") if a.accounts else [f"<storage-account-{i}>" for i in
                                                          range(plan["storage"]["strategy_B_stream_keep_text"]["storage_accounts"]["value"])]
    tpl = dict(TEMPLATES)
    if a.templates:
        tpl.update(json.load(open(a.templates)))
    skipped = [c for c in crawls if not os.path.exists(os.path.join(PATHS, f"crawl-data_{c}__warc.paths.gz"))]
    if skipped:
        print(f"skipping {len(skipped)} crawl(s) with no published warc.paths.gz (ARC era): {skipped}")
    crawls = [c for c in crawls if c not in skipped]
    jobs = []
    for ci, crawl in enumerate(crawls):
        acct = accounts[ci % len(accounts)]
        n = warc_count(crawl)
        tasks, tid = [], 0
        for start in range(0, n, a.files_per_task):
            cnt = min(a.files_per_task, n - start)
            rng = f"{start:06d}-{start + cnt - 1:06d}"
            tasks.append({
                "id": str(tid), "kind": "shard", "range": rng,
                "command": tpl["shard"].format(crawl=crawl, start=start, count=cnt, langs=a.langs),
                "output": blob_url(acct, "scrubbed", f"{crawl}/{rng}/"), "depends_on": None,
            })
            tid += 1
        last_shard = tid - 1
        tasks.append({"id": str(tid), "kind": "dedup", "range": "all",
                      "inputs": blob_url(acct, "scrubbed", f"{crawl}/"), "command": tpl["dedup"],
                      "output": blob_url(acct, "dedup", f"{crawl}/"), "depends_on": {"taskIdRanges": [[0, last_shard]]}})
        tasks.append({"id": str(tid + 1), "kind": "tokenize", "range": "all",
                      "inputs": blob_url(acct, "dedup", f"{crawl}/"), "command": tpl["tokenize"],
                      "output": blob_url(acct, "tokens", f"{crawl}/"), "depends_on": {"taskIds": [str(tid)]}})
        tasks.append({"id": str(tid + 2), "kind": "validate", "range": "all",
                      "inputs": blob_url(acct, "tokens", f"{crawl}/"), "command": tpl["validate"],
                      "output": blob_url(acct, "manifests", f"{crawl}/"), "depends_on": {"taskIds": [str(tid + 1)]}})
        jobs.append({"job_id": f"artemis-{label.lower()}-{crawl.lower()}", "crawl": crawl, "account": acct,
                     "warc_files": n, "tasks": tasks})
    return plan, jobs


def dry_run(a, plan, jobs):
    total = sum(len(j["tasks"]) for j in jobs)
    print(f"DRY RUN - nothing submitted.  plan={a.plan}  jobs={len(jobs)}  tasks={total:,}  "
          f"(files/task={a.files_per_task}, image={a.image})")
    for j in jobs[: a.show_jobs]:
        print(f"\njob {j['job_id']}  crawl={j['crawl']}  warc_files={j['warc_files']:,}  tasks={len(j['tasks']):,}  account={j['account']}")
        show = j["tasks"][: a.show] + j["tasks"][-3:]
        for t in show:
            dep = t["depends_on"] or ""
            print(f"  task {t['id']:>6} {t['kind']:<8} {t['range']:<14} -> {t['output']}  {dep}")
            if a.verbose:
                print("     $ " + t["command"])
    if len(jobs) > a.show_jobs:
        print(f"\n... {len(jobs) - a.show_jobs} more jobs")
    out = os.path.join(HERE, "plans", f"batch_tasks_{plan['label']}.sample.jsonl")
    with open(out, "w") as f:
        for j in jobs:
            for t in j["tasks"][:3] + j["tasks"][-3:]:
                f.write(json.dumps({"job_id": j["job_id"], **t}) + "\n")
    summary = {"plan": a.plan, "jobs": len(jobs), "tasks": total, "files_per_task": a.files_per_task,
               "per_job": [{"job_id": j["job_id"], "warc_files": j["warc_files"], "tasks": len(j["tasks"])} for j in jobs]}
    json.dump(summary, open(os.path.join(HERE, "plans", f"batch_tasks_{plan['label']}.summary.json"), "w"), indent=1)
    log(f"batch_job dry-run {plan['label']}: {len(jobs)} jobs, {total:,} tasks -> plans/batch_tasks_{plan['label']}.summary.json")


def submit(a, plan, jobs):  # pragma: no cover - never run in this repo; written for azure-batch 14.x (track-1 API), UNTESTED
    if not a.i_accept_azure_charges:
        raise SystemExit("refusing to submit without --i-accept-azure-charges")
    from azure.batch import BatchServiceClient
    from azure.batch import models as bm
    from azure.identity import DefaultAzureCredential

    cred = DefaultAzureCredential()

    class _Cred:  # adapt azure-identity token to the track-1 batch client
        def signed_session(self, session=None):
            import requests
            s = session or requests.Session()
            s.headers["Authorization"] = "Bearer " + cred.get_token("https://batch.core.windows.net/.default").token
            return s

    client = BatchServiceClient(_Cred(), batch_url=a.batch_url)
    ident = bm.ComputeNodeIdentityReference(resource_id=a.identity_id)
    for j in jobs:
        client.job.add(bm.JobAddParameter(id=j["job_id"], pool_info=bm.PoolInformation(pool_id=a.pool),
                                          uses_task_dependencies=True,
                                          on_all_tasks_complete=bm.OnAllTasksComplete.terminate_job))
        batch = []
        for t in j["tasks"]:
            res = []
            if t.get("inputs"):
                c_url, prefix = split_blob_url(t["inputs"])
                res = [bm.ResourceFile(storage_container_url=c_url, blob_prefix=prefix,
                                       file_path="in", identity_reference=ident)]
            o_url, o_prefix = split_blob_url(t["output"])
            out = [bm.OutputFile(file_pattern="out/**/*", destination=bm.OutputFileDestination(
                       container=bm.OutputFileBlobContainerDestination(container_url=o_url, path=o_prefix,
                           identity_reference=ident)),
                       upload_options=bm.OutputFileUploadOptions(upload_condition=bm.OutputFileUploadCondition.task_success))]
            dep = None
            if t["depends_on"]:
                dep = bm.TaskDependencies(task_ids=t["depends_on"].get("taskIds"),
                                          task_id_ranges=[bm.TaskIdRange(start=s, end=e) for s, e in t["depends_on"].get("taskIdRanges", [])] or None)
            batch.append(bm.TaskAddParameter(
                id=t["id"], command_line="/bin/bash -c " + shlex.quote(t["command"]),
                container_settings=bm.TaskContainerSettings(image_name=f"{a.acr}/{a.image}",
                                                            working_directory=bm.ContainerWorkingDirectory.task_working_directory,
                                                            registry=bm.ContainerRegistry(registry_server=a.acr, identity_reference=ident)),
                resource_files=res or None, output_files=out, depends_on=dep,
                constraints=bm.TaskConstraints(max_task_retry_count=3)))
            if len(batch) == 100:  # Batch API max 100 tasks per add_collection call
                client.task.add_collection(j["job_id"], batch); batch = []
        if batch:
            client.task.add_collection(j["job_id"], batch)
        log(f"batch_job: submitted {j['job_id']} ({len(j['tasks'])} tasks)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True)
    p.add_argument("--files-per-task", type=int, default=10)
    p.add_argument("--langs", default="eng")
    p.add_argument("--accounts", help="comma-separated storage account names (Bicep output storageAccounts)")
    p.add_argument("--templates", help="JSON file overriding command templates")
    p.add_argument("--image", default="artemis/stages:latest")
    p.add_argument("--limit-crawls", type=int, default=0)
    p.add_argument("--show", type=int, default=5); p.add_argument("--show-jobs", type=int, default=2)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--submit", action="store_true", help="actually submit (costs money)")
    p.add_argument("--i-accept-azure-charges", action="store_true")
    p.add_argument("--batch-url"); p.add_argument("--pool", default="stages-spot")
    p.add_argument("--identity-id"); p.add_argument("--acr")
    a = p.parse_args(argv)
    plan, jobs = build(a)
    if a.submit:
        submit(a, plan, jobs)
    else:
        dry_run(a, plan, jobs)


if __name__ == "__main__":
    sys.exit(main())
