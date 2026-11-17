from __future__ import annotations

# Internal imports
from . import networking
from audiobookdl import logging, Result, Audiobook
from audiobookdl.exceptions import DataNotPresent, GenericAudiobookDLException
from audiobookdl.utils import CustomSSLContextHTTPAdapter
from audiobookdl.utils.http import (
    create_impersonated_session,
    extract_session_cookies,
    update_session_cookies,
)

# External imports
import requests
from requests.adapters import HTTPAdapter
import lxml.html
from lxml.cssselect import CSSSelector
import re
import os
import weakref
from dataclasses import dataclass
from http.cookiejar import CookieJar, MozillaCookieJar
from typing import TYPE_CHECKING, Any, TypeVar, Generic
from ssl import SSLContext
import urllib3

if TYPE_CHECKING:
    from audiobookdl.options_schema import DownloadOptions

T = TypeVar("T")


def _close_sessions(
    primary: requests.Session | None,
    children: list[requests.Session] | None,
) -> None:
    """Close `primary` and all child sessions without raising."""
    if primary is not None and hasattr(primary, "close"):
        try:
            primary.close()
        except Exception:
            pass
    if children:
        for child in list(children):
            if hasattr(child, "close"):
                try:
                    child.close()
                except Exception:
                    pass
        children.clear()


@dataclass(frozen=True)
class CatalogBook:
    """Lightweight discovery entry returned by `Source.search` / `Source.list_library`."""

    title: str
    url: str
    author: str = ""
    series: str | None = None
    source: str = ""
    series_order: int | None = None
    item_type: str = "book"
    cover_url: str | None = None
    formats: tuple[str, ...] = ()

    @property
    def has_audiobook(self) -> bool:
        """Return `True` when this entry advertises an audiobook format."""
        return "audiobook" in self.formats

    @property
    def has_ebook(self) -> bool:
        """Return `True` when this entry advertises an ebook format."""
        return "ebook" in self.formats

    @property
    def has_video(self) -> bool:
        """Return `True` when this entry advertises a video format."""
        return "video" in self.formats


