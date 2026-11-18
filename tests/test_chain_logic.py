"""Unit tests for article-source and co-author chain logic."""

from datetime import date

from app.logic import (
    ArticleEvidence,
    CoverageRecord,
    article_sources,
    build_evidence,
    shortest_coauthor_chain,
)

D = date(2026, 11, 18)


def rec(pkg_id, name, issn, start, end, embargo=0):
    return CoverageRecord(pkg_id, name, issn, date.fromisoformat(start),
                          date.fromisoformat(end), embargo)


def art(doi, issn, pub, authors, sources=()):
    return ArticleEvidence(doi, issn, date.fromisoformat(pub), tuple(authors), tuple(sources))


def test_article_sources_basic_window_and_embargo():
    rows = [rec(1, "A", "1234-5678", "2010-01-01", "2030-12-31", 0)]
    assert article_sources("1234-5678", date(2020, 6, 1), D, rows)
    # outside coverage window
    assert not article_sources("1234-5678", date(2009, 1, 1), D, rows)
    assert not article_sources("1234-5678", date(2031, 1, 1), D, rows)
    # embargo wall: 12-month moving wall on the query date
    walled = [rec(1, "A", "1234-5678", "2010-01-01", "2030-12-31", 12)]
    assert article_sources("1234-5678", date(2025, 11, 18), D, walled)  # exactly at wall
    assert not article_sources("1234-5678", date(2025, 11, 19), D, walled)
    # wrong issn never matches
    assert not article_sources("9999-9999", date(2020, 1, 1), D, rows)


def test_article_sources_lists_every_real_source():
    rows = [
        rec(1, "A", "1234-5678", "2010-01-01", "2030-12-31", 0),
        rec(2, "B", "1234-5678", "2020-01-01", "2030-12-31", 0),
        rec(3, "C", "1234-5678", "2025-01-01", "2030-12-31", 24),  # wall blocks 2024
    ]
    out = article_sources("1234-5678", date(2024, 5, 1), D, rows)
    assert [r.package for r in out] == ["A", "B"]  # C blocked by embargo, not listed


def test_build_evidence_drops_unopenable_and_uses_grouped_coverage():
    coverage = {"1234-5678": [rec(1, "A", "1234-5678", "2010-01-01", "2030-12-31", 0)]}
    raw = [
        ("10.1/a", "1234-5678", date(2020, 1, 1), ("x", "y")),       # openable
        ("10.1/b", "9999-9999", date(2020, 1, 1), ("y", "z")),       # no coverage
        ("10.1/c", "1234-5678", date(2005, 1, 1), ("z", "w")),       # outside window
    ]
    evidence = build_evidence(raw, coverage, D)
    assert set(evidence) == {"10.1/a"}
    assert evidence["10.1/a"].sources[0].package == "A"


def test_bfs_direct_coauthorship_is_one_hop():
    a = art("10.1/a", "1234-5678", "2020-01-01", ("x", "y"))
    hops = shortest_coauthor_chain("x", "y", [a])
    assert [(h[0], h[1], h[2].doi) for h in hops] == [("x", "y", "10.1/a")]


def test_bfs_minimum_hops_and_rerouting():
    # long path x-p-q-r-y (3 hops) vs short x-m-y (2 hops)
    a1 = art("10.1/long1", "1234-5678", "2020-01-01", ("x", "p"))
    a2 = art("10.1/long2", "1234-5678", "2020-01-02", ("p", "q"))
    a3 = art("10.1/long3", "1234-5678", "2020-01-03", ("q", "r"))
    a4 = art("10.1/long4", "1234-5678", "2020-01-04", ("r", "y"))
    short = art("10.1/short", "1234-5678", "2020-02-01", ("x", "m"))
    short2 = art("10.1/short2", "1234-5678", "2020-02-02", ("m", "y"))
    hops = shortest_coauthor_chain("x", "y", [a1, a2, a3, a4, short, short2])
    assert [h[2].doi for h in hops] == ["10.1/short", "10.1/short2"]

    # when the short route's articles vanish, BFS falls back to the long chain
    hops2 = shortest_coauthor_chain("x", "y", [a1, a2, a3, a4])
    assert [h[2].doi for h in hops2] == ["10.1/long1", "10.1/long2", "10.1/long3", "10.1/long4"]


def test_bfs_no_connection_and_unknown_author():
    a = art("10.1/a", "1234-5678", "2020-01-01", ("x", "y"))
    assert shortest_coauthor_chain("x", "z", [a]) is None
    assert shortest_coauthor_chain("ghost", "y", [a]) is None


def test_bfs_extra_authors_on_article_do_not_detour_path():
    # x and y co-author an article that also lists z; the hop must stay x->y
    a = art("10.1/a", "1234-5678", "2020-01-01", ("x", "z", "y"))
    hops = shortest_coauthor_chain("x", "y", [a])
    assert [(h[0], h[1]) for h in hops] == [("x", "y")]


def test_bfs_deterministic_tie_break():
    # x connects to y directly through two different articles
    a1 = art("10.1/aaa", "1234-5678", "2020-01-01", ("x", "y"))
    a2 = art("10.1/zzz", "1234-5678", "2020-01-02", ("x", "y"))
    hops = shortest_coauthor_chain("x", "y", [a2, a1])
    assert hops[0][2].doi == "10.1/aaa"  # sorted by doi
