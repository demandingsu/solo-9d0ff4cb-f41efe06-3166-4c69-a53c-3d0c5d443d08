"""API integration tests: article metadata, multi-source evidence, rerouting."""

from conftest import chain, entry, make_package, put_article, put_coverage, query

ISSN = "1111-1111"
ISSN2 = "2222-2222"
QDATE = "2026-11-18"


def _two_packages_same_journal(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    put_coverage(client, a, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    put_coverage(client, b, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    return a, b


def test_article_create_correct_withdraw_and_reactivate(client):
    r = put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    assert r.status_code == 201, r.text
    assert r.json()["authors"] == ["u1", "u2"]
    assert r.json()["status"] == "active"
    v1 = r.json()["data_version"]

    # correcting the same DOI replaces the whole record -> 200, old authors gone
    r = put_article(client, "https://doi.org/10.1/a1", ISSN, "2021-01-02", ["u1", "u9"])
    assert r.status_code == 200
    body = r.json()
    assert body["doi"] == "10.1/a1"  # normalized
    assert body["publication_date"] == "2021-01-02"
    assert body["authors"] == ["u1", "u9"]
    assert body["data_version"] == v1 + 1

    # DOI lookup accepts URL/prefix forms
    assert client.get("/articles/doi:10.1/a1").json()["authors"] == ["u1", "u9"]

    # withdraw: record stays, status flips
    r = client.post("/articles/10.1/a1/withdraw")
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
    v_withdrawn = r.json()["data_version"]
    # idempotent: second withdraw does not bump the version
    r2 = client.post("/articles/10.1/a1/withdraw")
    assert r2.json()["status"] == "withdrawn"
    assert r2.json()["data_version"] == v_withdrawn

    # re-submitting a withdrawn DOI makes it active again
    r = put_article(client, "10.1/a1", ISSN, "2021-01-02", ["u1", "u2"])
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    # listing and 404
    assert len(client.get("/articles").json()) == 1
    assert client.get("/articles/10.1/missing").status_code == 404
    assert client.post("/articles/10.1/missing/withdraw").status_code == 404


def test_invalid_article_payloads_rejected_atomically(client):
    _two_packages_same_journal(client)
    ok = put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    version_before = ok.json()["data_version"]

    for bad in [
        {"issn": "junk", "publication_date": "2021-01-01", "authors": ["u1"]},
        {"issn": ISSN, "publication_date": "2021-01-01", "authors": []},
        {"issn": ISSN, "publication_date": "2021-01-01", "authors": ["u1", "u1"]},
        {"issn": ISSN, "publication_date": "2021-01-01", "authors": ["  "]},
    ]:
        r = client.put("/articles/10.1/bad", json=bad)
        assert r.status_code == 422, bad

    # bad DOI shape in the path
    assert client.put("/articles/not-a-doi",
                      json={"issn": ISSN, "publication_date": "2021-01-01",
                            "authors": ["u1"]}).status_code == 422

    # nothing committed: old record and version survive
    got = client.get("/articles/10.1/a1").json()
    assert got["authors"] == ["u1", "u2"]
    assert got["data_version"] == version_before
    r = chain(client, "u1", "u2", QDATE)
    assert r["connected"] is True and r["data_version"] == version_before


def test_chain_min_hop_with_multi_package_evidence(client):
    _two_packages_same_journal(client)
    put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    put_article(client, "10.1/a2", ISSN, "2021-02-01", ["u2", "u3"])
    put_article(client, "10.1/a3", ISSN, "2021-03-01", ["u1", "u3"])  # direct shortcut

    r = chain(client, "u1", "u3", QDATE)
    assert r["connected"] is True
    assert r["hops"] == 1 and r["path"] == ["u1", "u3"]
    hop = r["chain"][0]
    assert hop["article_doi"] == "10.1/a3"
    # same article covered by two packages: both real sources listed
    assert {s["package"] for s in hop["sources"]} == {"PackA", "PackB"}
    for s in hop["sources"]:
        assert s["embargo_months"] == 0
        assert s["embargo_cutoff"] == QDATE
        assert s["coverage_start"] == "2010-01-01"


def test_withdraw_invalidates_hop_and_chain_reroutes(client):
    _two_packages_same_journal(client)
    put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    put_article(client, "10.1/a2", ISSN, "2021-02-01", ["u2", "u3"])
    put_article(client, "10.1/a3", ISSN, "2021-03-01", ["u1", "u3"])

    assert chain(client, "u1", "u3", QDATE)["hops"] == 1

    # withdraw the shortcut article -> BFS must find the 2-hop alternative
    assert client.post("/articles/10.1/a3/withdraw").status_code == 200
    r = chain(client, "u1", "u3", QDATE)
    assert r["connected"] is True and r["hops"] == 2
    assert r["path"] == ["u1", "u2", "u3"]
    assert [h["article_doi"] for h in r["chain"]] == ["10.1/a1", "10.1/a2"]
    # withdrawn DOI is still stored but absent from the evidence list
    assert client.get("/articles/10.1/a3").json()["status"] == "withdrawn"

    # correcting/reactivating restores the shortest path
    put_article(client, "10.1/a3", ISSN, "2021-03-01", ["u1", "u3"])
    assert chain(client, "u1", "u3", QDATE)["hops"] == 1


def test_no_connection_and_unknown_author_are_explicit(client):
    _two_packages_same_journal(client)
    put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    put_article(client, "10.1/iso", ISSN, "2021-05-01", ["u8", "u9"])

    r = chain(client, "u1", "u9", QDATE)
    assert r["connected"] is False and r["hops"] == 0 and r["chain"] == []
    assert r["path"] == [] and r["message"]

    r = chain(client, "u1", "ghost", QDATE)
    assert r["connected"] is False
    assert "unknown author" in r["message"]

    r = chain(client, "u1", "u1", QDATE)
    assert r["connected"] is True and r["hops"] == 0 and r["path"] == ["u1"]


def test_embargo_wall_moves_with_query_date(client):
    pkg = make_package(client, "WallPack")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 12)])
    put_article(client, "10.1/a1", ISSN, "2021-02-01", ["u1", "u2"])

    # one day before the wall reaches the publication date: blocked, no chain
    r = chain(client, "u1", "u2", "2022-01-31")
    assert r["connected"] is False
    assert r["chain"] == [] and r["message"]

    # wall = query date - 12 months = 2021-02-01 == publication date: open
    r = chain(client, "u1", "u2", "2022-02-01")
    assert r["connected"] is True and r["hops"] == 1
    assert r["chain"][0]["sources"][0]["embargo_cutoff"] == "2021-02-01"


