import json
from unittest import TestCase, skip
from unittest.mock import patch

from app_settings.app_settings import AppSettings
from rq_settings import prefix, webhook_queue_name
from webhook import process_webhook_job


def my_get_current_job():
    class Result:
        id = 12345
        origin = webhook_queue_name
        connection = None

    return Result()


class TestWebhook(TestCase):

    def setUp(self):
        # Make sure that other tests didn't mess up our prefix
        AppSettings(prefix=prefix)

    def test_prefix(self):
        self.assertEqual(prefix, AppSettings.prefix)

    @patch('webhook.get_current_job', side_effect=my_get_current_job)
    def test_typical_full_payload(self, mocked_get_current_job_function):
        with open('tests/resources/webhook_release.json', 'rt') as json_file:
            payload_json = json.load(json_file)
        process_webhook_job(payload_json)
