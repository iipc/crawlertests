"""Rendering results as a terminal summary and a standalone HTML report."""

import html
import json
from pathlib import Path


def text_summary(results: dict) -> str:
    crawlers = list(results["crawlers"])
    name_width = max([len(n) for n in results["cases"]] + [4])
    col_width = max([len(c) for c in crawlers] + [7])
    lines = ["case".ljust(name_width) + "  " + "  ".join(c.ljust(col_width) for c in crawlers)]
    for name, case in results["cases"].items():
        cells = []
        for c in crawlers:
            r = case["results"].get(c)
            if r is None:
                cells.append("-".ljust(col_width))
            else:
                mark = "PASS" if r["passed"] else "FAIL"
                cells.append(f"{mark} {r['hits']}/{r['total']}".ljust(col_width))
        lines.append(name.ljust(name_width) + "  " + "  ".join(cells))
    lines.append("")
    lines.append(f"{'crawler'.ljust(col_width)}  {'cases':>7}  {'captured':>15}  {'overcrawled':>15}  {'score':>15}")
    for c, info in results["crawlers"].items():
        if s := info.get("scores"):
            lines.append(f"{c.ljust(col_width)}  {s['cases_passed']:>3}/{s['cases']:<3}  "
                         f"{_ratio(s['captured'], s['expected']):>15}  "
                         f"{_ratio(s['overcrawled'], s['unexpected']):>15}  "
                         f"{_ratio(s['hits'], s['total']):>15}")
        else:
            lines.append(f"{c.ljust(col_width)}  {info['status']}" + (f" — {info['message']}" if info.get("message") else ""))
    lines.append("")
    lines.append("captured: expected URLs captured · overcrawled: captured URLs a case says shouldn't be"
                 " · score: expectations met")
    return "\n".join(lines)


