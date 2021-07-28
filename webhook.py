# DOOR43 WEBHOOK
#
# NOTE: This module name and function name are defined by the rq package and our own door43-enqueue-job package
# This code adapted by RJH June 2018 from tx-manager/client_webhook/ClientWebhook/process_webhook

# NOTE: rq_settings.py is executed at program start-up, reads some environment variables, and sets queue name, etc.
#       job() function (at bottom here) is executed by rq package when there is an available entry in the named queue.

import os
import shutil
import tempfile
import traceback
from time import time, sleep
# Python imports
from typing import Dict, Tuple, Any, Optional
from urllib.error import HTTPError
from zipfile import BadZipFile

# Library (PyPI) imports
from rq import get_current_job, Queue
from statsd import StatsClient  # Graphite front-end

from app_settings.app_settings import AppSettings
from general_tools.file_utils import unzip, empty_folder
from general_tools.url_utils import download_file
# Local imports
from rq_settings import prefix, debug_mode_flag, tx_post_url, webhook_queue_name

OUR_NAME = 'Door43_catalog_job_handler'
KNOWN_RESOURCE_SUBJECTS = ('Generic_Markdown',
                           'Greek_Lexicon', 'Hebrew-Aramaic_Lexicon',
                           # and 14 from https://api.door43.org/v3/subjects (last checked Mar 2020)
                           'Bible', 'Aligned_Bible', 'Greek_New_Testament', 'Hebrew_Old_Testament',
                           'Translation_Academy', 'Translation_Questions', 'Translation_Words',
                           'Translation_Notes', 'TSV_Translation_Notes',
                           'Open_Bible_Stories', 'OBS_Study_Notes', 'OBS_Study_Questions',
                           'OBS_Translation_Notes', 'OBS_Translation_Questions',
                           )
# A similar table also exists in tx-enqueue-job:check_posted_tx_payload.py
# TODO: Will we also need 'book' in this map below???
RESOURCE_SUBJECT_MAP = {
    # Maps from rc.resource.identifier and possibly also from rc.resource.type
    'obs': 'Open_Bible_Stories',
    'obs-sn': 'OBS_Study_Notes',
    'obs-sq': 'OBS_Study_Questions',
    'obs-tn': 'OBS_Translation_Notes',
    'obs-tq': 'OBS_Translation_Questions',
    'obs-sg': 'Generic_Markdown',  # See if this works for OBS Study Guide

    'bible': 'Bible', 'reg': 'Bible',
    'ulb': 'Bible', 'udb': 'Bible',  # These sometimes don't have the correct subject in the manifest

    'ta': 'Translation_Academy',
    'tn': 'Translation_Notes',
    'tq': 'Translation_Questions',
    'tw': 'Translation_Words',

    'ugl': 'Greek_Lexicon',  # Subject for en_ugl is 'Greek English Lexicon' but we want to stay more generic
    'uhal': 'Hebrew-Aramaic_Lexicon',

    # TODO: Have I got these next two correct???
    # 'help':'Translation_Academy',
    # 'man':'Translation_Academy',
}

AppSettings(prefix=prefix)
if prefix not in ('', 'dev-'):
    AppSettings.logger.critical(f"Unexpected prefix: '{prefix}' — expected '' or 'dev-'")
door43_stats_prefix = f"door43-catalog.{'dev' if prefix else 'prod'}"
job_handler_stats_prefix = f"{door43_stats_prefix}.job-handler"
webhook_stats_prefix = f'{job_handler_stats_prefix}.webhook'
prefixed_our_name = prefix + OUR_NAME

long_prefix = 'develop' if prefix else 'git'
DOOR43_CALLBACK_URL = f'https://{long_prefix}.door43.org/client/webhook/tx-callback/'
ADJUSTED_DOOR43_CALLBACK_URL = 'http://127.0.0.1:8080/tx-callback/' \
    if prefix and debug_mode_flag and ':8090' in tx_post_url \
    else DOOR43_CALLBACK_URL

