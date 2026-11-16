from audiobookdl.exceptions import NoSourceFound
from audiobookdl.sources import find_compatible_source
import pytest

TEST_DATA = {
    "https://www.audiobooks.com/book/stream/413879": "Audiobooksdotcom",
    "https://www.audiobooks.com/browse/library": "Audiobooksdotcom",
    "https://www.bookbeat.no/bok/somethingsomething-999999": "BookBeat",
    "https://bibliotek.kk.dk/work/work-of:870970-basis:53978223": "Ereolen",
    "https://www.aakb.dk/work/work-of:870970-basis:53978223": "Ereolen",
    "https://www.everand.com/listen/579426746": "Everand",
    "https://www.chirpbooks.com/player/11435746": "Chirp",
    "https://librivox.org/library-of-the-worlds-best-literature-ancient-and-modern-volume-3-by-various/": "Librivox",
    "https://www.nextory.no/bok/somethingsomethingsomething-99999999/": "Nextory",
    "https://ofs-d2b6150a9dec641552f953da2637d146.listen.overdrive.com/?d=...": "Overdrive",
    "https://www.scribd.com/listen/579426746": "Everand",
    "https://www.storytel.com/no/nn/books/somethingsomething-9999999": "Storytel",
}

def test_url_to_source():
    for url, source_name in TEST_DATA.items():
        source = find_compatible_source(url)
        assert source.__name__ == source_name + "Source"


def test_ereolen_rejects_empty_domain_and_unlisted_host():
    with pytest.raises(NoSourceFound):
        find_compatible_source("https://www./work/work-of:870970-basis:53978223")
    with pytest.raises(NoSourceFound):
        find_compatible_source("https://evil-aakb.dk/work/work-of:870970-basis:53978223")


def test_ereolen_refreshes_library_domains_once_on_unknown_dk_url(monkeypatch):
    import re
    import audiobookdl.sources as sources_mod
    from audiobookdl.sources.ereolen import EreolenSource

    orig_domains = list(EreolenSource.library_domains)
    orig_match = list(EreolenSource.match)
    orig_refreshed = sources_mod._EREOLEN_REFRESHED
    calls = []

    def fake_refresh():
        calls.append(1)
        EreolenSource.library_domains = [*orig_domains, "nybibliotek.dk"]
        EreolenSource.match = [
            rf"https://(www\.)?({'|'.join(re.escape(d) for d in EreolenSource.library_domains)})/work/work-of:.+"
        ]
        return list(EreolenSource.library_domains)

    try:
        sources_mod._EREOLEN_REFRESHED = False
        monkeypatch.setattr(EreolenSource, "refresh_library_domains", staticmethod(fake_refresh))
        src = find_compatible_source("https://nybibliotek.dk/work/work-of:870970-basis:123456")
        assert src is EreolenSource
        assert len(calls) == 1
        with pytest.raises(NoSourceFound):
            find_compatible_source("https://still-unknown.dk/work/work-of:1")
        assert len(calls) == 1
    finally:
        sources_mod._EREOLEN_REFRESHED = orig_refreshed
        EreolenSource.library_domains = orig_domains
        EreolenSource.match = orig_match


def test_resolve_service_label_uses_registry_and_fallbacks():
    from audiobookdl.sources import get_available_services, resolve_service_label

    by_key = {svc.key: svc for svc in get_available_services()}
    assert resolve_service_label("https://www.storytel.com/en/books/1", by_key) == by_key["storytel"].label
    assert resolve_service_label("https://librivox.org/book") == "librivox"
    assert resolve_service_label("https://unknown.example.invalid/book", by_key) == "Unknown"

