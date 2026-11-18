"""FastAPI application: journal coverage snapshots -> access segments."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated, List

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from . import db
from .logic import (
    CoverageRecord,
    CoverageRow,
    ArticleEvidence,
    build_evidence,
    compute_segments,
    shortest_coauthor_chain,
    subtract_months,
)
from .schemas import (
    AccessResponse,
    ArticleOut,
    ArticleSource,
    ArticleUpsert,
    ChainHop,
    CoauthorChainResponse,
    CoverageSnapshot,
    LicenseUpdate,
    PackageCreate,
    PackageOut,
    SegmentOut,
    normalize_doi,
    normalize_issn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Library Journal Access API",
    version="1.0.0",
    description=(
        "Stores per-package journal coverage snapshots (ISSN, publication "
        "date range, embargo months) and package license windows, then "
        "answers, for a query date + ISSN, which publication date ranges "
        "are accessible and from which packages."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers

def _package_or_404(conn: sqlite3.Connection, package_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"package {package_id} not found")
    return row


def _package_out(conn: sqlite3.Connection, row: sqlite3.Row) -> PackageOut:
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM coverage WHERE package_id = ?", (row["id"],)
    ).fetchone()["n"]
    return PackageOut(
        id=row["id"],
        name=row["name"],
        license_start=date.fromisoformat(row["license_start"]),
        license_end=date.fromisoformat(row["license_end"]),
        active=bool(row["active"]),
        coverage_rows=count,
    )


def _article_out(conn: sqlite3.Connection, doi: str, version: int) -> ArticleOut:
    row = conn.execute("SELECT * FROM articles WHERE doi = ?", (doi,)).fetchone()
    if row is None:  # pragma: no cover - callers check existence first
        raise HTTPException(status_code=404, detail=f"article {doi} not found")
    authors = [
        r["author_id"]
        for r in conn.execute(
            "SELECT author_id FROM article_authors WHERE doi = ? ORDER BY position", (doi,)
        ).fetchall()
    ]
    return ArticleOut(
        doi=row["doi"],
        issn=row["issn"],
        publication_date=date.fromisoformat(row["publication_date"]),
        status=row["status"],
        authors=authors,
        updated_at=datetime.fromisoformat(row["updated_at"]),
        data_version=version,
    )


# ---------------------------------------------------------------- routes

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/packages", response_model=PackageOut, status_code=201)
def create_package(body: PackageCreate) -> PackageOut:
    """Register a package with its license start/end dates."""
    with db.write_tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO packages (name, license_start, license_end) VALUES (?, ?, ?)",
                (body.name, body.license_start.isoformat(), body.license_end.isoformat()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"package name {body.name!r} already exists")
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, cur.lastrowid))


@app.get("/packages", response_model=List[PackageOut])
def list_packages() -> List[PackageOut]:
    with db.read_tx() as conn:
        rows = conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
        return [_package_out(conn, r) for r in rows]


@app.get("/packages/{package_id}", response_model=PackageOut)
def get_package(package_id: int) -> PackageOut:
    with db.read_tx() as conn:
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/license", response_model=PackageOut)
def update_license(package_id: int, body: LicenseUpdate) -> PackageOut:
    """Replace a package's license window (single committed transaction)."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute(
            "UPDATE packages SET license_start = ?, license_end = ? WHERE id = ?",
            (body.license_start.isoformat(), body.license_end.isoformat(), package_id),
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/coverage", response_model=PackageOut)
def replace_coverage(package_id: int, body: CoverageSnapshot) -> PackageOut:
    """Atomically replace the package's full coverage snapshot.

    The old snapshot stays in place until the new one commits; any
    failure rolls back to the previous committed version.
    """
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("DELETE FROM coverage WHERE package_id = ?", (package_id,))
        conn.executemany(
            "INSERT INTO coverage (package_id, issn, start_date, end_date, embargo_months)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (package_id, e.issn, e.start_date.isoformat(), e.end_date.isoformat(), e.embargo_months)
                for e in body.entries
            ],
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/packages/{package_id}/coverage")
def get_coverage(package_id: int) -> dict:
    with db.read_tx() as conn:
        _package_or_404(conn, package_id)
        rows = conn.execute(
            "SELECT issn, start_date, end_date, embargo_months FROM coverage"
            " WHERE package_id = ? ORDER BY issn, start_date",
            (package_id,),
        ).fetchall()
        return {
            "package_id": package_id,
            "data_version": db.current_version(conn),
            "entries": [dict(r) for r in rows],
        }