# Get the Graphite URL from the environment, otherwise use a local test instance
graphite_url = os.getenv('GRAPHITE_HOSTNAME', 'localhost')
stats_client = StatsClient(host=graphite_url, port=8125)


def download_and_unzip_repo(base_temp_dir_name: str, commit_url: str, repo_dir: str) -> None:
    """
    Downloads and unzips a git repository from Github or git.door43.org
        Has a number of tries
            (in case that Gitea hasn't actually finished building the .zip file yet)

    :param commit_url: The URL of the repository to download
    :param repo_dir:   The directory where the downloaded file should be unzipped
    :return: None
    """
    repo_zip_url = commit_url if commit_url.endswith('.zip') \
        else commit_url.replace('commit', 'archive') + '.zip'
    repo_zip_file = os.path.join(base_temp_dir_name, repo_zip_url.rpartition(os.path.sep)[2])

    MAX_TRIES = 4
    SECONDS_BETWEEN_TRIES = 5
    AppSettings.logger.info(f"Downloading and unzipping repo from {repo_zip_url} …")
    try_number = 1
    while True:
        if try_number > 1:
            AppSettings.logger.warning(f"Try {try_number}: Downloading and unzipping repo from {repo_zip_url} …")
        try:
            # If the file already exists, remove it, we want a fresh copy
            if os.path.isfile(repo_zip_file):
                os.remove(repo_zip_file)

            try:
                download_file(repo_zip_url, repo_zip_file)
            finally:
                AppSettings.logger.debug("  Downloading finished.")

            AppSettings.logger.debug(f"  Unzipping {repo_zip_file} …")
            try:
                # NOTE: This is unsafe if the zipfile comes from an untrusted source
                unzip(repo_zip_file, repo_dir)
            finally:
                AppSettings.logger.debug("  Unzipping finished.")
            break  # Get out of lopp
        except HTTPError as e:  # Could this also be a race condition within Gitea ???
            # We do less tries for this condition (with shorter waits also)
            AppSettings.logger.error(f"Try {try_number}: Unable to download repo from {repo_zip_url}: {e}")
            if try_number < MAX_TRIES - 1:
                AppSettings.logger.info(f"  Waiting a few seconds before retrying…")
                sleep(SECONDS_BETWEEN_TRIES - 1)  # Try again after a few seconds
                try_number += 1
            else:
                AppSettings.logger.error(f"Unable to download file from {repo_zip_url} after {try_number} tries")
                raise e
        except BadZipFile as e:  # I suspect a race condition within Gitea ???
            AppSettings.logger.error(
                f"Try {try_number}: Got bad zip file when downloading repo from {repo_zip_url}: {e}")
            if try_number < MAX_TRIES:
                AppSettings.logger.info(f"  Waiting a few seconds before retrying…")
                sleep(SECONDS_BETWEEN_TRIES)  # Try again after a few seconds
                try_number += 1
            else:
                raise BadZipFile(f"Unable to get a good zip file from {repo_zip_url} after {try_number} tries")

    # Remove the downloaded zip file (now unzipped)
    if not prefix:  # For dev- save this file longer
        if os.path.isfile(repo_zip_file):
            os.remove(repo_zip_file)


# end of download_and_unzip_repo function


