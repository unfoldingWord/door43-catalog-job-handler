import unittest

from sqlalchemy import Column, Integer, String
from moto import mock_dynamodb2, mock_s3

from app_settings.app_settings import AppSettings
from models.tx_model import TxModel


class User(AppSettings.Base, TxModel):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    fullname = Column(String)
    password = Column(String)


class TestAppSettings(unittest.TestCase):

    def test_init(self):
        gogs_url = 'https://my.gogs.org'
        AppSettings(gogs_url=gogs_url)
        self.assertEqual(AppSettings.gogs_url, gogs_url)

    def test_construction_connection_string(self):
        """
        Test the construction of the connection string with multiple attributes
        """
        AppSettings(db_protocol='protocol', db_user='user', db_pass='pass', db_end_point='my.endpoint.url', db_port='9999',
            db_name='db', db_connection_string_params='charset=utf8', auto_setup_db=False)
        expected = "protocol://user:pass@my.endpoint.url:9999/db?charset=utf8"
        connection_str = AppSettings.construct_connection_string()
        self.assertEqual(connection_str, expected)

    def test_db(self):
        AppSettings(db_connection_string='sqlite:///:memory:')
        AppSettings.db_create_tables([User.__table__])
        user = User(name='ed', fullname='Edward Scissorhands', password='12345')
        user.insert()
        user_from_db = User.get(name='ed')
        self.assertIsNotNone(user_from_db)
        self.assertEqual(user_from_db.password, '12345')

    def test_setup_db_with_connection_string_parts(self):
        AppSettings(db_protocol='sqlite', db_user=None, db_pass=None, db_end_point=None, db_port=None, db_name=':memory:',
            db_connection_string_params=None)
        AppSettings.db_create_tables([User.__table__])
        user = User(name='ed', fullname='Edward Scissorhands', password='12345')
        user.insert()
        user_from_db = User.get(name='ed')
        self.assertIsNotNone(user_from_db)
        self.assertEqual(user_from_db.password, '12345')
        AppSettings.db_close()

    @mock_s3
    def test_s3_handler(self):
        self.assertIsNotNone(AppSettings.cdn_s3_handler())

    # @mock_dynamodb2
    # def test_dynamodb_handler(self):
    #     self.assertIsNotNone(AppSettings.language_stats_db_handler())

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
