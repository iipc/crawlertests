# Contributing

Contributions are welcome from crawler maintainers, web archivists and anyone
else. You can help by:

- **disputing an expectation** you think is wrong or unfair,
- **proposing a case** for a behaviour that isn't covered yet,
- **improving how a crawler is set up**, especially if you maintain it,
- **adding a crawler**.

Open an issue to discuss anything before it gets big, or send a pull request
for small changes. By contributing, you agree that your work is licensed
under the [Apache License 2.0](LICENSE), like the rest of the project.

## What the cases test

The suite asks one question: **does the archive hold what's needed to replay
the page as a browser shows it?** The cases follow these rules:

- **Expect what a browser would load or navigate to.** How a crawler finds it
  doesn't matter: a heuristic that reaches the same resources by another
  route is fine. A case can also include markup that browsers ignore, and
  put the URLs in it in `expect_not`, to catch crawlers that don't ignore it.
  Some cases go a step further
  and expect what an archive needs on other devices or after interaction,
  such as every `srcset` candidate or the result of a click. Their
  descriptions say so.
- **Expect only the URLs the case is about.** Don't also expect the steps on
  the way to them that other cases already cover: the `<script src>` that
  carries a source map comment, the link to a redirect, the page that a
  gzipped body arrives on. A crawler that misses a step misses the URL behind
  it anyway, so listing both counts one failure twice.
- **Use `expect_not` for things a crawler shouldn't fetch:** URLs that only
  look like links, scope violations, or URLs resolved against the wrong base.
- **One behaviour per case**, with a description of why it matters for
  archiving. Where possible, link to a real-world example or a crawler issue
  (`heritrix3#795`).
- **Keep cases fast.** The whole suite runs for every crawler every night, so
  avoid long delays and big files.

[Writing a case](README.md#writing-a-case) explains the format.

## Disputing an expectation

If a case expects something you think a crawler shouldn't do, or penalises
something reasonable, open an issue with:

- the case name and the expectation,
- what your crawler does and why,
- if you can, a reference: a spec, browser behaviour, or a real site where
  it matters.

Expectations can be changed without re-crawling anything. Saved runs are
re-scored against the current expectations, so a fix applies to the whole
history.

## How each crawler is set up

Each crawler runs in a configuration chosen by the maintainers of this suite,
kept as close to its defaults as possible. These are the main choices:

- Heritrix uses its stock profile, with the politeness delays set to zero.
- Every crawler obeys robots.txt.

The [crawler notes](README.md#crawler-notes) and the code in
`src/crawlertests/crawlers/` have the details. If you maintain a crawler and
think it's set up badly, please say so.

Some crawlers skip some kinds of resources by design, such as a crawl that
only collects text. They get a note on the site, so their score isn't read as
a bug. Ask for one if your crawler needs it.

## Adding a crawler

1. Subclass `Crawler` in a new module under `src/crawlertests/crawlers/` and
   implement these methods:
   - `crawl()`, which writes WARCs to the work directory,
   - `version()`,
   - `unavailable()`, which says why the crawler can't run here.
2. Register its type in `make_crawler()` in `crawlers/__init__.py` and in
   `DEFAULT_CRAWLERS` in `cli.py`.
3. For the nightly runs:
   - Add a job to `.github/workflows/crawl.yml`.
   - Add it to the default entries in `.github/scripts/plan-crawl.py`.
   - If its old releases can be re-crawled, add it to `stale_versions()` in
     `history.py`.
4. Add a section for it to [crawler notes](README.md#crawler-notes).

## Development

```bash
uv sync
uv run pytest
```

The tests start the fixture server and check that a "perfect crawler" passes
every case, so a new case must have consistent expectations and fixtures.
Run a crawler locally with `uv run crawlertests run`. See the
[quick start](README.md#quick-start).
