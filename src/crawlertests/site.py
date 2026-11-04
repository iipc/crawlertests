"""The results history website: static pages built from a directory of run records.

Every record is re-scored against the current cases, so the whole history
reflects today's expectations. Pages:

  index.html                       latest result for each crawler and track, scores over time
  history/<crawler>--<track>.html  each case's result across that crawler's runs
  runs/<run>/index.html            the full report for one run
  compare.html                     expectations that differ between two runs (reads data/)
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .cases import Case
from .compare import crawler_scores
from .history import load_records, results_for, score_record
from .report import STYLE, _cell_class, _e, _ratio, case_details, cases_table, page, scores_table

# A categorical palette that reads on both the light and dark backgrounds.
SERIES_COLORS = ["#4269d0", "#ef6c3b", "#3ca951", "#a463f2", "#d4a106", "#2bb3c0", "#e2559b", "#8c6d4f"]

SITE_STYLE = """
nav { display: flex; gap: 16px; flex-wrap: wrap; margin: 0 0 16px; font-size: 14px; }
.chart { width: 100%; height: auto; display: block; background: var(--card); border: 1px solid var(--line); }
.chart text { fill: var(--muted); font: 11px system-ui, sans-serif; }
.chart .grid { stroke: var(--line); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 8px 0; font-size: 14px; }
.legend span::before { content: ""; display: inline-block; width: 12px; height: 3px; margin-right: 6px;
  vertical-align: middle; background: var(--c); }
th.run { font-weight: normal; font-size: 12px; white-space: nowrap; }
td.changed { box-shadow: inset 0 0 0 2px var(--fg); }
select { font: inherit; max-width: 100%; }
.pickers { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px 16px; }
"""


def series_slug(record: dict) -> str:
    return f"{record['crawler']}--{record.get('track') or 'local'}"


def series_label(record: dict) -> str:
    track = record.get("track")
    return record["crawler"] if track in (None, "", "local") else f"{record['crawler']} · {track}"


def version_label(record: dict) -> str:
    v = record.get("version") or {}
    label = v.get("version") or "unknown version"
    if rev := v.get("revision"):
        label += f" @{rev[:8]}"
    return label


def record_date(record: dict) -> datetime:
    """When the crawler version was made (release or commit date), or else when it ran."""
    for value in ((record.get("version") or {}).get("date"), record.get("time")):
        if value:
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return datetime.fromtimestamp(0, timezone.utc)


def build_site(results_dir: Path, cases: dict[str, Case], out: Path):
    records = [r for r in load_records(results_dir) if r["status"] == "ok"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "site.css").write_text(STYLE + SITE_STYLE)

    scored = {}
    for r in records:
        case_results = score_record(r, cases)
        scored[(r["run"], r["crawler"])] = (case_results, crawler_scores(list(case_results.values())))

    series = defaultdict(list)
    for r in records:
        series[series_slug(r)].append(r)
    for rs in series.values():
        rs.sort(key=lambda r: (record_date(r), r.get("time") or ""))
    slugs = sorted(series, key=lambda s: series_label(series[s][-1]))

    _write_index(out, cases, series, slugs, scored)
    for slug in slugs:
        _write_history(out, slug, series[slug], cases, scored)
    by_run = defaultdict(list)
    for r in records:
        by_run[r["run"]].append(r)
    for run, rs in by_run.items():
        results = results_for([(series_label(r), r) for r in rs], cases, run)
        when = ", ".join(sorted({f"{series_label(r)}: {version_label(r)}" for r in rs}))
        intro = f'<nav><a href="../../index.html">← all results</a></nav><p class="desc">{_e(when)}</p>'
        path = out / "runs" / run / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_retitle(results, f"Run {run}", intro))
    _write_compare(out, records, scored)


def _retitle(results: dict, title: str, intro: str) -> str:
    body = f"""{intro}<h1>{_e(title)}</h1>
<h2>Scores</h2>
{scores_table(results)}
<h2>Cases</h2>
{cases_table(results)}
{case_details(results)}"""
    return page(title, body, root="../../")


def _write_index(out: Path, cases, series, slugs, scored):
    latest = [series[s][-1] for s in slugs]
    results = results_for([(series_label(r), r) for r in latest], cases, "latest")
    by_label = {series_label(r): r for r in latest}
    columns = [
        ("version", lambda label, info: _e(version_label(by_label[label]))),
        ("date", lambda label, info: _e(record_date(by_label[label]).date().isoformat())),
        ("", lambda label, info: f'<a href="history/{_e(series_slug(by_label[label]))}.html">history</a> · '
                                 f'<a href="runs/{_e(by_label[label]["run"])}/index.html">report</a>'),
    ]
    chart = _chart(series, slugs, scored) if latest else '<p class="desc">No runs yet.</p>'
    body = f"""<h1>Web archiving crawler tests</h1>