def download_repos_files_into_temp_folder(base_temp_dir_name: str, commit_url: str, repo_name: str) -> str:
    """
    """
    temp_folderpath = tempfile.mkdtemp(dir=base_temp_dir_name, prefix=f'{repo_name}_')
    download_and_unzip_repo(base_temp_dir_name, commit_url, temp_folderpath)
    repo_folderpath = os.path.join(temp_folderpath, repo_name.lower())
    if os.path.isdir(repo_folderpath):
        print("Returning1", repo_folderpath)
        return repo_folderpath
    # else the folder that we were expecting from inside the zipped repo is not there
    # NOTE: This can happen if the repo has been renamed in DCS -- maybe a Gitea bug???
    AppSettings.logger.error(f"Unable to find expected '{repo_name.lower()}' folder inside {temp_folderpath}")
    possibleFolderpaths = []
    for something in os.listdir(temp_folderpath):
        somepath = os.path.join(temp_folderpath, something)
        isDir = os.path.isdir(somepath)
        isFile = os.path.isfile(somepath)
        assert isDir or isFile
        AppSettings.logger.warning(f"  Seems we have: '{something}' {'folder' if isDir else 'file'}")
        if isDir: possibleFolderpaths.append(somepath)
    if len(possibleFolderpaths) == 1:
        AppSettings.logger.warning(f"  Assuming that '{something}' folder (only one found) is the repo folder")
        print("Returning2", possibleFolderpaths[0])
        return possibleFolderpaths[0]
    # else:
    print("Returning3", temp_folderpath)
    return temp_folderpath


# end of download_repos_files_into_temp_folder function


def check_for_forthcoming_pushes_in_queue(submitted_json_payload: Dict[str, Any], our_queue) -> Tuple[
    bool, Optional[str]]:
    """
    TODO: skip if new release
    If there's already another push queued for the same repo,
        let's abort this one.

    Returns True if we can safely abort this build
                        and let a follow-up push trigger the repo rebuild.
    """
    len_our_queue = len(our_queue)
    if submitted_json_payload['DCS_event'] == 'push' \
            and len(submitted_json_payload['commits']) == 1 \
            and len_our_queue:  # Have other entries
        AppSettings.logger.info(
            f"Checking for duplicate pushes in {len_our_queue} other queued job entr{'y' if len_our_queue == 1 else 'ies'}…")
        my_url_bits = submitted_json_payload['commits'][0]['url'].split('/')
        for queued_job in our_queue.jobs:
            if queued_job.get_status() == 'queued':
                queued_job_args = queued_job.args  # tuple
                assert len(queued_job_args) == 1
                queued_job_parameter_dict = queued_job_args[0]
                if queued_job_parameter_dict['DCS_event'] == 'push' \
                        and len(queued_job_parameter_dict['commits']) == 1:
                    queued_url_bits = queued_job_parameter_dict['commits'][0]['url'].split('/')
                    if queued_url_bits[:6] == my_url_bits[:6]:  # commit number at end can be different
                        AppSettings.logger.info("Found duplicate job later in queue—aborting this one!")
                        job_descriptive_name = queued_job_parameter_dict['commits'][0]['url'].replace('https://', '')
                        AppSettings.logger.info(f"  Not processing build for {job_descriptive_name}")
                        return True, job_descriptive_name
    return False, None


# end of check_for_forthcoming_pushes_in_queue function


# user_projects_invoked_string = 'user-projects.invoked.unknown--unknown'
project_types_invoked_string = f'{job_handler_stats_prefix}.types.invoked.unknown'


def clone_repo(url: str, dest: str) -> bool:
    os.system(f'git clone --depth 1 -- {url} {dest}')
    return os.path.exists(dest) and len(os.listdir(dest)) > 0


