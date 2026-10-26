# Artemis scale plan - target 29.000 PB  (generated 2026-10-08T17:53:15.668280-04:00)

Scope: public web data and open online datasets only (no private/user-owned data)

## Does all of Common Crawl reach the target?
- Crawls measured: **125** (MEASURED) - crawls with a published warc.paths.gz; collinfo lists 128; the 3 pre-2013 ARC-era crawls (2008-2009, 2009-2010, 2012) publish no WARC path lists (anonymous S3 listing is denied), so only their cc-index files are counted
- Files: **40,808,338** (MEASURED, exact from paths.gz)
- All of Common Crawl (warc, wat, wet, robotstxt, non200responses, cc-index, + CC-NEWS): **12.254 PB** +/- 29.3 TB (ESTIMATE)
- Raw WARC only: 8.627 PB (ESTIMATE)
  - warc: 8.571 PB
  - wat: 2.355 PB
  - wet: 0.985 PB
  - robotstxt: 18.3 TB
  - non200responses: 0.242 PB
  - cc-index: 27.2 TB
  - cc-news-warc: 55.7 TB
- **Reaches target: NO**; shortfall 16.746 PB
- Measurement errors excluded: 0

## Selection (newest first)
- Crawls needed: 128 ; files 40,808,338 ; bytes 12.254 PB +/- 29.3 TB
- First/last: ['CC-NEWS (all months)', 'CC-MAIN-2026-39', 'CC-MAIN-2026-34'] ... ['CC-MAIN-2009-2010', 'CC-MAIN-2008-2009']

## Gap-fill candidates (PUBLISHED sizes, not measured)
| dataset | published size | new vs CC | text? | open bulk? | access | source |
|---|---|---|---|---|---|---|
| Internet Archive (Wayback Machine + collections) | over 210 PB of unique data (2025 storage growth slide) | True | False | False | NOT an open bulk-download dataset. Wayback WARCs are generally not publicly downloadable in bulk; HPLT obtained 3.7 PB only via a research partnership. Requires an agreement with the Internet Archive; many collections are copyrighted media. | https://digitalpreservation.gov/meetings/DSA2026/0117_jonah_edwards_ia_dsa_2026.pdf |
| NOAA Open Data Dissemination (NODD) - weather/climate/satellite | more than 24 PB (PiB) as of September 2022 | True | False | True | Open, free (AWS/Azure/GCP buckets). Scientific gridded/satellite data - NOT web text; only useful for a multimodal/scientific model. Some products now have 90-day rolling retention. | https://exa.ai/library/publication/mw23mc20x9g  (paper: 'NOAA Open Data Dissemination: Petabyte-scale Earth system data in the cloud'); registry: https://registry.opendata.aws/collab/noaa/ |
| Software Heritage archive (source code) | object storage about 3.6 PB, +~750 TB/year (Dec 2025 estimate) | True | True | False | Bulk copy only via an official mirror agreement or their published dataset exports; files carry their own (mixed) open-source licences - licence filtering required. 26-28 billion small files. | https://docs.softwareheritage.org/sysadm/mirror-operations/planning.html |
| Wikimedia Commons (freely licensed media) | exceeded 1 PiB of current, undeleted files on 19 Aug 2026 | True | False | True | Open licences (CC-BY/CC-BY-SA/PD) per file - attribution metadata must be kept. Images/video/audio; bulk via Wikimedia dumps/mirrors, polite rate limits. | https://commons.wikimedia.org/wiki/Commons:The_Commons_Log |
| HPLT v2 monolingual text (Internet Archive + Common Crawl derived) | 21 TB deduplicated / 15 TB cleaned, 193 languages, ~7.6T words | True | True | True | Open download (wget map files). Partly overlaps Common Crawl (0.8 PB of its 4.5 PB input was CC); the IA part (3.7 PB input) is new. | https://hplt-project.org/datasets/v2.0  (download: https://data.hplt-project.org/two/) |
| FineWeb (Hugging Face) - filtered Common Crawl text | 15 trillion tokens, ~44 TB disk | False | True | True | Open (ODC-By). Already-processed subset OF Common Crawl - adds no new raw volume, but saves compute. | https://arxiv.org/html/2406.17557v2  and  https://huggingface.co/datasets/HuggingFaceFW/fineweb |

