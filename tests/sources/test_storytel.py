from datetime import date

import pytest

from audiobookdl.args import build_default_options
from audiobookdl.exceptions import BookNotReleased
from audiobookdl.sources.storytel import StorytelSource


def test_parse_url():
    book_id = StorytelSource.get_id_from_url("https://www.storytel.com/se/sv/books/shantaram-1404854")
    assert book_id == "1404854"


def test_parse_url_with_params():
    book_id = StorytelSource.get_id_from_url("https://www.storytel.com/se/sv/books/shantaram-1404854?appRedirect=true")
    assert book_id == "1404854"


def test_get_metadata_uses_abook_format_not_first_format(tmp_path):
    src = StorytelSource(build_default_options(database_directory=str(tmp_path)))
    book_details = {
        "title": "Sample Book",
        "shareUrl": "https://www.storytel.com/se/sv/books/sample-1",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "formats": [
            {
                "type": "ebook",
                "isReleased": False,
                "publisher": {"name": "Ebook Publisher"},
                "releaseDate": "2020-01-01T00:00:00Z",
            },
            {
                "type": "abook",
                "isReleased": True,
                "publisher": {"name": "Audiobook Publisher"},
                "releaseDate": "2022-05-15T00:00:00Z",
            },
        ],
    }

    metadata = src.get_metadata(book_details)
    assert metadata.publisher == "Audiobook Publisher"
    assert metadata.release_date == date(2022, 5, 15)


def test_get_metadata_checks_is_released_on_abook_format(tmp_path):
    src = StorytelSource(build_default_options(database_directory=str(tmp_path)))
    book_details = {
        "title": "Unreleased Audiobook",
        "shareUrl": "https://www.storytel.com/se/sv/books/unreleased-2",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "formats": [
            {
                "type": "ebook",
                "isReleased": True,
                "publisher": {"name": "Ebook Publisher"},
                "releaseDate": "2020-01-01T00:00:00Z",
            },
            {
                "type": "abook",
                "isReleased": False,
                "publisher": {"name": "Audiobook Publisher"},
                "releaseDate": "2030-01-01T00:00:00Z",
            },
        ],
    }

    with pytest.raises(BookNotReleased):
        src.get_metadata(book_details)


