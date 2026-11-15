from typing import Optional, Tuple
import requests


DEFAULT_HTTP_TIMEOUT: Tuple[int, int] = (15, 60)
DEFAULT_USER_AGENT: str = "audiobook-dl"


def redirect_of(
    url: str,
    amount: int = 1,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    Get the location where a url redirects to.

    :param url: the http endpoint
    :param amount: the amount of times the function is allowed to redirect
    :param session: optional `requests.Session` to preserve proxies, headers, and TLS settings
    :returns: the redirected location
    """
    getter = session.get if session is not None else requests.get
    for _ in range(amount):
        response = getter(
            url,
            allow_redirects=False,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if response.status_code not in [301, 302]:
            return None
        if "location" not in response.headers:
            return None
        url = response.headers["location"]
    return url
