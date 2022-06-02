import shutil
import ssl
import urllib.request as urllib2
from contextlib import closing
from time import sleep
from typing import Callable
from urllib.error import HTTPError

from app_settings.app_settings import AppSettings


def download_file(url: str, outfile: str) -> None:
    """Downloads a file and saves it."""
    _download_file(url, outfile, urlopen=urllib2.urlopen)


def _download_file(url: str, outfile: str, urlopen: Callable[[str], bytes]) -> None:
    """
    Handles "HTTP Error 503: Service Unavailable" internally with an automatic wait and retry.
    """
    AppSettings.logger.debug(f"_download_file( {url}, outfile={outfile}, …)…")
    MAX_TRIES = 5
    INITIAL_WAIT_TIME = 5  # seconds
    num_tries = 0
    while True:
        num_tries += 1
        if num_tries > 1:
            AppSettings.logger.debug(f"  _download_file try #{num_tries}…")
        need_to_wait = False
        # err:Optional[Exception] = None

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with closing(urlopen(url)) as request:
                with open(outfile, 'wb') as fp:
                    shutil.copyfileobj(request, fp)
        except HTTPError as e:
            if num_tries < MAX_TRIES \
                    and "HTTP Error 503: Service Unavailable" in str(e):
                saved_e = e
                need_to_wait = True
            else:
                raise e
        except IOError as e:
            error_message = f"Error retrieving {url}: {e}"
            AppSettings.logger.critical(error_message)
            raise IOError(error_message)
        if not need_to_wait \
                or num_tries >= MAX_TRIES:
            break

        adjusted_wait_time = INITIAL_WAIT_TIME * num_tries  # Make the wait progressively longer
        AppSettings.logger.warning(f"  _download_file: Waiting {adjusted_wait_time}s to fetch {url} after {saved_e}…")
        sleep(adjusted_wait_time)  # Then try again
    # end of loop
# end of _download_file function