def handle_catalog_release(repo_owner_username: str, repo_name: str, commit_id: str, repo_data_url: str):
    """
    Handles copying a release to the Door43-Catalog organization
    """
    temp_dir = os.path.join(tempfile.gettempdir(), 'dcs_releases')
    if not os.path.exists(temp_dir):
        try:
            os.mkdir(temp_dir)
        except OSError as e:
            print("Error: %s : %s" % (temp_dir, e.strerror))

    # download release
    release_path = download_repos_files_into_temp_folder(temp_dir, repo_data_url, repo_name)
    AppSettings.logger.info(f'Downloaded release to {release_path}')

    # create/clone repo
    repo_url = f'https://{AppSettings.gitea_user}:{AppSettings.gitea_password}@{AppSettings.gitea_domain}/Door43-Catalog/{repo_name}.git'
    repo_dir = tempfile.mkdtemp(dir=temp_dir, prefix=f'{repo_name}_')
    cloned = clone_repo(repo_url, repo_dir)
    if not cloned:
        AppSettings.logger.info(f'Creating new catalog repo {repo_name}')
        if not os.path.exists(repo_dir):
            os.mkdir(repo_dir)
        os.chdir(repo_dir)
        os.system('git init')
        os.system(f'git remote add origin {repo_url}')

    # copy release into repo
    # clear existing files
    os.system(f"find {repo_dir} -mindepth 1 -maxdepth 1 -not -name '.git' -delete")
    os.system(f'cp -R {os.path.join(release_path, "*")} {repo_dir}')
    os.chdir(repo_dir)
    os.system(f'git add .')
    os.system(f'git commit -m "Release \'{commit_id}\' from {repo_owner_username}/{repo_name}"')

    # push release to catalog
    AppSettings.logger.info(f'Pushing release to {repo_url}')
    os.system(f'git push origin master')

    # clean up files
    if os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except OSError as e:
            print("Error: %s : %s" % (temp_dir, e.strerror))


def get_release_info(queued_json_payload: Dict[str, Any]) -> Dict[str, Any] or None:
    """
    Extracts the release information from the webhook payload.
    """

    try:
        default_branch = queued_json_payload['repository']['default_branch']
    except KeyError:
        AppSettings.logger.critical("No default branch specified")
        default_branch = 'NoDefaultBranch'
    AppSettings.logger.debug(f"Got default_branch='{default_branch}'")

    # Gather other details from the commit that we will need for the job
    repo_owner_username = queued_json_payload['repository']['owner']['username']
    repo_name = queued_json_payload['repository']['name']

    commit_branch = commit_hash = None
    if queued_json_payload['DCS_event'] == 'release':
        # Note: payload doesn't include a commit hash
        try:
            tag_name = queued_json_payload['release']['tag_name']
        except (IndexError, AttributeError):
            AppSettings.logger.critical(f"Could not determine tag name from '{queued_json_payload['release']}'")
            tag_name = 'UnknownTagName'
        except KeyError:
            AppSettings.logger.critical("No tag name specified")
            tag_name = 'NoTagName'
        repo_data_url = queued_json_payload['release']['zipball_url']
        action_message = queued_json_payload['release']['name']

        if 'author' in queued_json_payload['release']:
            pusher_dict = queued_json_payload['release']['author']
        else:
            pusher_dict = queued_json_payload['sender']

        pusher_username = pusher_dict['username']
    else:
        AppSettings.logger.critical(f"Can't handle '{queued_json_payload['DCS_event']}' yet!")
        return None

    if commit_branch == default_branch:
        commit_type = 'defaultBranch'
        commit_id = commit_branch
    elif tag_name:
        commit_type = 'tag'
        commit_id = tag_name
    elif commit_branch not in (None, 'UnknownCommitBranch', 'NoCommitBranch'):
        commit_type = 'branch'
        commit_id = commit_branch
    else:
        commit_type = 'unknown'
        commit_id = None

    if commit_id:
        return {
            "commit_hash": commit_hash,
            "commit_id": commit_id,
            "commit_type": commit_type,
            "pusher_username": pusher_username,
            "repo_data_url": repo_data_url,
            "action_message": action_message,
            "repo_name": repo_name,
            "repo_owner_username": repo_owner_username
        }
    else:
        return None


