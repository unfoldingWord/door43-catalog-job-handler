import os
import unittest

from general_tools import url_utils


class Mock_urlopen:
    def __init__(self, url):
        self.result = ("hello " + url).encode("ascii")  # A bytes object

    # def __enter__(self):
    # pass
    # def __exit__(self, a, b, c):
    # pass
    def read(self):
        return self.result

    def close(self):
        pass


class UrlUtilsTests(unittest.TestCase):

    def setUp(self):
        """Runs before each test."""
        self.tmp_file = ""

    def tearDown(self):
        """Runs after each test."""
        # delete temp files
        if os.path.isfile(self.tmp_file):
            os.remove(self.tmp_file)

    # @staticmethod
    # def mock_urlopen(url):
    # return ("hello " + url).encode("ascii") # A bytes object

    @staticmethod
    def raise_urlopen(url):
        raise IOError("An error occurred")

    def test_get_url(self):
        self.assertEqual(url_utils._get_url("world",
                                            catch_exception=False,
                                            urlopen=Mock_urlopen),
                         "hello world")
        self.assertEqual(url_utils._get_url("world",
                                            catch_exception=True,
                                            urlopen=Mock_urlopen),
                         "hello world")

    def test_get_url_error(self):
        self.assertFalse(url_utils._get_url("world",
                                            catch_exception=True,
                                            urlopen=self.raise_urlopen))
        self.assertRaises(IOError, url_utils._get_url,
                          "world", catch_exception=False, urlopen=self.raise_urlopen)
