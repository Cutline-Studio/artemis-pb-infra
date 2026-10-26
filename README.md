# artemis-pb-infra

Scale-out plan + infrastructure-as-code to run the Artemis web-data stage modules
(`/workspace/artemis-9pb-web-data/stages`, built separately) at **9-29 PB** on Azure.
Public web data and open datasets only. **Nothing here deploys anything or spends money.**

| file | what it does |
|---|---|
| `cc_measure.py` | Measures ALL of Common Crawl: exact file counts from every `*.paths.gz`, real sizes via HTTP HEAD samples -> `plans/cc_measurement.json` |
| `bench_throughput.py` | Processes one real ~1 GB WARC on this box -> per-core MB/s + text yield -> `plans/throughput_bench.json` |
| `azure_prices.py` | Snapshot of real list prices from the Azure Retail Prices API -> `plans/azure_prices_eastus.json` |
| `gapfill_sources.py` | Published sizes (with links) of other open datasets, used only if Common Crawl falls short |
| `scale_plan.py` | `--target 9PB|29PB|<bytes>` -> crawls, files, storage accounts, nodes, wall-clock, costs, egress, blockers |
| `batch_job.py` | Builds Azure Batch jobs (one per crawl) and tasks (one per WARC shard range); **dry-run by default** |
| `bicep/` | ADLS Gen2 accounts (HNS, lifecycle cool/archive), managed identity, ACR, Batch Spot pool, optional AKS Spot pool |
| `plans/` | Real outputs: `plan_29PB.md/json` (primary), `plan_9PB.md/json` (secondary), Bicep parameter files, task lists |
| `PROGRESS.log` | Milestones with ET timestamps |

```bash
.venv/bin/python bench_throughput.py          # optional: re-measure throughput
python3 cc_measure.py                          # ~5 min, polite HEAD sampling
python3 scale_plan.py --target 29PB            # primary
python3 scale_plan.py --target 9PB             # secondary
python3 batch_job.py --plan plans/plan_9PB.json            # dry run
~/.azure/bin/bicep build bicep/main.bicep                   # compile only
# preview only, never applied by this repo:
# az deployment group what-if -g <rg> -f bicep/main.bicep -p plans/main.parameters.9PB.json
```

Labels in every plan: MEASURED, ESTIMATE, PUBLISHED, ASSUMPTION.