<nav><a href="compare.html">compare runs</a>
<a href="https://github.com/iipc/crawlertests">test suite</a></nav>
<p class="desc">How well web archiving crawlers discover and capture the resources of a set of small test sites.
Every run is scored against the current version of the tests, so scores are comparable across time.</p>

<h2>Latest results</h2>
{scores_table(results, columns)}

<h2>Score over time</h2>
{chart}

<h2>Cases</h2>
{cases_table(results, case_link=lambda name: f"runs/{by_label[next(iter(by_label))]['run']}/index.html#{name}",
             cell_link=lambda name, label: f"runs/{by_label[label]['run']}/index.html#{name}")}
"""
    (out / "index.html").write_text(page("Crawler tests", body, root="./"))


def _chart(series, slugs, scored) -> str:
    width, height, left, right, top, bottom = 900, 320, 44, 16, 12, 28
    points = {s: [(record_date(r), scored[(r["run"], r["crawler"])][1], r) for r in series[s]] for s in slugs}
    dates = [d for ps in points.values() for d, _, _ in ps]
    lo, hi = min(dates).timestamp(), max(dates).timestamp()
    if hi - lo < 86400:
        lo, hi = lo - 86400 * 15, hi + 86400 * 15
    pad = (hi - lo) * 0.03
    lo, hi = lo - pad, hi + pad

    def x(d: datetime) -> float:
        return left + (d.timestamp() - lo) / (hi - lo) * (width - left - right)

    def y(pct: float) -> float:
        return top + (1 - pct) * (height - top - bottom)

    parts = []
    for pct in (0, 0.25, 0.5, 0.75, 1):
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(pct):.1f}" y2="{y(pct):.1f}"/>'
                     f'<text x="{left - 6}" y="{y(pct) + 4:.1f}" text-anchor="end">{pct:.0%}</text>')
    for tick in _time_ticks(lo, hi):
        tx = x(tick)
        label = tick.strftime("%Y") if (hi - lo) > 86400 * 365 * 2 else tick.strftime("%b %Y")
        parts.append(f'<text x="{tx:.1f}" y="{height - 8}" text-anchor="middle">{label}</text>')

    legend = []
    for i, s in enumerate(slugs):
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        ps = points[s]
        coords = [(x(d), y(sc["hits"] / sc["total"] if sc["total"] else 0)) for d, sc, _ in ps]
        if len(coords) > 1:
            path = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
            parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for (cx, cy), (d, sc, r) in zip(coords, ps):
            tip = (f"{series_label(r)} {version_label(r)} ({d.date()})\nscore {_ratio(sc['hits'], sc['total'])}, "
                   f"captured {_ratio(sc['captured'], sc['expected'])}, "
                   f"overcrawled {_ratio(sc['overcrawled'], sc['unexpected'])}")
            parts.append(f'<a href="runs/{_e(r["run"])}/index.html"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" '
                         f'fill="{color}"><title>{_e(tip)}</title></circle></a>')
        legend.append(f'<span style="--c: {color}">{_e(series_label(series[s][-1]))}</span>')
    svg = (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Score over time">'
           + "".join(parts) + "</svg>")
    return (f'<div class="legend">{"".join(legend)}</div>{svg}'
            '<p class="desc">Expectations met, by the date of the crawler version (or of the run, for local runs). '
            'Hover a point for details; click it for the run report.</p>')


def _time_ticks(lo: float, hi: float) -> list[datetime]:
    start = datetime.fromtimestamp(lo, timezone.utc)
    end = datetime.fromtimestamp(hi, timezone.utc)
    years = (hi - lo) / (86400 * 365)
    step = 12 if years > 2 else 3 if years > 0.75 else 1
    month = start.month - 1
    month += -month % step
    tick = datetime(start.year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
    ticks = []
    while tick <= end:
        if tick >= start:
            ticks.append(tick)
        m = tick.month - 1 + step
        tick = datetime(tick.year + m // 12, m % 12 + 1, 1, tzinfo=timezone.utc)
    return ticks


def _write_history(out: Path, slug: str, records: list[dict], cases, scored, limit: int = 60):
    records = records[-limit:]
    label = series_label(records[-1])
    head = "".join(f'<th class="run"><a href="../runs/{_e(r["run"])}/index.html">{_e(version_label(r))}</a><br>'
                   f'{record_date(r).date()}</th>' for r in records)
    totals = []
    for r in records:
        sc = scored[(r["run"], r["crawler"])][1]
        totals.append(f'<td class="num"><strong>{sc["hits"] / sc["total"]:.0%}</strong></td>' if sc["total"]
                      else "<td></td>")
    rows = [f'<tr><th scope="row">score</th>{"".join(totals)}</tr>']
    for name, case in cases.items():
        cells, previous = [], None
        for r in records:
            result = scored[(r["run"], r["crawler"])][0].get(name)
            if result is None:
                cells.append('<td class="na">–</td>')
                previous = None
                continue
            changed = " changed" if previous is not None and previous != result["hits"] else ""
            over = f'<span class="over">+{result["overcrawled"]}</span>' if result["overcrawled"] else ""
            cells.append(f'<td class="rate {_cell_class(result)}{changed}">'
                         f'<a href="../runs/{_e(r["run"])}/index.html#{_e(name)}">{result["hits"]}/{result["total"]}'
                         f'</a>{over}</td>')
            previous = result["hits"]
        if any(name in scored[(r["run"], r["crawler"])][0] for r in records):
            rows.append(f'<tr><th scope="row" title="{_e(case.title)}">{_e(name)}</th>{"".join(cells)}</tr>')
    body = f"""<nav><a href="../index.html">← all results</a><a href="../compare.html">compare runs</a></nav>