- Shortfall 16.746 PB. New open TEXT/code sources total 3.621 PB (closes gap: False). All openly downloadable new sources total 28.168 PB (closes gap: True). Including partnership-only Internet Archive: 241.768 PB. PUBLISHED sizes, not measured; raw bytes are not training value - media/scientific bytes yield little or no LM text.

## Storage (Azure ADLS Gen2, LRS)
- A) land raw: 12.254 PB -> **3 storage accounts** (ceil(bytes / (5 PiB x 0.9 fill)); default cap 5 PiB/account, raise via Azure Support); 1 needed for ingress in 30 days
  - monthly: Hot $218,481, Cool $173,470, Cold $41,085, Archive $11,298 (ESTIMATE from list prices)
  - write ops one-time $9,495; archive read-back $228,250
- B) stream, keep text: 0.801 PB upper bound -> 1 account(s); Hot $14,754/month

## Compute (Azure Batch, Spot)
- Standard_D32as_v5 (32 vCPU), extractor=quality, 1.17 MB/s/core (MEASURED), overhead x2.0 (ASSUMPTION)
- WARC to process 8.627 PB; core-hours 4,098,050; VM-hours 128,064
- **178 spot nodes (5,696 vCPU)** for 30 days; 857,746 Batch tasks
- Alt extractor (fast, 15.01 MB/s/core MEASURED): 14 nodes, spot $2,898
- If network-bound: 27 nodes (at 1.0 Gbps sustained per node from Common Crawl (ASSUMPTION))
- Spot $0.290474/h -> **$37,199**; on-demand $1.376/h -> $176,216; ACR $50

## Network / egress
- Need ~26.6 Gbps sustained from Common Crawl (150 Mbps/node); measured single stream here 84 MB/s
- Azure transfer-in $0.0/GB (MEASURED list price)
- Common Crawl is hosted by the AWS Open Data Sponsorship program; HTTPS download is free to the requester (commoncrawl.org/get-started). Azure charges $0 for data transfer IN (price API above).
- Cross-cloud pulls at this scale are throttled: our own measurement run got HTTP 403/503 rate-limit responses at only 4-8 concurrent HEAD requests. Multi-Gbps sustained pulls need coordination with Common Crawl.
- Common Crawl recommends processing IN AWS us-east-1 next to the data. Doing extraction there and shipping only the text (~9.3% of WARC bytes) to Azure cuts transfer ~11x; AWS egress on that text is billed by AWS (AWS price NOT fetched here - check https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer).
- Azure East US is also in Virginia, so latency to us-east-1 is low, but it is still internet/cross-cloud traffic.

Prices: https://prices.azure.com/api/retail/prices fetched 2026-10-08T21:48:15.113573+00:00 (UTC), region eastus

## Blockers
- All measured Common Crawl data (12.254 PB) is BELOW the 29.000 PB target; shortfall 16.746 PB. Open TEXT data cannot close this gap; only media/scientific archives or partnership data (Internet Archive) are that large.
- Default Azure limits: 5 PiB/storage account, 250 accounts/region, 60 Gbps ingress/account, low default Batch/Spot vCPU quota -> support requests needed before any real run.
- Common Crawl rate limiting (observed 403/503) caps cross-cloud throughput; plan for AWS-side extraction or a CC agreement.
- Spot capacity for thousands of vCPUs in one region is not guaranteed; spread across regions/VM sizes.
- Licensing: Common Crawl ToU allows use, but page copyrights remain with owners - licence/robots/opt-out filtering is required.
