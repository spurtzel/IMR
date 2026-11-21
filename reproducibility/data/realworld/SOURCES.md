# Real-world tables

Each file holds 100,000 consecutive rows of a public data set, in time order, with a running `id` (1 to 100,000) and
the timestamp column `time`. The experiment (`reproducibility/run_trino_realworld.sh`) checks each file against the
SHA-256 in `manifest.json` before it loads it. The 15 queries are in `specs/<table>/Q1.json` to `Q5.json`.

| File | Source | License line of the source | Attribution |
|---|---|---|---|
| `taxi_100000.parquet` | New York City Taxi and Limousine Commission, *TLC Trip Record Data*, <https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page> | none stated on the page; the terms of use of nyc.gov apply | New York City Taxi and Limousine Commission (TLC) |
| `chicago_100000.parquet` | Chicago Police Department, *Crimes - 2001 to Present*, City of Chicago Data Portal, <https://data.cityofchicago.org/Public-Safety/Crimes-2001-to-Present/ijzp-q8t2> | "See Terms of Use" | Chicago Police Department |
| `ecommerce_100000.parquet` | Michael Kechinov, *eCommerce Behavior Data from Multi-category Store*, Kaggle, <https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store> | "Data files © Original Authors" | REES46 Marketing Platform, <https://rees46.com/> |

Rows taken (the column `real_time` is the source's own timestamp, on which the queries state their time windows; `time`
and `ts` are the running timestamp that orders the rows and forms the batches):

| File | `real_time` of the first and the last row |
|---|---|
| `taxi_100000.parquet` | up to 2026-04-02 06:09:56 (the source also has a few trips with pick-up times years off; they are kept) |
| `chicago_100000.parquet` | 2025-01-30 08:05:00 to 2026-06-04 00:00:00 |
| `ecommerce_100000.parquet` | 2019-10-01 00:00:00 to 2019-10-01 04:28:27 |

Notes required by the sources:

- **Chicago crimes.** The data reflect reported incidents of crime and may be based on preliminary information supplied
  to the Police Department by the reporting parties that has not been verified. The data should not be used for
  comparisons over time, and attempts to derive specific addresses are prohibited. The City of Chicago and the Chicago
  Police Department are not responsible for errors or omissions.
- **E-commerce.** The data set may be used for free with a mention of its source: the Kaggle page above and the REES46
  Marketing Platform. The data were collected by the Open CDP project.
- **Taxi trips.** The trip data were not created by the TLC, and the TLC makes no representations as to their accuracy.

The files are extracts for the reproduction of the paper's experiment; for any other use, obtain the data from the
sources above under their terms.
