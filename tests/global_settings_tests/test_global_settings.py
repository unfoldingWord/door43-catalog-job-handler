import unittest

from sqlalchemy import Column, Integer, String
from app_settings.app_settings import AppSettings


class TestAppSettings(unittest.TestCase):

    def test_init(self):
        gogs_url = 'https://my.gogs.org'
        AppSettings(gogs_url=gogs_url)
        self.assertEqual(AppSettings.gogs_url, gogs_url)

    def test_prefix_vars(self):
        AppSettings(prefix='')
        self.assertEqual(AppSettings.cdn_bucket_name, 'cdn.door43.org')
        self.assertEqual(AppSettings.api_url, 'https://api.door43.org')
        AppSettings(prefix='test-')
        self.assertEqual(AppSettings.cdn_bucket_name, 'test-cdn.door43.org')
        self.assertEqual(AppSettings.api_url, 'https://test-api.door43.org')
        AppSettings(prefix='test2-')
        self.assertEqual(AppSettings.cdn_bucket_name, 'test2-cdn.door43.org')
        self.assertEqual(AppSettings.api_url, 'https://test2-api.door43.org')
        AppSettings(prefix='')
        self.assertEqual(AppSettings.cdn_bucket_name, 'cdn.door43.org')
        self.assertEqual(AppSettings.api_url, 'https://api.door43.org')

    def test_reset_app(self):
        default_name = AppSettings.name
        AppSettings(name='test-name')
        AppSettings()
        self.assertEqual(AppSettings.name, default_name)
        AppSettings.name = 'test-name-2'
        AppSettings(name='test-name-2', reset=False)
        self.assertNotEqual(AppSettings.name, default_name)