<h1>{_e(label)}: history</h1>
<p class="desc">Each case's result in the last {len(records)} saved runs, oldest first, scored against the current
tests. An outlined cell differs from the run before.</p>
<div class="scroll"><table>
<tr><th>case</th>{head}</tr>
{''.join(rows)}
</table></div>"""
    path = out / "history" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page(f"{label} history", body, root="../"))


def _write_compare(out: Path, records: list[dict], scored):
    index = []
    for r in sorted(records, key=lambda r: (series_label(r), record_date(r))):
        case_results = scored[(r["run"], r["crawler"])][0]
        data = {name: {"met": {label: res["captured"] for label, res in cr["expect"].items()}
                       | {f"not {label}": not res["captured"] for label, res in cr["expect_not"].items()}}
                for name, cr in case_results.items()}
        path = out / "data" / r["run"] / f"{r['crawler']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")))
        index.append({"id": f"{r['run']}/{r['crawler']}", "series": series_label(r),
                      "label": f"{series_label(r)} — {version_label(r)} ({record_date(r).date()})"})
    (out / "data" / "index.json").write_text(json.dumps(index, indent=1))
    body = """<nav><a href="index.html">← all results</a></nav>
<h1>Compare runs</h1>
<p class="desc">Expectations met by one run and not the other, scored against the current tests.</p>
<div class="pickers">
<label>Before<br><select id="a"></select></label>
<label>After<br><select id="b"></select></label>
</div>
<div id="out"></div>
<script>
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text;
  if (cls) e.className = cls; return e; };
async function main() {
  const runs = await (await fetch("data/index.json")).json();
  for (const id of ["a", "b"]) for (const r of runs) $(id).append(new Option(r.label, r.id));
  const params = new URLSearchParams(location.hash.slice(1));
  // default to the last two runs of a crawler that has more than one
  let last = runs.findLastIndex((r, i) => i > 0 && runs[i - 1].series === r.series);
  if (last < 0) last = runs.length - 1;
  $("a").value = params.get("a") || runs[Math.max(last - 1, 0)]?.id;
  $("b").value = params.get("b") || runs[last]?.id;
  for (const id of ["a", "b"]) $(id).onchange = show;
  show();
}
async function show() {
  const [a, b] = [$("a").value, $("b").value];
  location.hash = new URLSearchParams({a, b}).toString();
  const [da, db] = await Promise.all([a, b].map(id => fetch(`data/${id}.json`).then(r => r.json())));
  const out = $("out"); out.replaceChildren();
  const fixed = [], broke = [];
  for (const name of Object.keys(db)) {
    if (!(name in da)) continue;
    for (const [label, met] of Object.entries(db[name].met)) {
      const before = da[name].met[label];
      if (before === undefined || before === met) continue;
      (met ? fixed : broke).push([name, label]);
    }
  }
  const onlyA = Object.keys(da).filter(n => !(n in db)), onlyB = Object.keys(db).filter(n => !(n in da));
  for (const [title, rows, cls] of [["Newly met", fixed, "pass"], ["No longer met", broke, "fail"]]) {
    out.append(el("h2", `${title} (${rows.length})`));
    if (!rows.length) { out.append(el("p", "None.", "desc")); continue; }
    const table = el("table"), head = table.insertRow();
    head.append(el("th", "case"), el("th", "expectation"));
    for (const [name, label] of rows) {
      const tr = table.insertRow(); tr.append(el("td", name));
      const td = el("td", null, cls); td.append(el("code", label)); td.style.textAlign = "left"; tr.append(td);
    }
    const wrap = el("div", null, "scroll"); wrap.append(table); out.append(wrap);
  }
  if (onlyA.length || onlyB.length) {
    out.append(el("h2", "Cases in only one run"));
    if (onlyA.length) out.append(el("p", "Before only: " + onlyA.join(", "), "desc"));
    if (onlyB.length) out.append(el("p", "After only: " + onlyB.join(", "), "desc"));
  }
}
main();
</script>"""
    (out / "compare.html").write_text(page("Compare runs", body, root="./"))
