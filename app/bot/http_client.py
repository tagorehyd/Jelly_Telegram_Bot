import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HTTP_TIMEOUT = 10
POLL_LONG_TIMEOUT = 60
POLL_TIMEOUT = 70


def create_session():
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET", "POST", "DELETE"},
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


HTTP_SESSION = create_session()
