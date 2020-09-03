import os
import sys
import shutil
import time

# Add current directory so we can run this as a module
sys.path.append(os.path.dirname(__file__))
from test_scripts.media.util import all_tests, project_name

import setup
import wandb
import hashlib
import datetime
import requests

from wandb.old.retry import retriable
from gql.client import RetryError

def get_history_file(run, history_object):
    file = run.file(history_object['path'])
    r = requests.get(file.url, timeout=20)
    max_retry = 7
    i = 0
    timeout = 2
    # Gorilla is returning 404 for a bit after upload
    while r.status_code != 200:
        print("Retrying Status Code: " + str(r.status_code))
        print("--- Content ---")
        print(r.content)
        r = requests.get(file.url)
        time.sleep(timeout)

    return file.download(WANDB_TEST_TEMP_DIR, replace=True)


WANDB_TEST_TEMP_DIR = '/tmp/wandb_media_test'
def test_all_media():
    # Cleanup old test
    if os.path.exists(WANDB_TEST_TEMP_DIR):
        shutil.rmtree(WANDB_TEST_TEMP_DIR)

    api = wandb.Api()
    run_path = setup.test_user["username"] + "/" + project_name
    runs = api.runs(run_path)

    run = list(runs)[-1]

    # Test history Data
    history = run.history()
    for index, row in history.iterrows():
        # print(row)
        for k, v in all_tests.items():
            history_object = row[k]

            if not 'path' in history_object:
                continue

            file = get_history_file(run, history_object)

            with open(file.name, 'rb') as f:
                # This asserts we don't corrupt the file in upload or download.
                # The sha256 in the metadata is created from the file before it
                # leaves the client machhine. We download the file and calculate the
                # new sha to confirm now file corruption between write, upload, and download,
                sha256 = hashlib.sha256(f.read()).hexdigest()
                assert sha256 == history_object['sha256']

            assert history_object is not None