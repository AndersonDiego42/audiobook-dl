"""Confidence scoring for enrichment match candidates."""

from difflib import SequenceMatcher

from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.enrichment.models import (
    EnrichmentCandidate,
    _clean_asin,
    _clean_isbn,
    _normalize_person,
    _normalize_title,
)


def score_candidate_match(metadata: AudiobookMetadata, candidate: EnrichmentCandidate) -> float:
    """
    Compute a confidence score in `[0.0, 1.0]` measuring how closely
    `candidate` matches `metadata`.
    """
    q_asin = _clean_asin(metadata.asin)
    c_asin = _clean_asin(candidate.asin)
    if q_asin and c_asin and q_asin == c_asin:
        return 1.0

    q_isbn = _clean_isbn(metadata.isbn)
    c_isbn = _clean_isbn(candidate.isbn)
    if q_isbn and c_isbn and q_isbn == c_isbn:
        return 1.0

    q_title = _normalize_title(metadata.title or "")
    c_title = _normalize_title(candidate.title or "")
    if not q_title or not c_title:
        return 0.0

    if q_title == c_title:
        title_score = 1.0
    else:
        title_score = SequenceMatcher(None, q_title, c_title).ratio()
        if c_title.startswith(q_title + " ") or q_title.startswith(c_title + " "):
            title_score = max(title_score, 0.88)

    q_authors = [_normalize_person(a) for a in metadata.authors if _normalize_person(a)]
    c_authors = [_normalize_person(a) for a in candidate.authors if _normalize_person(a)]
    if q_authors and c_authors:
        best_author = 0.0
        for qa in q_authors:
            qa_tokens = set(qa.split())
            for ca in c_authors:
                seq = SequenceMatcher(None, qa, ca).ratio()
                ca_tokens = set(ca.split())
                if qa_tokens and ca_tokens:
                    overlap = len(qa_tokens & ca_tokens) / max(len(qa_tokens), len(ca_tokens))
                    seq = max(seq, overlap)
                if seq > best_author:
                    best_author = seq
        score = 0.65 * title_score + 0.35 * best_author
    elif q_authors and not c_authors:
        score = 0.85 * title_score
    else:
        score = title_score

    if (
        metadata.release_date is not None
        and candidate.release_date is not None
        and metadata.release_date.year == candidate.release_date.year
    ):
        score = min(1.0, score + 0.04)

    return round(max(0.0, min(1.0, score)), 4)
