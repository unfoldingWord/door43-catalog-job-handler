# This is used to test running the webhook job from your dev environment.
# `python tests/dry_run.py tests/resources/webhook_release.json`
# If you are missing required environment variables, the script will raise an error
import tempfile
from webhook import process_webhook_job
import sys
import json

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Syntax: webhook.py <payload_file>.json")
        exit(1)
    tempfile.tempdir = '/tmp'
    print(sys.argv[1])
    with open(sys.argv[1]) as f:
        data = json.load(f)
    process_webhook_job(data)
