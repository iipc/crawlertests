"""Scoring each crawler's captures against each case's expectations."""

from .cases import Case, Expectation
from .urls import normalize, normalize_path
from .warcs import Capture


def captured_keys(captures: list[Capture], origins: dict[int, str]) -> dict[str, list[Capture]]:
    keys: dict[str, list[Capture]] = {}
    for c in captures:
        keys.setdefault(normalize(c.url, origins), []).append(c)
    return keys


def requested_keys(requests: list[dict], origins: dict[int, str]) -> set[str]:
    return {origins.get(r["port"], "") + normalize_path(r["path"]) for r in requests}


def score_case(case: Case, captured: dict[str, list[Capture]], requested: set[str] | None) -> dict:
    def check(exp: Expectation) -> dict:
        captures = captured.get(exp.key, [])
        matching = [c for c in captures if exp.matches(c)]
        shown = matching or captures
        return {
            "captured": bool(matching),
            "status": shown[0].status if shown else None,
            "requested": exp.key in requested if requested is not None else None,
        }

    expect = {exp.label: check(exp) for exp in case.expected()}
    expect_not = {exp.label: check(exp) for exp in case.unexpected()}
    known = {exp.key for exp in case.expected() + case.unexpected()} | case.seeds()
    extra = sorted(k for k in captured if case.owns(k) and k not in known)
    not_archived = sorted(k for k in (requested or ()) if case.owns(k) and k not in captured)

    missed = sum(not r["captured"] for r in expect.values())
    overcrawled = sum(r["captured"] for r in expect_not.values())
    total = len(expect) + len(expect_not)
    hits = total - missed - overcrawled
    return {
        "passed": hits == total,
        "hits": hits,
        "total": total,
        "missed": missed,
        "overcrawled": overcrawled,
        "expect": expect,
        "expect_not": expect_not,
        "extra": extra,
        "not_archived": not_archived,
    }


def crawler_scores(case_results: list[dict]) -> dict:
    """Totals over every case a crawler ran.

    captured / missed: expected URLs captured or not, out of all expected URLs.
    overcrawled: URLs captured that a case says shouldn't be, out of all such URLs.
    score (hits/total): expectations met (expected captured, unexpected avoided), out of all expectations.
    """
    expected = sum(len(r["expect"]) for r in case_results)
    unexpected = sum(len(r["expect_not"]) for r in case_results)
    missed = sum(r["missed"] for r in case_results)
    overcrawled = sum(r["overcrawled"] for r in case_results)
    hits = sum(r["hits"] for r in case_results)
    return {
        "cases_passed": sum(r["passed"] for r in case_results),
        "cases": len(case_results),
        "expected": expected,
        "captured": expected - missed,
        "missed": missed,
        "unexpected": unexpected,
        "overcrawled": overcrawled,
        "hits": hits,
        "total": expected + unexpected,
    }