@app.post("/packages/{package_id}/deactivate", response_model=PackageOut)
def deactivate_package(package_id: int) -> PackageOut:
    """Stop using a package in answers; its rows stay stored and other
    packages' coverage is untouched."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 0 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.post("/packages/{package_id}/activate", response_model=PackageOut)
def activate_package(package_id: int) -> PackageOut:
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 1 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/access", response_model=AccessResponse)
def access(
    issn: str = Query(..., description="Journal ISSN, e.g. 1234-5678"),
    query_date: date = Query(..., alias="date", description="Query date, YYYY-MM-DD"),
) -> AccessResponse:
    """Accessible publication date segments for an ISSN on a query date.

    Only packages licensed *and* active on the query date contribute;
    each package's embargo moves with the query date.  Segments are
    split wherever the set of source packages changes.
    """
    try:
        norm_issn = normalize_issn(issn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.read_tx() as conn:
        version = db.current_version(conn)
        rows = conn.execute(
            """
            SELECT p.name AS package, c.start_date, c.end_date, c.embargo_months
            FROM coverage c
            JOIN packages p ON p.id = c.package_id
            WHERE c.issn = ?
              AND p.active = 1
              AND p.license_start <= ?
              AND p.license_end   >= ?
            """,
            (norm_issn, query_date.isoformat(), query_date.isoformat()),
        ).fetchall()

    coverage = [
        CoverageRow(
            package=r["package"],
            start=date.fromisoformat(r["start_date"]),
            end=date.fromisoformat(r["end_date"]),
            embargo_months=r["embargo_months"],
        )
        for r in rows
    ]
    segments = compute_segments(coverage, query_date)
    return AccessResponse(
        issn=norm_issn,
        date=query_date,
        data_version=version,
        segments=[
            SegmentOut(start=s.start, end=s.end, sources=list(s.sources)) for s in segments
        ],
    )


# ---------------------------------------------------------------- articles

@app.put("/articles/{doi:path}", response_model=ArticleOut, status_code=201)
def upsert_article(doi: str, body: ArticleUpsert, response: Response) -> ArticleOut:
    """Register or correct article metadata keyed by DOI.

    The whole record (ISSN, publication date, ordered author list) is
    replaced inside one transaction, so a correction never leaves a mix of
    old and new rows; a withdrawn DOI re-submitted this way becomes active
    again.  Returns 201 on first insertion and 200 on correction.
    """
    try:
        norm_doi = normalize_doi(doi)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = datetime.now(timezone.utc).isoformat()
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT doi FROM articles WHERE doi = ?", (norm_doi,)
        ).fetchone()
        conn.execute(
            "INSERT INTO articles (doi, issn, publication_date, status, updated_at)"
            " VALUES (?, ?, ?, 'active', ?)"
            " ON CONFLICT(doi) DO UPDATE SET issn = excluded.issn,"
            " publication_date = excluded.publication_date,"
            " status = 'active', updated_at = excluded.updated_at",
            (norm_doi, body.issn, body.publication_date.isoformat(), now),
        )
        conn.execute("DELETE FROM article_authors WHERE doi = ?", (norm_doi,))
        conn.executemany(
            "INSERT INTO article_authors (doi, position, author_id) VALUES (?, ?, ?)",
            [(norm_doi, pos, author) for pos, author in enumerate(body.authors)],
        )
        version = db.bump_version(conn)
        result = _article_out(conn, norm_doi, version)
    response.status_code = 200 if existing else 201
    return result


@app.post("/articles/{doi:path}/withdraw", response_model=ArticleOut)
def withdraw_article(doi: str) -> ArticleOut:
    """Withdraw an article: it stays stored but can no longer back any hop.

    Idempotent: withdrawing an already withdrawn DOI still succeeds and
    bumps the version only on an actual status change.
    """
    try:
        norm_doi = normalize_doi(doi)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.write_tx() as conn:
        row = conn.execute("SELECT status FROM articles WHERE doi = ?", (norm_doi,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"article {norm_doi} not found")
        version = db.current_version(conn)
        if row["status"] != "withdrawn":
            conn.execute(
                "UPDATE articles SET status = 'withdrawn', updated_at = ? WHERE doi = ?",
                (datetime.now(timezone.utc).isoformat(), norm_doi),
            )
            version = db.bump_version(conn)
        return _article_out(conn, norm_doi, version)


@app.get("/articles", response_model=List[ArticleOut])
def list_articles(
    status: Annotated[str | None, Query(description="active | withdrawn; default: all")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> List[ArticleOut]:
    if status is not None and status not in ("active", "withdrawn"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'withdrawn'")
    with db.read_tx() as conn:
        version = db.current_version(conn)
        if status is None:
            rows = conn.execute(
                "SELECT doi FROM articles ORDER BY doi LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT doi FROM articles WHERE status = ? ORDER BY doi LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [_article_out(conn, r["doi"], version) for r in rows]


@app.get("/articles/{doi:path}", response_model=ArticleOut)
def get_article(doi: str) -> ArticleOut:
    try:
        norm_doi = normalize_doi(doi)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with db.read_tx() as conn:
        return _article_out(conn, norm_doi, db.current_version(conn))


# ------------------------------------------------------------ co-author chain

def _chain_hop(
    prev_author: str, next_author: str, evidence: ArticleEvidence, query_date: date
) -> ChainHop:
    return ChainHop(
        from_author=prev_author,
        to_author=next_author,
        article_doi=evidence.doi,
        issn=evidence.issn,
        publication_date=evidence.publication_date,
        sources=[
            ArticleSource(
                package_id=rec.package_id,
                package=rec.package,
                coverage_start=rec.start,
                coverage_end=rec.end,
                embargo_months=rec.embargo_months,
                embargo_cutoff=subtract_months(query_date, rec.embargo_months),
            )
            for rec in evidence.sources
        ],
    )


@app.get("/coauthor-chain", response_model=CoauthorChainResponse)
def coauthor_chain(
    author_a: Annotated[str, Query(...)],
    author_b: Annotated[str, Query(...)],
    query_date: Annotated[date, Query(alias="date")],
) -> CoauthorChainResponse:
    """Minimum-hop co-authorship connection openable on the query date.

    The whole answer is computed inside one read transaction, so the
    articles, their author lists and every coverage/license basis come from
    the same committed ``data_version``.  Each hop carries one article that
    is genuinely openable that day plus every package (real source) backing
    it.  An explicit ``connected: false`` answer is returned when no path of
    openable articles joins the two authors.
    """
    a = author_a.strip()
    b = author_b.strip()
    if not a or not b:
        raise HTTPException(status_code=422, detail="author_a and author_b must be non-empty")

    with db.read_tx() as conn:
        version = db.current_version(conn)

        # Coverage rows that are even allowed to serve on the query date:
        # package active AND within its license window.  The embargo wall
        # and publication-date containment are checked per article below.
        coverage_rows = conn.execute(
            """
            SELECT p.id AS package_id, p.name AS package, c.issn,
                   c.start_date, c.end_date, c.embargo_months
            FROM coverage c
            JOIN packages p ON p.id = c.package_id
            WHERE p.active = 1
              AND p.license_start <= ?
              AND p.license_end   >= ?
            """,
            (query_date.isoformat(), query_date.isoformat()),
        ).fetchall()
        coverage_by_issn: dict[str, List[CoverageRecord]] = {}
        for r in coverage_rows:
            rec = CoverageRecord(
                package_id=r["package_id"],
                package=r["package"],
                issn=r["issn"],
                start=date.fromisoformat(r["start_date"]),
                end=date.fromisoformat(r["end_date"]),
                embargo_months=r["embargo_months"],
            )
            coverage_by_issn.setdefault(rec.issn, []).append(rec)

        article_rows = conn.execute(
            """
            SELECT a.doi, a.issn, a.publication_date,
                   GROUP_CONCAT(aa.author_id, CHAR(31)) AS authors
            FROM articles a
            JOIN (
                SELECT doi, position, author_id
                FROM article_authors
                ORDER BY doi, position
            ) aa ON aa.doi = a.doi
            WHERE a.status = 'active'
            GROUP BY a.doi
            ORDER BY a.doi
            """
        ).fetchall()
        raw = [
            (
                r["doi"],
                r["issn"],
                date.fromisoformat(r["publication_date"]),
                r["authors"].split("\x1f"),
            )
            for r in article_rows
        ]

        known_authors = {author for r in article_rows for author in r["authors"].split("\x1f")}
        evidence = build_evidence(raw, coverage_by_issn, query_date)
        accessible = list(evidence.values())

        if a == b:
            return CoauthorChainResponse(
                author_a=a, author_b=b, date=query_date, data_version=version,
                connected=True, hops=0, path=[a], chain=[],
                message="same author; zero hops",
            )

        missing = [x for x in (a, b) if x not in known_authors]
        if missing:
            return CoauthorChainResponse(
                author_a=a, author_b=b, date=query_date, data_version=version,
                connected=False, hops=0, path=[], chain=[],
                message=f"no connection: unknown author id(s): {', '.join(missing)}",
            )

        chain_hops = shortest_coauthor_chain(a, b, accessible)
        if chain_hops is None:
            return CoauthorChainResponse(
                author_a=a, author_b=b, date=query_date, data_version=version,
                connected=False, hops=0, path=[], chain=[],
                message=(
                    "no connection on the query date: no path of co-authored, "
                    "openable articles joins the two authors (articles may be "
                    "withdrawn or blocked by coverage/embargo/license boundaries)"
                ),
            )

        path = [a]
        hops_out: List[ChainHop] = []
        for prev_author, next_author, art in chain_hops:
            hops_out.append(_chain_hop(prev_author, next_author, art, query_date))
            path.append(next_author)

        return CoauthorChainResponse(
            author_a=a, author_b=b, date=query_date, data_version=version,
            connected=True, hops=len(hops_out), path=path, chain=hops_out,
            message=f"connected in {len(hops_out)} co-authorship hop(s); "
                    f"every article is openable on {query_date.isoformat()} via the listed sources",
        )