def test_license_change_kills_hop_other_packages_keep_chain(client):
    # direct shortcut lives in journal 1, only PackA covers it;
    # the alternative 2-hop literature lives in journal 2 under PackB.
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    put_coverage(client, a, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    put_coverage(client, b, [entry(ISSN2, "2010-01-01", "2030-12-31", 0)])
    put_article(client, "10.1/direct", ISSN, "2021-03-01", ["u1", "u3"])
    put_article(client, "10.1/h1", ISSN2, "2021-01-01", ["u1", "u2"])
    put_article(client, "10.1/h2", ISSN2, "2021-02-01", ["u2", "u3"])

    assert chain(client, "u1", "u3", QDATE)["hops"] == 1

    # PackA's license window no longer covers the query date: direct hop dies,
    # the chain reroutes through PackB-backed articles
    client.put(f"/packages/{a}/license",
               json={"license_start": "2020-01-01", "license_end": "2025-12-31"})
    r = chain(client, "u1", "u3", QDATE)
    assert r["connected"] is True and r["hops"] == 2
    assert [h["article_doi"] for h in r["chain"]] == ["10.1/h1", "10.1/h2"]
    assert {s["package"] for h in r["chain"] for s in h["sources"]} == {"PackB"}

    # deactivate PackB as well -> no openable evidence at all
    client.post(f"/packages/{b}/deactivate")
    r = chain(client, "u1", "u3", QDATE)
    assert r["connected"] is False

    # restore license + reactivation -> shortest chain comes back
    client.put(f"/packages/{a}/license",
               json={"license_start": "2020-01-01", "license_end": "2030-12-31"})
    client.post(f"/packages/{b}/activate")
    assert chain(client, "u1", "u3", QDATE)["hops"] == 1


def test_chain_answer_uses_one_committed_version_under_interleaved_writes(client):
    import threading

    _two_packages_same_journal(client)
    put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    put_article(client, "10.1/a2", ISSN, "2021-02-01", ["u2", "u3"])
    put_article(client, "10.1/a3", ISSN, "2021-03-01", ["u1", "u3"])

    errors = []
    seen_versions = set()

    def reader():
        try:
            for _ in range(60):
                r = chain(client, "u1", "u3", QDATE)
                seen_versions.add(r["data_version"])
                # every answer must be internally coherent for its snapshot
                assert r["path"][0] == "u1" and r["path"][-1] == "u3"
                assert r["hops"] == len(r["chain"])
                for i, hop in enumerate(r["chain"]):
                    assert hop["from_author"] == r["path"][i]
                    assert hop["to_author"] == r["path"][i + 1]
                    assert len(hop["sources"]) >= 1  # evidence must be openable
                if r["hops"] == 1:
                    assert r["chain"][0]["article_doi"] == "10.1/a3"
                else:
                    assert [h["article_doi"] for h in r["chain"]] == ["10.1/a1", "10.1/a2"]
        except AssertionError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(15):
        client.post("/articles/10.1/a3/withdraw")
        put_article(client, "10.1/a3", ISSN, "2021-03-01", ["u1", "u3"])
    for t in threads:
        t.join()

    assert not errors
    assert len(seen_versions) >= 2  # readers really observed both states


def test_chain_version_tracks_access_version(client):
    _two_packages_same_journal(client)
    put_article(client, "10.1/a1", ISSN, "2021-01-01", ["u1", "u2"])
    r1 = chain(client, "u1", "u2", QDATE)
    r2 = query(client, ISSN, QDATE)
    assert r1["data_version"] == r2["data_version"]

    client.post("/articles/10.1/a1/withdraw")
    r3 = chain(client, "u1", "u2", QDATE)
    r4 = query(client, ISSN, QDATE)
    assert r3["data_version"] == r4["data_version"] == r1["data_version"] + 1
    assert r3["connected"] is False