class Source(Generic[T]):
    """An abstract class for downloading audiobooks from a specific
    online source."""

    # Data required for logging in
    login_data: list[str] = [ "username", "password" ]
    # A list of regexes that indicates which website a service supports
    match: list[str] = []
    # list of names
    names: list[str] = []
    # Methods for authenticating
    _authentication_methods: list[str] = [ "cookies" ]
    # Create database directory for source
    create_storage_dir: bool = False
    # Discovery capabilities
    supports_search: bool = False
    supports_library: bool = False

    def __init__(self, options: DownloadOptions):
        self.__authenticated: bool = False
        self.__pages: dict[str, bytes] = {}
        self._options: DownloadOptions = options
        self.database_directory = os.path.join(options.database_directory, self.name)
        self.skip_downloaded = options.skip_downloaded
        self._session: requests.Session = self.create_session(options)
        self._child_sessions: list[requests.Session] = []
        self._session_finalizer = weakref.finalize(
            self,
            _close_sessions,
            self._session,
            self._child_sessions,
        )
        if self.create_storage_dir:
            os.makedirs(self.database_directory, exist_ok=True)

    def close(self) -> None:
        """Close the HTTP session and any unauthenticated CDN sessions held by this source."""
        finalizer = getattr(self, "_session_finalizer", None)
        if finalizer is not None:
            finalizer()
            return
        _close_sessions(
            getattr(self, "_session", None),
            getattr(self, "_child_sessions", None),
        )

    def __enter__(self) -> Source[T]:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


    @property
    def name(self) -> str:
        """Primary name of source"""
        return self.names[0].lower()


    @property
    def requires_authentication(self):
        """Returns `True` if this source requires authentication to download books"""
        return len(self._authentication_methods) > 0


    @property
    def authenticated(self):
        """Returns `True` if the source has been authenticated"""
        return self.__authenticated


    @property
    def supports_cookies(self):
        """Returns `True` if the source supports authentication with cookies"""
        return "cookies" in self._authentication_methods


    def load_cookie_file(self, cookie_file: str):
        """Loads non-expired cookies from a cookie file into session"""
        if self.supports_cookies:
            logging.debug(f"Loading cookies from '{cookie_file}'")
            cookie_jar = MozillaCookieJar()
            cookie_jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
            valid_jar = CookieJar()
            valid_count = 0
            for cookie in cookie_jar:
                # Netscape format serializes session cookies with expires=0.
                is_expired = bool(
                    cookie.expires is not None and cookie.expires > 0 and cookie.is_expired()
                )
                if not is_expired:
                    valid_jar.set_cookie(cookie)
                    valid_count += 1
            if valid_count > 0:
                update_session_cookies(self._session, valid_jar)
                self.__authenticated = True
            else:
                logging.debug(f"No valid non-expired cookies found in '{cookie_file}'")

    def cached_cookie_path(self) -> str:
        """Return the per-source, per-profile persistent session cookie jar file path."""
        profile = (getattr(self._options, "profile", None) or "").strip().lower()
        if profile and profile != "default":
            safe_profile = re.sub(r"[^a-z0-9_-]", "_", profile)
            return os.path.join(self.database_directory, f"session_cookies_{safe_profile}.txt")
        return os.path.join(self.database_directory, "session_cookies.txt")

    def save_cookie_file(self, cookie_file: str | None = None) -> str:
        """Atomically persist current session cookies in Netscape format and return path written."""
        from audiobookdl.utils.browser_cookies import export_cookies_netscape

        target = cookie_file or self.cached_cookie_path()
        cookies = extract_session_cookies(self._session)
        return export_cookies_netscape(cookies, target)

    def search(self, query: str) -> list[CatalogBook]:
        """Search the service catalog for audiobooks matching `query`."""
        raise NotImplementedError(f"{self.name} does not support catalog search")

    def list_library(self) -> list[CatalogBook]:
        """Return audiobook entries from the user's bookshelf/library or feed."""
        raise NotImplementedError(f"{self.name} does not support library listing")

    def verify_auth(self, url: str = "") -> bool:
        """Return True if authentication credentials/cookies appear valid."""
        if not self.requires_authentication:
            return True
        return self.authenticated


    @property
    def supports_login(self):
        """Returns `True` if the source supports authentication with login"""
        return "login" in self._authentication_methods


    def _login(self, url: str, username: str, password: str):
        pass


    def login(self, url: str, **kwargs) -> None:
        """Authenticate with source using username and password"""
        if self.supports_login:
            logging.debug("Logging in")
            self._login(url, **kwargs)
            self.__authenticated = True


    def download_from_id(self, book_id: T) -> Result:
        """Download book specified by id"""
        raise NotImplementedError


    def download(self, url: str) -> Result:
        """Download book or series"""
        raise NotImplementedError
    
    
    def on_download_complete(self, audiobook: Audiobook):
        """Called after the download is complete"""
        pass


    def _get_page(self, url: str, use_cache: bool = True, **kwargs) -> bytes:
        """Download a page and caches it"""
        if not use_cache:
            return self.get(url, **kwargs)
        if url not in self.__pages:
            self.__pages[url] = self.get(url, **kwargs)
        return self.__pages[url]


    def find_elem_in_page(self, url: str, selector: str, data=None, **kwargs):
        """
        Find the first html element in page from `url` that matches `selector`.
        Will return the attribute specified in `data`. Will return element text
        if `data` is `None`.
        Will cache the page.
        """
        results = self.find_elems_in_page(url, selector, **kwargs)
        if len(results) == 0:
            logging.debug(f"Could not find matching element from {url} with {selector}")
            raise DataNotPresent
        elem = results[0]
        if data is None:
            return elem.text
        return elem.get(data)


    def find_elems_in_page(self, url: str, selector: str, **kwargs) -> Any:
        """
        Find all html elements in the page from `url` that's matches `selector`.
        Will cache the page.
        """
        sel = CSSSelector(selector)
        page: bytes = self._get_page(url, **kwargs)
        tree = lxml.html.fromstring(page.decode("utf8"))
        results = sel(tree)
        return results


    def find_in_page(self, url: str, regex: str, group_index: int = 0, **kwargs) -> str:
        """
        Find some text in a page based on a regex.
        Will cache the page.
        """
        page = self._get_page(url, **kwargs).decode("utf8")
        m = re.search(regex, page)
        if m is None:
            logging.debug(f"Could not find match from {url} with {regex}")
            raise DataNotPresent
        return m.group(group_index)


    def find_all_in_page(self, url: str, regex: str, **kwargs) -> list:
        """
        Find all places in a page that matches the regex.
        Will cache the page.
        """
        return re.findall(regex, self._get_page(url, **kwargs).decode("utf8"))

    # Networking
    request = networking.request
    post = networking.post
    get = networking.get
    post_json = networking.post_json
    get_json = networking.get_json
    get_stream_files = networking.get_stream_files

    def create_ssl_context(self, options: DownloadOptions | None = None) -> SSLContext:
        try:
            ssl_context: SSLContext = urllib3.util.create_urllib3_context()  # type: ignore[attr-defined]

            # Workaround for regression in requests version 2.32.3
            # https://github.com/psf/requests/issues/6730
            ssl_context.load_default_certs()

            # Prevent the padding extension from appearing in the TLS ClientHello
            # It's used by Cloudflare for bot detection
            # See issue #106
            ssl_context.options &= ~(1 << 4) # SSL_OP_TLSEXT_PADDING
            return ssl_context
        except AttributeError as error: # AttributeError: module 'urllib3.util' has no attribute 'create_urllib3_context'
            raise GenericAudiobookDLException(
                "Please update urllib3 to version >= 2 using the command 'pip install -U urllib3'"
            ) from error

    def create_session(self, options: DownloadOptions | None = None) -> requests.Session:
        proxy = getattr(options, "proxy", None) if options is not None else None
        impersonate = (getattr(options, "impersonate", None) or "").strip() if options is not None else ""
        if impersonate:
            return create_impersonated_session(impersonate, proxy=proxy)
        session = requests.Session()
        ssl_context: SSLContext = self.create_ssl_context(options)
        session.mount(
            "https://",
            CustomSSLContextHTTPAdapter(ssl_context, pool_connections=20, pool_maxsize=20),
        )
        session.mount(
            "http://",
            HTTPAdapter(pool_connections=20, pool_maxsize=20),
        )
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def create_unauthenticated_session(self) -> requests.Session:
        """Create a fresh pooled session without auth headers or cookies for CDN requests."""
        session = self.create_session(getattr(self, "_options", None))
        child_sessions = getattr(self, "_child_sessions", None)
        if isinstance(child_sessions, list):
            child_sessions.append(session)
        return session
