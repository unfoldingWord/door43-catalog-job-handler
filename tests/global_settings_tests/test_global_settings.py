import unittest

from app_settings.app_settings import AppSettings


class TestAppSettings(unittest.TestCase):

    def test_init(self):
        dcs_domain = 'my.dcs.org'
        AppSettings(dcs_domain=dcs_domain)
        self.assertEqual(AppSettings.dcs_domain, dcs_domain)

    def test_prefix_vars(self):
        AppSettings(prefix='')
        self.assertEqual(AppSettings.name, 'Door43-Catalog-Job-Handler')
        AppSettings(prefix='test-')
        self.assertEqual(AppSettings.name, 'test-Door43-Catalog-Job-Handler')
        AppSettings(prefix='test2-')
        self.assertEqual(AppSettings.name, 'test2-Door43-Catalog-Job-Handler')
        AppSettings(prefix='')
        self.assertEqual(AppSettings.name, 'Door43-Catalog-Job-Handler')

    def test_reset_app(self):
        default_name = AppSettings.name
        AppSettings(name='test-name')
        AppSettings()
        self.assertEqual(AppSettings.name, default_name)
        AppSettings.name = 'test-name-2'
        AppSettings(name='test-name-2', reset=False)
        self.assertNotEqual(AppSettings.name, default_name)