def process_webhook_job(queued_json_payload: Dict[str, Any]) -> str:
    """
    Parameters:
        queued_json_payload is a dict

    It gathers details from the JSON payload.

    The given payload will be automatically appended to the 'failed' queue
        by rq if an exception is thrown in this module.
    """
    AppSettings.logger.debug(f"WEBHOOK {prefix + ' ' if prefix else ''}processing: {queued_json_payload}")

    #  Update repo/owner/pusher stats
    #   (all the following fields are expected from the Gitea webhook from push)
    try:
        stats_client.set(f'{webhook_stats_prefix}.repo_ids', queued_json_payload['repository']['id'])
    except (KeyError, AttributeError, IndexError, TypeError):
        stats_client.set(f'{webhook_stats_prefix}.repo_ids', 'No id')
    try:
        stats_client.set(f'{webhook_stats_prefix}.owner_ids', queued_json_payload['repository']['owner']['id'])
    except (KeyError, AttributeError, IndexError, TypeError):
        stats_client.set(f'{webhook_stats_prefix}.owner_ids', 'No id')
    try:
        stats_client.set(f'{webhook_stats_prefix}.pusher_ids', queued_json_payload['pusher']['id'])
    except (KeyError, AttributeError, IndexError, TypeError):
        stats_client.set(f'{webhook_stats_prefix}.pusher_ids', 'No id')

    # Get the commit_id, commit_url
    try:
        default_branch = queued_json_payload['repository']['default_branch']
    except KeyError:
        AppSettings.logger.critical("No default branch specified")
        default_branch = 'NoDefaultBranch'
    AppSettings.logger.debug(f"Got default_branch='{default_branch}'")

    release = get_release_info(queued_json_payload)
    # TRICKY: we are pushing releases to the Door43-Catalog, so we ignore events coming from there.
    # TODO: we may want to restrict to releases from unfoldingWord
    if release and release['repo_owner_username'] != 'Door43-Catalog':
        AppSettings.logger.debug(
            f"Got new '{release['commit_type']}' commit_id='{release['commit_id']}' (commit_hash={release['commit_hash']})")
        AppSettings.logger.debug(f"Got repo_data_url='{release['repo_data_url']}'")
        our_identifier = f"'{release['pusher_username']}' releasing '{release['repo_owner_username']}/{release['repo_name']}'"
        AppSettings.logger.info(f"Processing job for {our_identifier} for \"{release['action_message']}\"")

        # Seems that statsd 3.3.0 can only handle ASCII chars (not full Unicode)
        ascii_repo_owner_username_bytes = release['repo_owner_username'].encode('ascii',
                                                                                'replace')  # Replaces non-ASCII chars with '?'
        adjusted_repo_owner_username = ascii_repo_owner_username_bytes.decode('utf-8')  # Recode as a str
        stats_client.incr(f'{webhook_stats_prefix}.users.invoked.{adjusted_repo_owner_username}')

        handle_catalog_release(release['repo_owner_username'], release['repo_name'], release['commit_id'],
                               release['repo_data_url'])
        job_descriptive_name = f'{our_identifier}'
    else:
        # There was no valid event to process
        AppSettings.logger.critical(f"Nothing to process for '{queued_json_payload['DCS_event']}'!")
        repo_owner_username = queued_json_payload['repository']['owner']['username']
        repo_name = queued_json_payload['repository']['name']
        job_descriptive_name = f"'{repo_owner_username}/{repo_name}'"

    AppSettings.logger.info(f"{prefixed_our_name} process_webhook_job() for {job_descriptive_name} has finished.")
    return job_descriptive_name


# end of process_webhook_job function


