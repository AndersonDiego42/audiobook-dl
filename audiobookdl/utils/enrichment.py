from contextlib import nullcontext
from datetime import date
import re
from typing import Any, Optional

import requests

from audiobookdl import logging
from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.http import DEFAULT_USER_AGENT


def resolve_redirect_url(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
) -> str:
    """Follow HTTP redirects on short/share links to obtain the canonical target URL."""
    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        return url
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    session_cm = nullcontext(session) if session is not None else requests.Session()
    try:
        with session_cm as sess:
            resp = sess.head(
                cleaned,
                allow_redirects=True,
                timeout=timeout,
                proxies=proxies,
                headers=headers,
            )
            if resp.status_code in (403, 405) or resp.url == cleaned:
                resp = sess.get(
                    cleaned,
                    allow_redirects=True,
                    stream=True,
                    timeout=timeout,
                    proxies=proxies,
                    headers=headers,
                )
                try:
                    final_url = resp.url or cleaned
                finally:
                    resp.close()
                return final_url
            return resp.url or cleaned
    except requests.RequestException:
        return url


def _extract_first_isbn(doc: dict[str, Any]) -> Optional[str]:
    isbns = doc.get("isbn") or []
    for raw in isbns:
        candidate = re.sub(r"[^0-9Xx]", "", str(raw))
        if len(candidate) in (10, 13):
            return candidate
    return None


def enrich_metadata_from_openlibrary(
    metadata: AudiobookMetadata,
    *,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """
    Populate missing `AudiobookMetadata` fields via the OpenLibrary public search API.

    Existing non-empty fields on `metadata` are preserved; network or parse errors
    log a debug message and return `False` without failing the download.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    params: dict[str, Any] = {"limit": 1}
    if metadata.isbn:
        params["isbn"] = metadata.isbn
    else:
        params["title"] = metadata.title
        if metadata.authors:
            params["author"] = metadata.authors[0]

    session_cm = nullcontext(session) if session is not None else requests.Session()
    try:
        with session_cm as sess:
            resp = sess.get(
                "https://openlibrary.org/search.json",
                params=params,
                timeout=timeout,
                proxies=proxies,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logging.debug(f"OpenLibrary metadata lookup skipped: {exc}")
        return False

    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not docs or not isinstance(docs, list) or not isinstance(docs[0], dict):
        return False
    doc = docs[0]
    updated = False

    if not metadata.isbn:
        isbn = _extract_first_isbn(doc)
        if isbn:
            metadata.isbn = isbn
            updated = True
    if not metadata.publisher:
        publishers = doc.get("publisher") or []
        if publishers:
            metadata.publisher = str(publishers[0])
            updated = True
    if metadata.release_date is None:
        year = doc.get("first_publish_year")
        if isinstance(year, int) and 1 <= year <= 9999:
            metadata.release_date = date(year, 1, 1)
            updated = True
    if not metadata.genres:
        subjects = [str(s) for s in (doc.get("subject") or [])[:5] if str(s).strip()]
        if subjects:
            metadata.add_genres(subjects)
            updated = True
    if not metadata.description and isinstance(doc.get("first_sentence"), (list, str)):
        sentence = doc["first_sentence"]
        text = sentence[0] if isinstance(sentence, list) and sentence else sentence
        if isinstance(text, str) and text.strip():
            metadata.description = text.strip()
            updated = True
    return updated
