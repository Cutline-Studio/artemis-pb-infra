# Artemis scale plan - target 9.000 PB  (generated 2026-10-08T17:53:15.832871-04:00)

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
- **Reaches target: YES**; shortfall 0.0 TB
- Measurement errors excluded: 0

## Selection (newest first)
- Crawls needed: 80 ; files 31,371,822 ; bytes 9.040 PB +/- 13.2 TB
- First/last: ['CC-NEWS (all months)', 'CC-MAIN-2026-39', 'CC-MAIN-2026-34'] ... ['CC-MAIN-2018-26', 'CC-MAIN-2018-22']

## Storage (Azure ADLS Gen2, LRS)
- A) land raw: 9.000 PB -> **2 storage accounts** (ceil(bytes / (5 PiB x 0.9 fill)); default cap 5 PiB/account, raise via Azure Support); 1 needed for ingress in 30 days
  - monthly: Hot $160,596, Cool $127,405, Cold $30,175, Archive $8,298 (ESTIMATE from list prices)
  - write ops one-time $6,974; archive read-back $167,638
- B) stream, keep text: 0.602 PB upper bound -> 1 account(s); Hot $11,204/month

## Compute (Azure Batch, Spot)
- Standard_D32as_v5 (32 vCPU), extractor=quality, 1.17 MB/s/core (MEASURED), overhead x2.0 (ASSUMPTION)
- WARC to process 6.478 PB; core-hours 3,077,435; VM-hours 96,170
- **134 spot nodes (4,288 vCPU)** for 30 days; 631,119 Batch tasks
- Alt extractor (fast, 15.01 MB/s/core MEASURED): 11 nodes, spot $2,176
- If network-bound: 20 nodes (at 1.0 Gbps sustained per node from Common Crawl (ASSUMPTION))
- Spot $0.290474/h -> **$27,935**; on-demand $1.376/h -> $132,330; ACR $50

## Network / egress
- Need ~20.0 Gbps sustained from Common Crawl (149 Mbps/node); measured single stream here 84 MB/s
- Azure transfer-in $0.0/GB (MEASURED list price)
- Common Crawl is hosted by the AWS Open Data Sponsorship program; HTTPS download is free to the requester (commoncrawl.org/get-started). Azure charges $0 for data transfer IN (price API above).
- Cross-cloud pulls at this scale are throttled: our own measurement run got HTTP 403/503 rate-limit responses at only 4-8 concurrent HEAD requests. Multi-Gbps sustained pulls need coordination with Common Crawl.
- Common Crawl recommends processing IN AWS us-east-1 next to the data. Doing extraction there and shipping only the text (~9.3% of WARC bytes) to Azure cuts transfer ~11x; AWS egress on that text is billed by AWS (AWS price NOT fetched here - check https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer).
- Azure East US is also in Virginia, so latency to us-east-1 is low, but it is still internet/cross-cloud traffic.

Prices: https://prices.azure.com/api/retail/prices fetched 2026-10-08T21:48:15.113573+00:00 (UTC), region eastus

## Blockers
- Default Azure limits: 5 PiB/storage account, 250 accounts/region, 60 Gbps ingress/account, low default Batch/Spot vCPU quota -> support requests needed before any real run.
- Common Crawl rate limiting (observed 403/503) caps cross-cloud throughput; plan for AWS-side extraction or a CC agreement.
- Spot capacity for thousands of vCPUs in one region is not guaranteed; spread across regions/VM sizes.
- Licensing: Common Crawl ToU allows use, but page copyrights remain with owners - licence/robots/opt-out filtering is required.