def job(queued_json_payload: Dict[str, Any]) -> None:
    """
    This function is called by the rq package to process a job in the queue(s).
        (Don't rename this function.)

    The job is removed from the queue before the job is started,
        but if the job throws an exception or times out (timeout specified in enqueue process)
            then the job gets added to the 'failed' queue.
    """
    AppSettings.logger.debug(f"{OUR_NAME} received a job" + (" (in debug mode)" if debug_mode_flag else ""))
    start_time = time()
    stats_client.incr(f'{webhook_stats_prefix}.jobs.attempted')
    if 'echoed_from_production' in queued_json_payload and queued_json_payload['echoed_from_production']:
        AppSettings.logger.info("This job was ECHOED FROM PRODUCTION (for dev- chain testing)!")

    AppSettings.logger.debug(f"Clearing /tmp folder…")
    empty_folder('/tmp/', only_prefix='Door43_')  # Stops failed jobs from accumulating in /tmp

    current_job = get_current_job()

    our_queue = Queue(webhook_queue_name, connection=current_job.connection)
    len_our_queue = len(our_queue)  # Should normally sit at zero here

    abort_duplicate_flag, job_descriptive_name = check_for_forthcoming_pushes_in_queue(queued_json_payload, our_queue)
    if not abort_duplicate_flag:
        stats_client.gauge(f'"{door43_stats_prefix}.enqueue-job.webhook.queue.length.current', len_our_queue)
        AppSettings.logger.info(
            f"Updated stats for '{door43_stats_prefix}.enqueue-job.webhook.queue.length.current' to {len_our_queue}")

        try:
            job_descriptive_name = process_webhook_job(queued_json_payload)
        except Exception as e:
            # Catch most exceptions here so we can log them to CloudWatch
            AppSettings.logger.critical(
                f"{prefixed_our_name} webhook threw an exception while processing:\n{queued_json_payload}\ngetting exception:\n{e}: {traceback.format_exc()}")
            AppSettings.close_logger()  # Ensure queued logs are uploaded to AWS CloudWatch
            # Now attempt to log it to an additional, separate FAILED log
            import logging
            from boto3 import Session
            from watchtower import CloudWatchLogHandler
            logger2 = logging.getLogger(prefixed_our_name)
            test_mode_flag = os.getenv('TEST_MODE', '')
            travis_flag = os.getenv('TRAVIS_BRANCH', '')
            log_group_name = f"FAILED_{'' if test_mode_flag or travis_flag else prefix}tX" \
                             f"{'_DEBUG' if debug_mode_flag else ''}" \
                             f"{'_TEST' if test_mode_flag else ''}" \
                             f"{'_TravisCI' if travis_flag else ''}"
            aws_access_key_id = os.environ['AWS_ACCESS_KEY_ID']
            boto3_session = Session(aws_access_key_id=aws_access_key_id,
                                    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
                                    region_name='us-west-2')
            failure_watchtower_log_handler = CloudWatchLogHandler(boto3_session=boto3_session,
                                                                  use_queues=False,
                                                                  log_group=log_group_name,
                                                                  stream_name=prefixed_our_name)
            logger2.addHandler(failure_watchtower_log_handler)
            logger2.setLevel(logging.DEBUG)
            logger2.info(f"Logging to AWS CloudWatch group '{log_group_name}' using key '…{aws_access_key_id[-2:]}'.")
            logger2.critical(
                f"{prefixed_our_name} webhook threw an exception while processing:\n{queued_json_payload}\ngetting exception:\n{e}: {traceback.format_exc()}")
            failure_watchtower_log_handler.close()
            # NOTE: following line removed as stats recording used too much disk space
            # stats_client.gauge(user_projects_invoked_string, 1) # Mark as 'failed'
            stats_client.gauge(project_types_invoked_string, 1)  # Mark as 'failed'
            raise e  # We raise the exception again so it goes into the failed queue

    elapsed_milliseconds = round((time() - start_time) * 1000)
    stats_client.timing(f'{webhook_stats_prefix}.job.duration', elapsed_milliseconds)
    if elapsed_milliseconds < 2000:
        AppSettings.logger.info(
            f"{prefixed_our_name} webhook job handling for {job_descriptive_name} completed in {elapsed_milliseconds:,} milliseconds.")
    else:
        AppSettings.logger.info(
            f"{prefixed_our_name} webhook job handling for {job_descriptive_name} completed in {round(time() - start_time)} seconds.")

    stats_client.incr(f'{webhook_stats_prefix}.jobs.completed')
    AppSettings.close_logger()  # Ensure queued logs are uploaded to AWS CloudWatch
# end of job function

# end of webhook.py for door43_enqueue_job