def _ratio(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else f"{n}/0"


def write_json(results: dict, path: Path):
    path.write_text(json.dumps(results, indent=2) + "\n")


def write_html(results: dict, path: Path):
    path.write_text(render_html(results))


def _e(s) -> str:
    return html.escape(str(s))


def _mark(ok: bool | None) -> str:
    if ok is None:
        return '<td class="na">–</td>'
    return '<td class="pass">✓</td>' if ok else '<td class="fail">✗</td>'


# Pass-rate buckets and colours as used by wpt.fyi, from red (none met) to green (all met).
PASS_BUCKETS = [(0.2, "hardly"), (0.4, "a-few"), (0.6, "half"), (0.8, "lots"), (1.0, "most")]


def _cell_class(r: dict) -> str:
    if r["passed"]:
        return "passes-all"
    if r["hits"] == 0:
        return "passes-none"
    rate = r["hits"] / r["total"]
    return "passes-" + next(name for limit, name in PASS_BUCKETS if rate < limit)


STYLE = """
:root {
  --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6b6b66; --line: #e2e1dc; --card: #ffffff;
  --pass: #1f7a3e; --pass-bg: #e6f4ea; --fail: #b3261e; --fail-bg: #fbe9e7; --accent: #2f5fb3;
  --passes-none: #e57373; --passes-hardly: #ffb74d; --passes-a-few: #ffd54f; --passes-half: #fff176;
  --passes-lots: #dce775; --passes-most: #aed581; --passes-all: #81c784;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --fg: #ecebe6; --muted: #9a9993; --line: #33332f; --card: #1e1e1c;
    --pass: #7fd39a; --pass-bg: #19301f; --fail: #f2a39c; --fail-bg: #3a1d1a; --accent: #8fb0ef;
  }
}
body { background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 24px 16px; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 32px 0 8px; }
h3 { font-size: 1rem; margin: 16px 0 6px; }
.meta, .desc { color: var(--muted); }
a { color: var(--accent); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; background: var(--card); width: 100%; }
th, td { border: 1px solid var(--line); padding: 4px 10px; text-align: left; vertical-align: top; }
td.pass, td.fail, td.rate, td.na { text-align: center; white-space: nowrap; }
td.rate, td.rate a { color: #000; }
td.passes-none { background: var(--passes-none); }
td.passes-hardly { background: var(--passes-hardly); }
td.passes-a-few { background: var(--passes-a-few); }
td.passes-half { background: var(--passes-half); }
td.passes-lots { background: var(--passes-lots); }
td.passes-most { background: var(--passes-most); }
td.passes-all { background: var(--passes-all); }
td.pass { background: var(--pass-bg); color: var(--pass); }
td.fail { background: var(--fail-bg); color: var(--fail); }
td.na { color: var(--muted); }
td.pass a, td.fail a { color: inherit; }
code { font: 13px/1.4 ui-monospace, monospace; word-break: break-all; }
.tag { font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; font-weight: normal; }
section.case { border-top: 1px solid var(--line); margin-top: 24px; }
ul { margin: 4px 0; padding-left: 20px; }
td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.over { font-size: 12px; font-weight: 600; margin-left: 6px; }
"""


def page(title: str, body: str, root: str = "") -> str:
    """A standalone HTML page in the report style; root is the relative path to site.css, or "" to inline it."""
    css = f'<link rel="stylesheet" href="{root}site.css">' if root else f"<style>{STYLE}</style>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
{css}
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def scores_table(results: dict, extra_columns=None) -> str:
    """The per-crawler scores table. extra_columns: list of (heading, fn(label, info) -> html)."""
    extra_columns = extra_columns or []
    rows = []
    for c, info in results["crawlers"].items():
        extra = "".join(f"<td>{fn(c, info)}</td>" for _, fn in extra_columns)
        if sc := info.get("scores"):
            duration = f'{info["duration"]:.0f}s' if info.get("duration") is not None else ""
            rows.append(
                f'<tr><th scope="row">{_e(c)}</th>{extra}'
                f'<td class="num">{sc["cases_passed"]}/{sc["cases"]}</td>'
                f'<td class="num">{_e(_ratio(sc["captured"], sc["expected"]))}</td>'
                f'<td class="num">{_e(_ratio(sc["overcrawled"], sc["unexpected"]))}</td>'
                f'<td class="num"><strong>{_e(_ratio(sc["hits"], sc["total"]))}</strong></td>'
                f'<td class="num">{duration}</td>'
                f'<td>{_e(info.get("message") or "")}</td></tr>')
        else:
            rows.append(f'<tr><th scope="row">{_e(c)}</th>{extra}<td colspan="5" class="desc">{_e(info["status"])}'
                        f'</td><td>{_e(info.get("message") or "")}</td></tr>')
    extra_head = "".join(f"<th>{_e(h)}</th>" for h, _ in extra_columns)
    return f"""<div class="scroll"><table>
<tr><th>crawler</th>{extra_head}<th>cases passed</th><th>captured ↑</th><th>overcrawled ↓</th><th>score ↑</th>
<th>time</th><th>message</th></tr>
{''.join(rows)}
</table></div>
<p class="desc"><b>Captured</b>: URLs a case expects that were captured.
<b>Overcrawled</b>: URLs captured that a case says shouldn't be (<code>expect_not</code>).
<b>Score</b>: expectations met, counting both. In the case table, +N is the number overcrawled.</p>"""


def cases_table(results: dict, case_link=lambda name: f"#{name}", cell_link=None) -> str:
    """The case × crawler matrix. cell_link(case, crawler) gives each cell's link (default: case_link)."""
    crawlers = list(results["crawlers"])
    head = "".join(f"<th>{_e(c)}</th>" for c in crawlers)
    rows = []
    for name, case in results["cases"].items():
        cells = []
        for c in crawlers:
            r = case["results"].get(c)
            if r is None:
                cells.append('<td class="na">–</td>')
            else:
                href = cell_link(name, c) if cell_link else case_link(name)
                over = f'<span class="over">+{r["overcrawled"]}</span>' if r["overcrawled"] else ""
                cells.append(f'<td class="rate {_cell_class(r)}"><a href="{_e(href)}">{r["hits"]}/{r["total"]}</a>'
                             f'{over}</td>')
        tags = " ".join(f'<span class="tag">{_e(t)}</span>' for t in case["tags"])
        rows.append(f'<tr><th scope="row"><a href="{_e(case_link(name))}">{_e(name)}</a> {tags}</th>'
                    + "".join(cells) + "</tr>")
    return f"""<div class="scroll"><table>
<tr><th>case</th>{head}</tr>
{''.join(rows)}
</table></div>"""


def case_details(results: dict) -> str:
    crawlers = list(results["crawlers"])
    return "".join(_case_detail(name, case, crawlers) for name, case in results["cases"].items())


def render_html(results: dict, title: str = "Crawler test report", intro: str = "", root: str = "") -> str:
    body = f"""<h1>{_e(title)}</h1>
<p class="meta">Run {_e(results['run'])}</p>
{intro}
<h2>Scores</h2>
{scores_table(results)}

<h2>Cases</h2>
{cases_table(results)}

{case_details(results)}"""
    return page(title, body, root)


def _case_detail(name: str, case: dict, crawlers: list[str]) -> str:
    ran = [c for c in crawlers if c in case["results"]]
    head = "".join(f"<th>{_e(c)}</th>" for c in ran)
    rows = []
    any_result = next(iter(case["results"].values()), None)
    if any_result:
        for kind, label, want in (("expect", "should capture", True), ("expect_not", "should not capture", False)):
            for key in any_result[kind]:
                cells = "".join(_mark(case["results"][c][kind][key]["captured"] == want) for c in ran)
                rows.append(f"<tr><td><code>{_e(key)}</code></td><td>{label}</td>{cells}</tr>")

    notes = []
    for c in ran:
        r = case["results"][c]
        if r["extra"]:
            items = "".join(f"<li><code>{_e(k)}</code></li>" for k in r["extra"])
            notes.append(f"<h3>{_e(c)}: other captures</h3><ul>{items}</ul>")
        if r["not_archived"]:
            items = "".join(f"<li><code>{_e(k)}</code></li>" for k in r["not_archived"])
            notes.append(f"<h3>{_e(c)}: requested but not archived</h3><ul>{items}</ul>")

    table = (f'<div class="scroll"><table><tr><th>URL</th><th>expectation</th>{head}</tr>{"".join(rows)}'
             f"</table></div>") if rows else "<p class=\"desc\">No crawler results.</p>"
    return (f'<section class="case" id="{_e(name)}"><h2>{_e(name)} — {_e(case["title"])}</h2>'
            f'<p class="desc">{_e(case["description"])}</p>{table}{"".join(notes)}</section>')
