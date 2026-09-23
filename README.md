# Amiees pin feeds

Three RSS 2.0 feeds that Pinterest reads and publishes from automatically.

```
pins.csv  ->  make_feeds.py  ->  feeds/*.xml  ->  Pinterest
```

## Why this exists

Pinterest's scheduler caps you at 10 pins queued for the future, and the bulk
CSV upload obeys the same cap: a 32 row file published 9 rows and silently
discarded the rest. RSS auto publish has no such cap. Pinterest reads the feed
within 24 hours, publishes the oldest items first, and will do up to 200 pins a
day across all feeds.

That means Pinterest will happily publish everything it finds, all at once. So
the pace is set here instead: each row in `pins.csv` has a `release_at`, and an
item only enters its feed once that time has passed. The GitHub Action reruns
the generator every six hours and commits the result.

## Rules that are not negotiable

* **RSS 2.0 or RSS 1.0 RDF.** Atom is not supported.
* **Every item `<link>` must be on the claimed domain**, amiees.com. The feed
  URL itself does not have to be, which is why GitHub Pages works here.
* **One feed publishes to exactly one board.** Hence three feeds.
* **Media URLs must be publicly reachable** when Pinterest fetches them. The
  images sit in Shopify Files and are served from the Shopify CDN.
* **Never use em dashes** in titles or descriptions.

## Adding a batch

Append rows to `pins.csv` and push. The Action does the rest.

| column | what it is |
|---|---|
| `guid` | stable unique id. Pinterest dedupes on it, so never reuse or rewrite one |
| `board` | must match the Pinterest board name character for character |
| `title` | 100 characters max, unique across the file |
| `description` | 500 characters max |
| `link` | the product page on amiees.com |
| `image` | public image URL, 1000x1500 JPEG |
| `keywords` | comma separated, for your own reference |
| `release_at` | **UTC**, `YYYY-MM-DD HH:MM:SS`. The item is invisible until then |
| `status` | `queued` to publish, anything else to hold it back forever |

`release_at` is UTC, not Eastern. 9am Eastern is 13:00 UTC in summer, 14:00 in
winter. Getting this wrong is what made a test pin land four hours early.

Set `status` to `published` on anything already live on Pinterest, so it is
never pinned twice.

## Running it by hand

```bash
python3 make_feeds.py
```

No dependencies beyond the standard library. It validates that every guid is
unique and every link is on amiees.com before writing anything.

## Current state

32 pins. 9 published manually before this existed and marked `published`.
The remaining 23 release at four a day.
