"""Pure date/interval logic for the moving-wall access computation."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_ONE_DAY = timedelta(days=1)


def subtract_months(d: date, months: int) -> date:
    """``d`` shifted back by ``months`` calendar months.

    The day is clamped to the target month's length, so
    2026-03-31 minus 1 month is 2026-02-28.
    """
    if months < 0:
        raise ValueError("months must be >= 0")
    total = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class CoverageRow:
    package: str
    start: date
    end: date
    embargo_months: int


@dataclass(frozen=True)
class Segment:
    start: date
    end: date
    sources: Tuple[str, ...]


def accessible_interval(row: CoverageRow, query_date: date) -> Tuple[date, date] | None:
    """Accessible part of one coverage row on ``query_date``.

    The embargo is a moving wall: issues published within the most
    recent ``embargo_months`` before the query date are blocked, so the
    accessible window ends at ``query_date - embargo_months`` (and never
    past the row's own coverage end).
    """
    cutoff = subtract_months(query_date, row.embargo_months)
    acc_end = min(row.end, cutoff)
    if row.start > acc_end:
        return None
    return row.start, acc_end


def compute_segments(rows: Iterable[CoverageRow], query_date: date) -> List[Segment]:
    """Merge accessible intervals into segments split at source changes.

    Adjacent days served by the *same set* of packages are merged;
    wherever the contributing package set changes, a new segment starts,
    so every segment lists exactly the packages backing it.
    """
    intervals: List[Tuple[date, date, str]] = []
    for row in rows:
        acc = accessible_interval(row, query_date)
        if acc is not None:
            intervals.append((acc[0], acc[1], row.package))
    if not intervals:
        return []

    boundaries = set()
    for start, end, _ in intervals:
        boundaries.add(start)
        boundaries.add(end + _ONE_DAY)
    ordered = sorted(boundaries)

    segments: List[Segment] = []
    for a, b in zip(ordered, ordered[1:]):
        seg_end = b - _ONE_DAY
        sources = tuple(sorted({pkg for s, e, pkg in intervals if s <= a and e >= seg_end}))
        if not sources:
            continue
        if segments and segments[-1].sources == sources and segments[-1].end + _ONE_DAY == a:
            segments[-1] = Segment(segments[-1].start, seg_end, sources)
        else:
            segments.append(Segment(a, seg_end, sources))
    return segments


# ------------------------------------------------------- article-level access

@dataclass(frozen=True)
class CoverageRecord:
    """One active, in-license coverage row, as visible in one DB snapshot."""

    package_id: int
    package: str
    issn: str
    start: date
    end: date
    embargo_months: int


@dataclass(frozen=True)
class ArticleEvidence:
    """An article plus the packages that make it openable on a query date."""

    doi: str
    issn: str
    publication_date: date
    authors: Tuple[str, ...]
    sources: Tuple[CoverageRecord, ...] = field(default_factory=tuple)


def article_sources(
    issn: str,
    publication_date: date,
    query_date: date,
    coverage: Sequence[CoverageRecord],
) -> Tuple[CoverageRecord, ...]:
    """Coverage rows for the ISSN whose moving-wall window contains the article.

    The same rules as :func:`accessible_interval` apply: the package must
    cover the publication date and the query date minus the embargo must be
    on or after the publication date (the embargo wall moves with the query
    date).  Every matching row is returned, so when several packages cover
    the same journal all real sources are listed.
    """
    return tuple(
        row
        for row in coverage
        if row.issn == issn
        and row.start <= publication_date <= row.end
        and publication_date <= subtract_months(query_date, row.embargo_months)
    )


# ------------------------------------------------------------- co-author BFS

def build_evidence(
    rows: Iterable[Tuple],
    coverage_by_issn: Dict[str, List[CoverageRecord]],
    query_date: date,
) -> Dict[str, ArticleEvidence]:
    """Turn raw ``(doi, issn, publication_date, authors)`` rows into evidence.

    Withdrawn articles, and articles with no package making them openable on
    ``query_date`` (no coverage, outside a coverage window, or behind the
    embargo wall), are excluded: they cannot support any chain hop.
    """
    evidence: Dict[str, ArticleEvidence] = {}
    for doi, issn, pub_date, authors in rows:
        sources = article_sources(issn, pub_date, query_date, coverage_by_issn.get(issn, ()))
        if sources:
            evidence[doi] = ArticleEvidence(doi, issn, pub_date, tuple(authors), sources)
    return evidence


def shortest_coauthor_chain(
    start: str,
    target: str,
    accessible_articles: Sequence[ArticleEvidence],
) -> Optional[List[Tuple[str, str, ArticleEvidence]]]:
    """BFS over authors; each adjacency is backed by one accessible article.

    Returns, for each hop of a minimum-hop path, a
    ``(from_author, to_author, article)`` tuple in path order, or ``None``
    when the two authors are not connected through openable articles.
    Ties between equal-hop paths are broken deterministically by
    (neighbour author id, doi) so identical inputs always yield the same
    answer -- even when a co-authored article lists extra authors.
    """
    # Build the author -> (neighbour, article) adjacency once.
    adjacency: Dict[str, List[Tuple[str, ArticleEvidence]]] = {}
    for art in accessible_articles:
        for a in art.authors:
            for b in art.authors:
                if b != a:
                    adjacency.setdefault(a, []).append((b, art))

    if start not in adjacency:
        return None

    visited = {start: None}  # author -> (parent author, ArticleEvidence)
    frontier = [start]
    while frontier:
        current = frontier.pop(0)
        if current == target:
            break
        for neighbour, art in sorted(adjacency.get(current, ()), key=lambda x: (x[0], x[1].doi)):
            if neighbour not in visited:
                visited[neighbour] = (current, art)
                frontier.append(neighbour)

    if target not in visited:
        return None

    hops: List[Tuple[str, str, ArticleEvidence]] = []
    node = target
    while node != start:
        parent, art = visited[node]
        hops.append((parent, node, art))
        node = parent
    hops.reverse()
    return hops
