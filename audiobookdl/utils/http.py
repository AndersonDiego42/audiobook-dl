from typing import Optional, Tuple
import requests


DEFAULT_HTTP_TIMEOUT: Tuple[int, int] = (15, 60)
DEFAULT_USER_AGENT: str = "audiobook-dl"


def redirect_of(url: str, amount: int = 1) -> Optional[str]:
    """
    Get the location where a url redirects to

    :param url: the http endpoint
    :param amount: the amount of times the function is allowed to redirect
    :returns: the redirected location
    """
    for _ in range(amount):
        response = requests.get(
            url,
            allow_redirects=False,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if response.status_code not in [ 301, 302 ]:
            return None
        if "location" not in response.headers:
            return None
        url = response.headers["location"]
    return url
