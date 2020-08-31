import wandb
import setup
import os
import sys

# Add current directory so we can run this as a module
sys.path.append(os.path.dirname(__file__))
from test_scripts.media.util import all_tests, project_name



def test_all_media():
    # run_path = run_tests()
    # with open('data.txt', 'r') as file:
    #     data = file.read().replace('\n', '')

    api = wandb.Api()
    run_path = setup.test_user["username"] + "/" + project_name
    runs = api.run(run_path)
    run = runs[-1]

    # Test history Data
    history = run.history()
    for index, row in history.iterrows():
        # print(row)
        for k, v in all_tests.items():
            history_object = row[k]
            # TODO: Test file data 
            # SOLUTION: Add an assert that compares the sha of a file downloading via the path variable in history
            #           and the local files

            assert history_object != None

