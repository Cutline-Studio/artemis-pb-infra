"""
gapfill_sources.py - REAL public/open datasets that could add volume beyond
Common Crawl.  Every size below is a PUBLISHED figure copied from the linked
source (not measured by us), with the date the source states.  Only public web
data / online datasets are listed (no private or user-owned data).

Fields
  published_bytes : the publisher's own number (decimal bytes; PiB converted)
  new_vs_cc       : True if the bytes are largely NOT already inside Common Crawl
  text_like       : True if it is mostly text/code (directly LM-trainable)
  open_bulk_download : True if anyone can bulk-download it today without an agreement
  access          : how bulk access works (and blockers)
"""
PB = 10**15
PiB = 2**50

GAPFILL = [
    {
        "name": "Internet Archive (Wayback Machine + collections)",
        "published_bytes": 210 * PB, "published_as": "over 210 PB of unique data (2025 storage growth slide)",
        "source": "https://digitalpreservation.gov/meetings/DSA2026/0117_jonah_edwards_ia_dsa_2026.pdf",
        "open_bulk_download": False, "new_vs_cc": True, "text_like": False,
        "access": "NOT an open bulk-download dataset. Wayback WARCs are generally not publicly downloadable in bulk; "
                  "HPLT obtained 3.7 PB only via a research partnership. Requires an agreement with the Internet Archive; "
                  "many collections are copyrighted media.",
    },
    {
        "name": "NOAA Open Data Dissemination (NODD) - weather/climate/satellite",
        "published_bytes": 24 * PiB, "published_as": "more than 24 PB (PiB) as of September 2022",
        "source": "https://exa.ai/library/publication/mw23mc20x9g  (paper: 'NOAA Open Data Dissemination: Petabyte-scale Earth system data in the cloud'); registry: https://registry.opendata.aws/collab/noaa/",
        "open_bulk_download": True, "new_vs_cc": True, "text_like": False,
        "access": "Open, free (AWS/Azure/GCP buckets). Scientific gridded/satellite data - NOT web text; only useful for a "
                  "multimodal/scientific model. Some products now have 90-day rolling retention.",
    },
    {
        "name": "Software Heritage archive (source code)",
        "published_bytes": int(3.6 * PB), "published_as": "object storage about 3.6 PB, +~750 TB/year (Dec 2025 estimate)",
        "source": "https://docs.softwareheritage.org/sysadm/mirror-operations/planning.html",
        "open_bulk_download": False, "new_vs_cc": True, "text_like": True,
        "access": "Bulk copy only via an official mirror agreement or their published dataset exports; files carry "
                  "their own (mixed) open-source licences - licence filtering required. 26-28 billion small files.",
    },
    {
        "name": "Wikimedia Commons (freely licensed media)",
        "published_bytes": PiB, "published_as": "exceeded 1 PiB of current, undeleted files on 19 Aug 2026",
        "source": "https://commons.wikimedia.org/wiki/Commons:The_Commons_Log",
        "open_bulk_download": True, "new_vs_cc": True, "text_like": False,
        "access": "Open licences (CC-BY/CC-BY-SA/PD) per file - attribution metadata must be kept. Images/video/audio; "
                  "bulk via Wikimedia dumps/mirrors, polite rate limits.",
    },
    {
        "name": "HPLT v2 monolingual text (Internet Archive + Common Crawl derived)",
        "published_bytes": 21 * 10**12, "published_as": "21 TB deduplicated / 15 TB cleaned, 193 languages, ~7.6T words",
        "source": "https://hplt-project.org/datasets/v2.0  (download: https://data.hplt-project.org/two/)",
        "open_bulk_download": True, "new_vs_cc": True, "text_like": True,
        "access": "Open download (wget map files). Partly overlaps Common Crawl (0.8 PB of its 4.5 PB input was CC); the IA part (3.7 PB input) is new.",
    },
    {
        "name": "FineWeb (Hugging Face) - filtered Common Crawl text",
        "published_bytes": 44 * 10**12, "published_as": "15 trillion tokens, ~44 TB disk",
        "source": "https://arxiv.org/html/2406.17557v2  and  https://huggingface.co/datasets/HuggingFaceFW/fineweb",
        "open_bulk_download": True, "new_vs_cc": False, "text_like": True,
        "access": "Open (ODC-By). Already-processed subset OF Common Crawl - adds no new raw volume, but saves compute.",
    },
]
