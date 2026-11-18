"""Pydantic request/response models."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator

_ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dXx]$")
_DOI_RE = re.compile(r"^10\.\S+/\S+$")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                 "http://dx.doi.org/", "doi:")


def normalize_issn(value: str) -> str:
    """Accept ``12345678`` / ``1234-5678`` / ``1234-567X``; store ``1234-567X``."""
    if not isinstance(value, str):
        raise ValueError("ISSN must be a string")
    v = value.strip().upper()
    if not _ISSN_RE.match(v):
        raise ValueError("invalid ISSN: expected 7 digits + digit/X, e.g. 1234-5678")
    return f"{v[:4]}-{v[-4:]}"


def normalize_doi(value: str) -> str:
    """Strip URL/``doi:`` prefixes and whitespace; store the bare DOI lower-cased."""
    if not isinstance(value, str):
        raise ValueError("DOI must be a string")
    v = value.strip()
    for prefix in _DOI_PREFIXES:
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
            break
    v = v.strip().lower()
    if not _DOI_RE.match(v):
        raise ValueError("invalid DOI: expected '10.PREFIX/suffix', e.g. 10.1000/xyz123")
    return v


class CoverageEntry(BaseModel):
    issn: str
    start_date: date
    end_date: date
    embargo_months: int = Field(default=0, ge=0)

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)

    @model_validator(mode="after")
    def _range(self) -> "CoverageEntry":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CoverageSnapshot(BaseModel):
    """Full replacement snapshot for one package (empty list clears it)."""

    entries: List[CoverageEntry]


class PackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "PackageCreate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class LicenseUpdate(BaseModel):
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "LicenseUpdate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class PackageOut(BaseModel):
    id: int
    name: str
    license_start: date
    license_end: date
    active: bool
    coverage_rows: int


class SegmentOut(BaseModel):
    start: date
    end: date
    sources: List[str]


class AccessResponse(BaseModel):
    issn: str
    date: date
    data_version: int
    segments: List[SegmentOut]


# ---------------------------------------------------------------- articles

class ArticleUpsert(BaseModel):
    """Full metadata for one DOI. Re-submitting the same DOI corrects it:
    the whole row (including its author list) is replaced atomically."""

    issn: str
    publication_date: date
    authors: List[str] = Field(min_length=1)

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)

    @field_validator("authors")
    @classmethod
    def _authors(cls, v: List[str]) -> List[str]:
        cleaned = [a.strip() for a in v]
        if any(not a for a in cleaned):
            raise ValueError("author ids must be non-empty strings")
        if any("\x1f" in a for a in cleaned):
            raise ValueError("author ids must not contain the US control character (0x1f)")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate author id in author list")
        return cleaned


class ArticleOut(BaseModel):
    doi: str
    issn: str
    publication_date: date
    status: str
    authors: List[str]
    updated_at: datetime
    data_version: int


class ArticleSource(BaseModel):
    """One package whose coverage makes the article openable on the query date."""

    package_id: int
    package: str
    coverage_start: date
    coverage_end: date
    embargo_months: int
    embargo_cutoff: date


class ChainHop(BaseModel):
    from_author: str
    to_author: str
    article_doi: str
    issn: str
    publication_date: date
    sources: List[ArticleSource]


class CoauthorChainResponse(BaseModel):
    author_a: str
    author_b: str
    date: date
    data_version: int
    connected: bool
    hops: int = Field(description="Number of co-authorship hops; 0 when not connected")
    path: List[str] = Field(description="Author ids along the connection, endpoint to endpoint")
    chain: List[ChainHop]
    message: str
