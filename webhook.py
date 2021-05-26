# DOOR43 WEBHOOK
#
# NOTE: This module name and function name are defined by the rq package and our own door43-enqueue-job package
# This code adapted by RJH June 2018 from tx-manager/client_webhook/ClientWebhook/process_webhook

# NOTE: rq_settings.py is executed at program start-up, reads some environment variables, and sets queue name, etc.
#       job() function (at bottom here) is executed by rq package when there is an available entry in the named queue.

# Python imports
from typing import Dict, List, Tuple, Any, Optional, Union
import os
import tempfile
import json
import hashlib
import shutil
from datetime import datetime, timedelta
from time import time, sleep
import traceback
from zipfile import BadZipFile
from urllib.error import HTTPError

# Library (PyPI) imports
import requests
from rq import get_current_job, Queue
from redis import exceptions as redis_exceptions
from statsd import StatsClient # Graphite front-end

# Local imports
from rq_settings import prefix, debug_mode_flag, tx_post_url, REDIS_JOB_LIST, webhook_queue_name # gogs_user_token
from general_tools.file_utils import unzip, add_contents_to_zip, write_file, remove_tree, empty_folder
from general_tools.url_utils import download_file
from resource_container.ResourceContainer import RC
from preprocessors.preprocessors import do_preprocess
from models.manifest import TxManifest
from app_settings.app_settings import AppSettings



OUR_NAME = 'Door43_job_handler'
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
            'obs-sg': 'Generic_Markdown', # See if this works for OBS Study Guide

            'bible': 'Bible', 'reg': 'Bible',
                'ulb': 'Bible', 'udb': 'Bible', # These sometimes don't have the correct subject in the manifest

            'ta': 'Translation_Academy',
            'tn': 'Translation_Notes',
            'tq': 'Translation_Questions',
            'tw': 'Translation_Words',

            'ugl': 'Greek_Lexicon', # Subject for en_ugl is 'Greek English Lexicon' but we want to stay more generic
            'uhal': 'Hebrew-Aramaic_Lexicon',

            # TODO: Have I got these next two correct???
            #'help':'Translation_Academy',
            #'man':'Translation_Academy',
            }



AppSettings(prefix=prefix)
if prefix not in ('', 'dev-'):
    AppSettings.logger.critical(f"Unexpected prefix: '{prefix}' — expected '' or 'dev-'")
door43_stats_prefix = f"door43.{'dev' if prefix else 'prod'}"
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


def clear_commit_directory_in_cdn(s3_commit_key:str) -> None:
    """
    Clear out the commit directory in the CDN bucket for this project revision.
    """
    AppSettings.logger.debug(f"Clearing objects from {prefix}CDN commit directory '{s3_commit_key}' …")
    # Original code
    # for obj in AppSettings.cdn_s3_handler().get_objects(prefix=s3_commit_key):
    #     # AppSettings.logger.debug(f"Removing s3 cdn file: {obj.key} …")
    #     AppSettings.cdn_s3_handler().delete_file(obj.key)
    # New code (adapted from https://stackoverflow.com/questions/11426560/amazon-s3-boto-how-to-delete-folder)
    # May also delete the folder itself (doesn't matter)
    AppSettings.cdn_s3_handler().bucket.objects.filter(Prefix=s3_commit_key).delete()
# end of clear_commit_directory_in_cdn function


def get_unique_job_id() -> str:
    """
    Returns a 64 hex-character (lowercase) string.
        e.g., 'e2cddf55dc410ec584d647157388e96f22bf7b60d900e79afd1c56e27aa0e417'

    :return string:
    """
    job_id = hashlib.sha256(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f').encode('utf-8')).hexdigest()
    # We no longer use TxJob so can't check it for duplicates
    #   (but could theoretically check the preconvert bucket since job_id.zip is saved there).
    #while TxJob.get(job_id):
        #job_id = hashlib.sha256(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f').encode('utf-8')).hexdigest()
    return job_id
# end of get_unique_job_id()


def upload_preconvert_zip_file(job_id:str, zip_filepath:str) -> str:
    """
    """
    zip_file_key = f'preconvert/{job_id}.zip'
    AppSettings.logger.debug(f"Uploading {zip_filepath} to {AppSettings.pre_convert_bucket_name}/{zip_file_key} …")
    try:
        AppSettings.pre_convert_s3_handler().upload_file(zip_filepath, zip_file_key, cache_time=0)
    except Exception as e:
        AppSettings.logger.error(f"Failed to upload zipped repo up to server—got exception: {e}")
    finally:
        AppSettings.logger.debug("Upload finished.")
    return zip_file_key
# end of upload_preconvert_zip_file function


def download_and_unzip_repo(base_temp_dir_name:str, commit_url:str, repo_dir:str) -> None:
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
            break # Get out of lopp
        except HTTPError as e: # Could this also be a race condition within Gitea ???
            # We do less tries for this condition (with shorter waits also)
            AppSettings.logger.error(f"Try {try_number}: Unable to download repo from {repo_zip_url}: {e}")
            if try_number < MAX_TRIES-1:
                AppSettings.logger.info(f"  Waiting a few seconds before retrying…")
                sleep(SECONDS_BETWEEN_TRIES-1) # Try again after a few seconds
                try_number += 1
            else:
                AppSettings.logger.error(f"Unable to download file from {repo_zip_url} after {try_number} tries")
                raise e
        except BadZipFile as e: # I suspect a race condition within Gitea ???
            AppSettings.logger.error(f"Try {try_number}: Got bad zip file when downloading repo from {repo_zip_url}: {e}")
            if try_number < MAX_TRIES:
                AppSettings.logger.info(f"  Waiting a few seconds before retrying…")
                sleep(SECONDS_BETWEEN_TRIES) # Try again after a few seconds
                try_number += 1
            else:
                raise BadZipFile(f"Unable to get a good zip file from {repo_zip_url} after {try_number} tries")

    # Remove the downloaded zip file (now unzipped)
    if not prefix: # For dev- save this file longer
        if os.path.isfile(repo_zip_file):
            os.remove(repo_zip_file)
# end of download_and_unzip_repo function


def download_repos_files_into_temp_folder(base_temp_dir_name:str, commit_url:str, repo_name:str) -> str:
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
        if isDir: possibleFolderpaths.append( somepath )
    if len(possibleFolderpaths) == 1:
        AppSettings.logger.warning(f"  Assuming that '{something}' folder (only one found) is the repo folder")
        print("Returning2", possibleFolderpaths[0])
        return possibleFolderpaths[0]
    # else:
    print("Returning3", temp_folderpath)
    return temp_folderpath
# end of download_repos_files_into_temp_folder function


def get_tX_subject(gts_repo_name:str, gts_rc) -> str:
    """
    Given a resource container, try to determine the repo subject
        even if the manifest has no subject field.

    https://api.door43.org/v3/subjects specifies 14 subjects (as of Mar 2020)

    Can return None if we can't determine one.
    """
    # AppSettings.logger.debug(f"get_tX_subject('{gts_repo_name}', rc)…")
    # AppSettings.logger.debug(f"gts_rc.resource.identifier={gts_rc.resource.identifier}")
    # AppSettings.logger.debug(f"gts_rc.resource.file_ext={gts_rc.resource.file_ext}")
    # AppSettings.logger.debug(f"gts_rc.resource.type={gts_rc.resource.type}")
    # AppSettings.logger.debug(f"gts_rc.resource.subject={gts_rc.resource.subject}")
    # AppSettings.logger.debug(f"gts_rc.resource.format={gts_rc.resource.format}")

    repo_subject = None

    adjusted_subject = gts_rc.resource.subject
    if adjusted_subject:
        adjusted_subject = adjusted_subject.replace(' ', '_') # NOTE: RC returns 'title' if 'subject' is missing
        if adjusted_subject in KNOWN_RESOURCE_SUBJECTS:
            AppSettings.logger.info(f"Using (adjusted) subject to set repo_subject='{adjusted_subject}'")
            repo_subject = adjusted_subject
        elif 'bible' in adjusted_subject.lower() and gts_rc.resource.identifier not in RESOURCE_SUBJECT_MAP:
            repo_subject = 'Bible'
            AppSettings.logger.info(f"Using 'bible' in (adjusted) subject=={adjusted_subject} to set repo_subject to '{repo_subject}'")
        else:
            AppSettings.logger.warning(f"Didn't use (adjusted) subject='{adjusted_subject}' to set repo_subject")
    else:
        AppSettings.logger.warning("No subject or title in RC manifest")

    if not repo_subject:
        rc_resource_format = gts_rc.resource.format
        if rc_resource_format:
            if rc_resource_format in ('usfm','usfm3','text/usfm','text/usfm3'):
                repo_subject = 'Bible'
                AppSettings.logger.info(f"Using rc.resource.format='{rc_resource_format}' to set repo_subject='{repo_subject}'")
            else:
                AppSettings.logger.debug(f"Didn't use rc.resource.format='{rc_resource_format}' to set repo_subject")
        else:
            AppSettings.logger.warning("No resource.format in RC manifest")

    if not repo_subject:
        rc_resource_identifier = gts_rc.resource.identifier
        if rc_resource_identifier:
            if rc_resource_identifier in RESOURCE_SUBJECT_MAP:
                repo_subject = RESOURCE_SUBJECT_MAP[rc_resource_identifier]
                AppSettings.logger.info(f"Using rc.resource.identifier='{rc_resource_identifier}' to set repo_subject='{repo_subject}'")
            else:
                AppSettings.logger.debug(f"Didn't use rc.resource.identifier='{rc_resource_identifier}' to set repo_subject")
        else:
            AppSettings.logger.warning("No resource.identifier in RC manifest")

    if not repo_subject and rc_resource_identifier:
        for resource_subject_string in RESOURCE_SUBJECT_MAP:
            if rc_resource_identifier.endswith('_'+resource_subject_string) \
            or rc_resource_identifier.endswith('-'+resource_subject_string):
                repo_subject = RESOURCE_SUBJECT_MAP[resource_subject_string]
                AppSettings.logger.info(f"Using '{resource_subject_string}' at end of rc.resource.identifier='{rc_resource_identifier}' to set repo_subject='{repo_subject}'")
                break
        else: # if didn't match/break above
            AppSettings.logger.debug(f"Didn't use end of rc.resource.identifier='{rc_resource_identifier}' to set repo_subject")

    if not repo_subject:
        rc_resource_type = gts_rc.resource.type
        if rc_resource_type:
            if rc_resource_type in RESOURCE_SUBJECT_MAP: # e.g., help, man
                repo_subject = RESOURCE_SUBJECT_MAP[rc_resource_type]
                AppSettings.logger.info(f"Using rc.resource.type='{rc_resource_type}' to set repo_subject='{repo_subject}'")
        else:
            AppSettings.logger.warning("No resource.type in RC manifest")

    if repo_subject=='Translation_Notes' and gts_rc.resource.format=='tsv':
        repo_subject = 'TSV_Translation_Notes'
        AppSettings.logger.info(f"Using rc.resource.format='{gts_rc.resource.format}' to change repo_subject from 'Translation_Notes' to '{repo_subject}'")

    if not repo_subject and ('-obs' in gts_repo_name or '_obs' in gts_repo_name):
        repo_subject = 'Open_Bible_Stories'
        AppSettings.logger.info(f"Trying setting repo_subject='{repo_subject}'")

    if not repo_subject:
        repo_subject = 'Generic_Markdown'
        AppSettings.logger.info(f"Trying setting repo_subject='{repo_subject}'")

    return repo_subject
# end of get_tX_subject function


def remember_job(rj_job_dict:Dict[str,Any], rj_redis_connection) -> None:
    """
    Save this outstanding job in a REDIS dict
        so that we can match it when we get a callback

    The REDIS dict contains a string representation of a json dict
        whose entries are job ids mapped to the full job info dict.
    """
    # AppSettings.logger.debug(f"remember_job( {rj_job_dict['job_id']} )")

    try:
        outstanding_jobs_dict_bytes = rj_redis_connection.get(REDIS_JOB_LIST) # Gets None or bytes!!!
    # This can happen ONCE if the format has changed by code updates—shouldn't normally happen
    # NOTE: Actually this code
    except redis_exceptions.ResponseError as e:
        AppSettings.logger.critical(f"Unable to load former outstanding_jobs_dict from Redis: {e}")
        AppSettings.logger.critical(f"Losing former outstanding_jobs_dict from Redis…")
        outstanding_jobs_dict_bytes = None # Error should self-correct
        # NOTE: Could potentially cause one forthcoming callback job to fail (coz we just deleted its job data)
    if outstanding_jobs_dict_bytes is None:
        AppSettings.logger.info("Created new outstanding_jobs_dict")
        outstanding_jobs_dict:Dict[str,object] = {}
    else:
        assert isinstance(outstanding_jobs_dict_bytes,bytes)
        outstanding_jobs_dict_json_string = outstanding_jobs_dict_bytes.decode() # bytes -> str
        assert isinstance(outstanding_jobs_dict_json_string,str)
        outstanding_jobs_dict = json.loads(outstanding_jobs_dict_json_string)
        assert isinstance(outstanding_jobs_dict,dict)
        # AppSettings.logger.debug(f"Got outstanding_jobs_dict: "
        #                            f" ({len(outstanding_jobs_dict)}) {outstanding_jobs_dict.keys()}")

        AppSettings.logger.debug(f"Already had {len(outstanding_jobs_dict)}"
                                   f" outstanding job(s) in '{REDIS_JOB_LIST}' redis store.")
        # Remove any outstanding jobs more than two weeks old
        for outstanding_job_id, outstanding_job_dict in outstanding_jobs_dict.copy().items():
            assert isinstance(outstanding_job_id,str)
            assert isinstance(outstanding_job_dict,dict)
            outstanding_duration = datetime.utcnow() \
                                - datetime.strptime(outstanding_job_dict['created_at'], '%Y-%m-%dT%H:%M:%SZ')
            if outstanding_duration >= timedelta(weeks=2):
                AppSettings.logger.info(f"Deleting expired saved job from {outstanding_job_dict['created_at']}")
                del outstanding_jobs_dict[outstanding_job_id] # Delete from our local copy

    # This new job shouldn't already be in the outstanding jobs dict
    assert rj_job_dict['job_id'] not in outstanding_jobs_dict
    outstanding_jobs_dict[rj_job_dict['job_id']] = rj_job_dict
    AppSettings.logger.info(f"Now have {len(outstanding_jobs_dict)}"
                               f" outstanding job(s) in '{REDIS_JOB_LIST}' redis store.")

    # Write the updated job list to Redis
    assert outstanding_jobs_dict # Should always contain at least one entry (the current new one)
    outstanding_jobs_json_string = json.dumps(outstanding_jobs_dict)
    rj_redis_connection.set(REDIS_JOB_LIST, outstanding_jobs_json_string)
# end of remember_job function


# def upload_to_BDB(job_name:str, BDB_zip_filepath:str) -> None:
#     """
#     Upload a Bible job (usfm) to the Bible Drop Box.

#     Included here temporarily as a way to compare handling of USFM files
#         and for a comparison of warnings/errors that are detected/displayed.
#         (Would have to be manually compared—nothing is done here with the BDB results.)
#     """
#     AppSettings.logger.debug(f"upload_to_BDB({job_name, BDB_zip_filepath})…")
#     BDB_url = 'http://Freely-Given.org/Software/BibleDropBox/SubmitAction.phtml'
#     files_data = {
#         'nameLine': (None, f'DCS_Auto_{prefixed_our_name}'),
#         'emailLine': (None, 'noone@nowhere.org'),
#         'projectLine': (None, job_name),
#             'doChecks': (None, 'Yes'),
#                 'NTfinished': (None, 'No'),
#                 'OTfinished': (None, 'No'),
#                 'DCfinished': (None, 'No'),
#                 'ALLfinished': (None, 'No'),
#             'doExports': (None, 'No'),
#                 'photoBible': (None, 'No'),
#                 'odfs': (None, 'No'),
#                 'pdfs': (None, 'No'),
#         'goalLine': (None, 'test'),
#             'permission': (None, 'Yes'),
#         'uploadedZipFile': (os.path.basename(BDB_zip_filepath), open(BDB_zip_filepath, 'rb'), 'application/zip'),
#         'uploadedMetadataFile': ('', b''),
#         'submit': (None, 'Submit'),
#         }
#     AppSettings.logger.debug(f"Posting data to {BDB_url} …")
#     try:
#         response = requests.post(BDB_url, files=files_data)
#     except requests.exceptions.ConnectionError as e:
#         AppSettings.logger.critical(f"BDB connection error: {e}")
#         response = None

#     if response:
#         AppSettings.logger.info(f"BDB response.status_code = {response.status_code}, response.reason = {response.reason}")
#         AppSettings.logger.debug(f"BDB response.headers = {response.headers}")
#         # AppSettings.logger.debug(f"BDB response.text = {response.text}")
#         if response.status_code == 200:
#             if "Your project has been submitted" in response.text:
#                 ix = response.text.find('eventually be available <a href="')
#                 if ix != -1:
#                     ixStart = ix + 33
#                     ixEnd = response.text.find('">here</a>')
#                     job_url = response.text[ixStart:ixEnd]
#                     AppSettings.logger.info(f"BDB results will be available at http://Freely-Given.org/Software/BibleDropBox/{job_url}")
#             else:
#                 AppSettings.logger.error(f"BDB didn't accept job: {response.text}")
#         else:
#             AppSettings.logger.error(f"Failed to submit job to BDB:"
#                                            f" {response.status_code}={response.reason}")
#     else: # no response
#         # error_msg = "Submission of job to BDB got no response"
#         AppSettings.logger.error("Submission of job to BDB got no response")
#         #raise Exception(error_msg) # Is this the best thing to do here?
# # end of upload_to_BDB


def clear_commit_directory_from_bucket(s3_bucket_handler, s3_commit_key:str) -> None:
    """
    Clear out and remove the commit directory from the requested bucket for this project revision.
    """
    AppSettings.logger.debug(f"Clearing objects from commit directory '{s3_commit_key}' in {s3_bucket_handler.bucket_name} bucket…")
    s3_bucket_handler.bucket.objects.filter(Prefix=s3_commit_key).delete()
# end of clear_commit_directory_from_bucket function


def handle_branch_delete(base_temp_dir_name:str, repo_owner_username:str, repo_name:str,
                            deleted_branch_name:str) -> None:
    """
    Deletes the branch name from project.json
        (project.json is read by the Javascript in door43.org/js/project-page-functions.js)
    """
    print(f"handle_branch_delete({base_temp_dir_name}, {repo_owner_username}, {repo_name}, {deleted_branch_name})")

    project_folder_key = f'u/{repo_owner_username}/{repo_name}/'
    project_json_key = f'{project_folder_key}project.json'
    project_json = AppSettings.cdn_s3_handler().get_json(project_json_key)

    AppSettings.logger.info("Rebuilding commits list for project.json…")
    if 'commits' not in project_json:
        project_json['commits'] = []
    cleaned_commits = project_json['commits'].copy()
    print(f"Got {len(project_json['commits'])} commits ({len(cleaned_commits)})")
    for ix, c in enumerate(project_json['commits']):
        AppSettings.logger.debug(f"  Looking at {ix}/ '{c['id']}'. Is wanted branch={c['id'] == deleted_branch_name}…")
        if c['id'] == deleted_branch_name: # the old entry for this branch
            AppSettings.logger.info(f"    Removing deleted {repo_owner_username}/{repo_name} '{deleted_branch_name}' branch…")
            cleaned_commits.pop(ix) # Delete this one from the list
            try:
                # Delete the commit hash folders from both CDN and D43 buckets
                commit_key = f"{project_folder_key}{deleted_branch_name}"
                AppSettings.logger.info(f"      Removing {prefix}CDN '{c['type']}' '{deleted_branch_name}' folder! …")
                clear_commit_directory_from_bucket(AppSettings.cdn_s3_handler(), commit_key)
                AppSettings.logger.info(f"      Removing {prefix}D43 '{c['type']}' '{deleted_branch_name}' folder! …")
                clear_commit_directory_from_bucket(AppSettings.door43_s3_handler(), commit_key)
                # Delete the pre-convert .zip file (available on Download button) from its bucket
                if c['job_id']:
                    zipFile_key = f"preconvert/{c['job_id']}.zip"
                    AppSettings.logger.info(f"      Removing {prefix}PreConvert '{c['type']}' '{zipFile_key}' file! …")
                    clear_commit_directory_from_bucket(AppSettings.pre_convert_s3_handler(), zipFile_key)
                else: # don't know the job_id (or the zip file was already deleted)
                    AppSettings.logger.warning("   No job_id so pre-convert zip file not deleted.")
                if cleaned_commits:
                    # Setup redirects (so users don't get 404 errors from old saved links)
                    old_repo_key = f"{project_folder_key}{deleted_branch_name}"
                    latest_repo_key = f"{project_folder_key}{cleaned_commits[-1]['id']}"
                    if latest_repo_key == old_repo_key:
                        AppSettings.logger.error(f"Can't redirect {repo_owner_username}/{repo_name} '{old_repo_key}' to itself!")
                        print("What's gone wrong here?")
                        print("commits", len(project_json['commits']), project_json['commits'])
                        print("cleaned_commits", len(cleaned_commits), cleaned_commits)
                    else: # Redirect deleted branch to latest branch
                        AppSettings.logger.info(f"     Redirecting {old_repo_key} and {old_repo_key}/index.html to {latest_repo_key} …")
                        latest_repo_key = f"/{latest_repo_key}" # Must start with /
                        AppSettings.door43_s3_handler().redirect(key=old_repo_key, location=latest_repo_key)
                        AppSettings.door43_s3_handler().redirect(key=f'{old_repo_key}/index.html', location=latest_repo_key)
                else:
                    AppSettings.logger.warning(f"Unable to redirect from '{deleted_branch_name}' — no remaining {prefix}builds for {repo_owner_username}/{repo_name}!")
            except Exception as e:
                AppSettings.logger.critical(f"  Removing deleted branch files threw an exception: {e}")
        else:
            AppSettings.logger.debug("    Keeping this one.")

    print(f"Now got {len(project_json['commits'])} commits ({len(cleaned_commits)})")
    if len(cleaned_commits) < len(project_json['commits']): # Then we removed some
        AppSettings.logger.info(f"  Saving dated copy of old project.json (with {project_json['commits']} commit entries)…")
        # Save a dated (coz this could happen more than once) backup of the project.json file
        save_project_filename = f"project.save.{datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')}.json"
        save_project_filepath = os.path.join(base_temp_dir_name, save_project_filename)
        write_file(save_project_filepath, project_json)
        save_project_json_key = f'{project_folder_key}{save_project_filename}'
        AppSettings.cdn_s3_handler().upload_file(save_project_filepath, save_project_json_key, cache_time=0)
        AppSettings.door43_s3_handler().upload_file(save_project_filepath, save_project_json_key, cache_time=0)

        # Now save the updated project.json file
        project_json['commits'] = cleaned_commits
        AppSettings.logger.info(f"  Saving updated project.json (with {project_json['commits']} commit entries)…")
        project_filepath = os.path.join(base_temp_dir_name, 'project.json')
        write_file(project_filepath, project_json)
        AppSettings.cdn_s3_handler().upload_file(project_filepath, project_json_key, cache_time=0)
        AppSettings.door43_s3_handler().upload_file(project_filepath, project_json_key, cache_time=0)
    else:
        AppSettings.logger.info(f"Didn't find any '{deleted_branch_name}' branch files to delete.")
# end of handle_branch_delete function


def check_for_forthcoming_pushes_in_queue(submitted_json_payload:Dict[str,Any], our_queue) -> Tuple[bool,Optional[str]]:
    """
    If there's already another push queued for the same repo,
        let's abort this one.

    Returns True if we can safely abort this build
                        and let a follow-up push trigger the repo rebuild.
    """
    len_our_queue = len(our_queue)
    if submitted_json_payload['DCS_event'] == 'push' \
    and len(submitted_json_payload['commits']) == 1 \
    and len_our_queue: # Have other entries
        AppSettings.logger.info(f"Checking for duplicate pushes in {len_our_queue} other queued job entr{'y' if len_our_queue==1 else 'ies'}…")
        my_url_bits = submitted_json_payload['commits'][0]['url'].split('/')
        for queued_job in our_queue.jobs:
            # print(f"{j}/ {queued_job!r}")
            # print(f"    status = '{queued_job.get_status()}'")
            # # print(f"Args {type(queued_job.args)} ({len(queued_job.args)}) = {queued_job.args}") # tuple containing one dict
            # # print(f"KWArgs = {queued_job.kwargs}") # empty dict
            if queued_job.get_status() == 'queued':
                queued_job_args = queued_job.args # tuple
                assert len(queued_job_args) == 1
                queued_job_parameter_dict = queued_job_args[0]
                if queued_job_parameter_dict['DCS_event'] == 'push' \
                and len(queued_job_parameter_dict['commits']) == 1:
                    queued_url_bits = queued_job_parameter_dict['commits'][0]['url'].split('/')
                    if queued_url_bits[:6] == my_url_bits[:6]: # commit number at end can be different
                        AppSettings.logger.info("Found duplicate job later in queue—aborting this one!")
                        job_descriptive_name = queued_job_parameter_dict['commits'][0]['url'].replace('https://','')
                        AppSettings.logger.info(f"  Not processing build for {job_descriptive_name}")
                        return True, job_descriptive_name
    return False, None
# end of check_for_forthcoming_pushes_in_queue function


# user_projects_invoked_string = 'user-projects.invoked.unknown--unknown'
project_types_invoked_string = f'{job_handler_stats_prefix}.types.invoked.unknown'
def handle_page_build(base_temp_dir_name:str, submitted_json_payload:Dict[str,Any], redis_connection,
                        commit_type:str, commit_id:str, commit_hash:Optional[str],
                        repo_data_url:str, repo_owner_username:str, repo_name:str,
                        source_url_base:str, our_identifier:str,
                        our_queue) -> str:
    """
    It downloads a zip file from the DCS repo to the temp folder and unzips the files,
        and then creates a ResourceContainer (RC) object.

    It creates a manifest_data dictionary,
        gets a TxManifest from the DB and updates it with the above,
        or creates a new one if none existed.

    It then gets and runs a preprocessor on the files in the temp folder.
        A preprocessor has a ResourceContainer (RC) and source and output folders.
        It copies the file(s) from the RC in the source folder, over to the output folder,
            assembling chunks/chapters if necessary.

    The preprocessed files are zipped up in the temp folder
        and then uploaded to the pre-convert bucket in S3.

    A job dict is now setup and remembered in REDIS
        so that we can match it when we get a future callback.

    An S3 CDN folder is now named and emptied
        and a build log dictionary is created and uploaded to it.

    The project.json (in the folder above the CDN one) is also updated, e.g., with new commits.

    The job is now passed to the tX system by means of a
        POST to the tX webhook (which should hopefully respond with a callback).

    This code is "successful" once the job is submitted—
        it has no way to determine if it actually gets completed
        other than if a callback is made.
    """
    global project_types_invoked_string

    try: # Download and unzip the repo files
        repo_dir = download_repos_files_into_temp_folder(base_temp_dir_name, repo_data_url, repo_name)
    except HTTPError as e:
        if 'HTTP Error 404: Not Found' in str(e):
            raise Exception(f"Unable to find any source file for {repo_owner_username}/{repo_name} for {repo_data_url} at {repo_data_url if repo_data_url.endswith('.zip') else (repo_data_url.replace('commit','archive')+'.zip')}")
        else:
            raise e # Can't download/unzip repo files

    # Get the resource container
    # AppSettings.logger.debug(f'Getting Resource Container…')
    rc = RC(repo_dir, repo_name)
    job_descriptive_name = f'{our_identifier} {rc.resource.type}({rc.resource.format}, {rc.resource.file_ext})'


    # Use the RC to set the resource_subject and input_format parameters for tX
    resource_subject = get_tX_subject(repo_name, rc) # use the subject to set the resource type more intelligently
    project_types_invoked_string = f'{job_handler_stats_prefix}.types.invoked.{resource_subject}'
    input_format = rc.resource.file_ext
    if resource_subject in ('Bible', 'Aligned_Bible', 'Greek_New_Testament', 'Hebrew_Old_Testament',) \
    and input_format not in ('usfm','usfm3',):
        # This can happen for usfm in .txt files (ts-desktop exports)
        use_logger = AppSettings.logger.warning if input_format=='txt' else AppSettings.logger.critical
        use_logger(f"Changing input_format from '{input_format}' to 'usfm' for  resource_subject={resource_subject}")
        input_format = 'usfm'
    AppSettings.logger.info(f"Got resource_subject='{resource_subject}', input_format='{input_format}'")
    if resource_subject not in KNOWN_RESOURCE_SUBJECTS:
        AppSettings.logger.critical(f"Got unexpected resource_subject={resource_subject} with input_format={input_format}")
    if not resource_subject or not input_format:
        # Might as well fail here if they're not set properly
        if prefix and debug_mode_flag:
            AppSettings.logger.debug(f"Temp folder '{base_temp_dir_name}' has been left on disk for debugging!")
        else:
            remove_tree(base_temp_dir_name)  # cleanup
        raise Exception(f"Unable to find a type or format for {repo_owner_username}/{repo_name}: id={rc.resource.identifier!r} subject={rc.resource.subject!r}, RC type={rc.resource.type!r} format={input_format!r}")


    # Save manifest to manifest table
    # AppSettings.logger.debug(f'Creating manifest dictionary…')
    # AppSettings.logger.debug(f"Getting RC as_dict = {rc.as_dict()}")
    manifest_data = {
        'repo_name': repo_name,
        'user_name': repo_owner_username,
        'lang_code': rc.resource.language.identifier,
        'resource_id': rc.resource.identifier if rc.resource.identifier else 'UnknownID',
        'resource_type': resource_subject, # This used to be rc.resource.type
        'title': rc.resource.title if rc.resource.title else 'UnknownTitle',
        'manifest': json.dumps(rc.as_dict()),
        'last_updated': datetime.utcnow()
    }
    # First see if manifest already exists in DB (can be slowish) and update it if it is
    AppSettings.logger.debug(f"Getting manifest from DB for '{repo_name}' with user '{repo_owner_username}' …")
    tx_manifest = TxManifest.get(repo_name=repo_name, user_name=repo_owner_username)
    if tx_manifest:
        for key, value in manifest_data.items():
            setattr(tx_manifest, key, value)
        AppSettings.logger.debug(f"Updating manifest in manifest table: {manifest_data}")
        tx_manifest.update()
    else:
        tx_manifest = TxManifest(**manifest_data)
        AppSettings.logger.debug(f"Inserting manifest into manifest table: {tx_manifest}")
        tx_manifest.insert()


    # Preprocess the files
    AppSettings.logger.debug("Preprocessing files…")
    preprocess_dir = tempfile.mkdtemp(dir=base_temp_dir_name, prefix='preprocess_')
    num_preprocessor_files_written, preprocessor_warning_list = do_preprocess(resource_subject, repo_owner_username, repo_data_url, rc, repo_dir, preprocess_dir)

    # Save the warnings for the user—put any RC messages in front
    if rc.error_messages or preprocessor_warning_list:
        AppSettings.logger.debug(f"Prepending {len(rc.error_messages):,} RC warnings to {len(preprocessor_warning_list):,} preprocessor warnings")
    preprocessor_warning_list = list(rc.error_messages) + preprocessor_warning_list
    if preprocessor_warning_list:
        if ' warnings reduced from ' not in preprocessor_warning_list[-1]: # Don't overwhelm with extra messages
            preprocessor_warning_list.append(f"{len(preprocessor_warning_list):,} total resource container and preprocessor warnings")
        pwlist_len = len(preprocessor_warning_list)
        adjusted_preprocessor_warning_list = preprocessor_warning_list if pwlist_len < 20 \
                            else f'{preprocessor_warning_list[:10]} …… {preprocessor_warning_list[-10:]}'
        AppSettings.logger.debug(f"Preprocessor warning list is ({pwlist_len:,}) {adjusted_preprocessor_warning_list}")

    # Copy the ReadMe file if it seems that this repo is just minimal
    if num_preprocessor_files_written < 3:
        if os.path.isfile(os.path.join(repo_dir, 'README.md')):
            AppSettings.logger.debug("Try copying README.md…")
            shutil.copy(os.path.join(repo_dir, 'README.md'),preprocess_dir)
            num_preprocessor_files_written += 1

    # Try creating a file if there's nothing else to at least cause the page to build
    #  (This gives a more helpful error message than the standard DCS "Conversion Successful" one)
    if not num_preprocessor_files_written:
        with open(os.path.join(preprocess_dir,'NothingFound.md'), 'wt') as f:
            f.write("# NO FILES FOUND\nSorry, we couldn't find any markdown files to convert (not even README.md). Please check your manifest file.")
            num_preprocessor_files_written += 1


    # Seems we should always process, even if no files
    #   so that at least any errors/warnings get displayed


    abort_duplicate_flag, _unwanted_job_descriptive_name = check_for_forthcoming_pushes_in_queue(submitted_json_payload, our_queue)
    if not abort_duplicate_flag:
        # Zip up the massaged files
        AppSettings.logger.info(f"Zipping {num_preprocessor_files_written:,} preprocessed files…")
        preprocessed_zip_file = tempfile.NamedTemporaryFile(dir=base_temp_dir_name, prefix='preprocessed_', suffix='.zip', delete=False)
        AppSettings.logger.debug(f'Zipping files from {preprocess_dir} to {preprocessed_zip_file.name} …')
        add_contents_to_zip(preprocessed_zip_file.name, preprocess_dir)
        AppSettings.logger.debug("Zipping finished.")

        # Upload zipped file to the S3 pre-convert bucket
        AppSettings.logger.info("Uploading zip file to S3 pre-convert bucket…")
        our_job_id = get_unique_job_id()
        file_key = upload_preconvert_zip_file(job_id=our_job_id, zip_filepath=preprocessed_zip_file.name)


        # We no longer use txJob class but just create our own Python dict
        #   This gets saved in Redis so it can be recalled by the callback function
        #       (only a very small subset gets posted to the tX-enqueue-job)
        AppSettings.logger.debug("Webhook.handle_page_build setting up job dict…")
        pj_job_dict:Dict[str,Any] = {}
        pj_job_dict['job_id'] = our_job_id
        pj_job_dict['identifier'] = our_identifier # So we can recognise this job inside tX Job Handler
        pj_job_dict['repo_owner_username'] = repo_owner_username
        pj_job_dict['repo_name'] = repo_name
        pj_job_dict['commit_type'] = commit_type
        pj_job_dict['commit_id'] = commit_id
        pj_job_dict['commit_hash'] = commit_hash
        pj_job_dict['manifests_id'] = tx_manifest.id
        pj_job_dict['created_at'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        pj_job_dict['resource_type'] = resource_subject # This used to be rc.resource.identifier
        pj_job_dict['input_format'] = input_format
        pj_job_dict['source'] = f'{source_url_base}/{file_key}'
        pj_job_dict['cdn_bucket'] = AppSettings.cdn_bucket_name
        pj_job_dict['cdn_file'] = f'tx/job/{our_job_id}.zip'
        pj_job_dict['output'] = f"https://{AppSettings.cdn_bucket_name}/{pj_job_dict['cdn_file']}"
        pj_job_dict['callback'] = f'{AppSettings.api_url}/client/callback'
        pj_job_dict['output_format'] = 'html'
        # NOTE: following line removed as stats recording used too much disk space
        # pj_job_dict['user_projects_invoked_string'] = user_projects_invoked_string # Need to save this for reuse
        pj_job_dict['links'] = {
            'href': f'{AppSettings.api_url}/tx/job/{our_job_id}',
            'rel': 'self',
            'method': 'GET'
        }
        pj_job_dict['door43_webhook_received_at'] = submitted_json_payload['door43_webhook_received_at']
        if preprocessor_warning_list:
            pj_job_dict['preprocessor_warnings'] = preprocessor_warning_list
        if 'echoed_from_production' in submitted_json_payload: # helps us keep track of where jobs are coming from in dev- chain
            pj_job_dict['echoed_from_production'] = submitted_json_payload['echoed_from_production']
        pj_job_dict['status'] = None
        pj_job_dict['success'] = False

        # Save the job info in Redis for the callback to use
        remember_job(pj_job_dict, redis_connection)

        # Get S3 cdn bucket/dir and empty it
        s3_commit_key = f"u/{pj_job_dict['repo_owner_username']}/{pj_job_dict['repo_name']}/{pj_job_dict['commit_id']}"
        clear_commit_directory_in_cdn(s3_commit_key)

        # Pass the work request onto the tX system
        AppSettings.logger.info(f"Post request to tX system @ {tx_post_url} …")
        tx_payload = {
            'job_id': our_job_id,
            'identifier': our_identifier, # So we can recognise this job inside tX Job Handler
            'resource_type': resource_subject, # This used to be rc.resource.identifier
            'input_format': 'usfm' if resource_subject=='bible' and input_format=='txt' \
                                else input_format, # special case for .txt Bibles
            'output_format': 'html',
            'source': source_url_base + '/' + file_key,
            'callback': 'http://127.0.0.1:8080/tx-callback/' \
                            if prefix and debug_mode_flag and ':8090' in tx_post_url \
                        else DOOR43_CALLBACK_URL,
            # TODO: gogs_user_token logic can be completely removed from the program
            #           if we're certain we're not worried about Host header spoofing.
            #           (Checking Host header is our new/current ID mechanism.)
            # 'user_token': gogs_user_token, # Used to be checked by tX enqueue job
            #   but it now authenticates because we usually send this from git.door43.org
            #       (or when debugging, from 127.0.0.1:80).
            }
        if 'options' in pj_job_dict and pj_job_dict['options']:
            AppSettings.logger.info(f"Have convert job options: {pj_job_dict['options']}!")
            tx_payload['options'] = pj_job_dict['options']

        AppSettings.logger.debug(f"Payload for tX: {tx_payload}")
        response:Optional[requests.Response]
        try:
            response = requests.post(tx_post_url, json=tx_payload)
        except requests.exceptions.ConnectionError as e:
            AppSettings.logger.critical(f"Callback connection error: {e}")
            response = None
        if response:
            #AppSettings.logger.info(f"response.status_code = {response.status_code}, response.reason = {response.reason}")
            #AppSettings.logger.debug(f"response.headers = {response.headers}")
            try:
                AppSettings.logger.info(f"response.json = {response.json()}")
            except json.decoder.JSONDecodeError:
                AppSettings.logger.info("No valid response JSON found")
                AppSettings.logger.debug(f"response.text = {response.text}")
            if response.status_code != 200:
                AppSettings.logger.critical(f"Failed to submit job to tX:"
                                            f" {response.status_code}={response.reason}")
        else: # no response
            error_msg = "Submission of job to tX system got no response"
            AppSettings.logger.critical(error_msg)
            raise Exception(error_msg) # So we go into the FAILED queue and monitoring system


        # if rc.resource.file_ext in ('usfm', 'usfm3'): # Upload source files to BibleDropBox
        #     if prefix and not debug_mode_flag: # Only for dev- chain
        #         # This was intended for comparing USFM linting during development of that area of code
        #         AppSettings.logger.info(f"Submitting {job_descriptive_name} originals to BDB…")
        #         original_zip_filepath = os.path.join(base_temp_dir_name, commit_url.rpartition(os.path.sep)[2] + '.zip')
        #         upload_to_BDB(f"{repo_owner_username}__{repo_name}__({pusher_username})", original_zip_filepath)
        #         # Not using the preprocessed files (only the originals above)
        #         # AppSettings.logger.info(f"Submitting {job_descriptive_name} preprocessed to BDB…")
        #         # upload_to_BDB(f"{repo_owner_username}__{repo_name}__({pusher_username})", preprocessed_zip_file.name)

    return job_descriptive_name
# end of handle_page_build function


def process_webhook_job(queued_json_payload:Dict[str,Any], redis_connection, our_queue) -> str:
    """
    Parameters:
        queued_json_payload is a dict
        redis_connection is a StrictRedis instance

    Sets up a temp folder in the AWS S3 bucket.

    It gathers details from the JSON payload.

    The given payload will be automatically appended to the 'failed' queue
        by rq if an exception is thrown in this module.
    """
    AppSettings.logger.debug(f"WEBHOOK {prefix+' ' if prefix else ''}processing: {queued_json_payload}")


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


    # Setup a temp folder to use
    source_url_base = f'https://s3-{AppSettings.aws_region_name}.amazonaws.com/{AppSettings.pre_convert_bucket_name}'
    # Move everything down one directory level for simple delete
    # NOTE: The base_temp_dir_name needs to be unique if we ever want multiple workers
    # TODO: This might not be enough 6-digit fractions of a second could collide???
    intermediate_dir_name = OUR_NAME + datetime.utcnow().strftime("_%Y-%m-%d_%H:%M:%S.%f")
    base_temp_dir_name = os.path.join(tempfile.gettempdir(), intermediate_dir_name)
    try:
        os.makedirs(base_temp_dir_name)
    except Exception as e:
        AppSettings.logger.warning(f"SetupTempFolder threw an exception: {e}")


    # for fieldname in queued_json_payload: # Display interesting fields given in payload
    #     if fieldname not in ('door43_webhook_retry_count', 'door43_webhook_received_at'):
    #         AppSettings.logger.info(f"{fieldname} = {queued_json_payload[fieldname]!r}")


    # Get the commit_id, commit_url
    try:
        default_branch = queued_json_payload['repository']['default_branch']
    except KeyError:
        AppSettings.logger.critical("No default branch specified")
        default_branch = 'NoDefaultBranch'
    AppSettings.logger.debug(f"Got default_branch='{default_branch}'")

    # Gather other details from the commit that we will note for the job(s)
    repo_owner_username = queued_json_payload['repository']['owner']['username']
    repo_name = queued_json_payload['repository']['name']

    commit_branch = commit_hash = repo_data_url = tag_name = None
    if queued_json_payload['DCS_event'] == 'push':
        try:
            commit_branch = queued_json_payload['ref'].split('/')[2]
        except (IndexError, AttributeError):
            AppSettings.logger.critical(f"Could not determine commit branch from '{queued_json_payload['ref']}'")
            commit_branch = 'UnknownCommitBranch'
        except KeyError:
            AppSettings.logger.critical("No commit branch specified")
            commit_branch = 'NoCommitBranch'
        # if commit_branch != default_branch:
        #     err_msg = f"Commit branch: '{commit_branch}' is not the default branch ({default_branch})"
        #     AppSettings.logger.critical(err_msg)
        #     return False, {'error': f"{err_msg}."}
        AppSettings.logger.debug(f"Got commit_branch='{commit_branch}'")

        commit_hash = queued_json_payload['after']
        commit = None
        for some_commit in queued_json_payload['commits']:
            if some_commit['id'] == commit_hash:
                commit = some_commit
                break
        commit_hash = commit_hash[:10]  # Only use the short form
        AppSettings.logger.debug(f"Got original commit_hash='{commit_hash}'")
        repo_data_url = commit['url']
        action_message = commit['message'].strip() # Seems to always end with a newline

        if 'pusher' in queued_json_payload:
            pusher_dict = queued_json_payload['pusher']
        else:
            pusher_dict = {'username': commit['author']['username']}
        pusher_username = pusher_dict['username']
        our_identifier = f"'{pusher_username}' pushing '{repo_owner_username}/{repo_name}'"

    elif queued_json_payload['DCS_event'] == 'release':
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
        # else:
            # pusher_dict = {'username': commit['author']['username']}
        pusher_username = pusher_dict['username']
        our_identifier = f"'{pusher_username}' releasing '{repo_owner_username}/{repo_name}'"

    # elif queued_json_payload['DCS_event'] == 'create': # create a branch
    #     if queued_json_payload['ref_type'] != 'branch':
    #         AppSettings.logger.critical(f"Unexpected create ref-type: '{queued_json_payload['ref_type']}'")
    #     try:
    #         created_branch_name = queued_json_payload['ref']
    #     except (IndexError, AttributeError):
    #         AppSettings.logger.critical(f"Could not determine created branch from '{queued_json_payload['ref']}'")
    #         created_branch_name = 'UnknownCreatedBranch'
    #     except KeyError:
    #         AppSettings.logger.critical("No commit branch specified")
    #         created_branch_name = 'NoCreatedBranch'
    #     AppSettings.logger.debug(f"Got created_branch_name='{created_branch_name}'")
    #     repo_data_url = f"{queued_json_payload['repository']['html_url']}/archive/{created_branch_name}.zip"
    #     action_message = created_branch_name
    #     sender_username = queued_json_payload['sender']['username']
    #     our_identifier = f"'{sender_username}' creating '{repo_owner_username}/{repo_name}/{created_branch_name}'"

    elif queued_json_payload['DCS_event'] == 'delete': # delete a branch
        if queued_json_payload['ref_type'] != 'branch':
            AppSettings.logger.critical(f"Unexpected delete ref-type: '{queued_json_payload['ref_type']}'")
        if queued_json_payload['pusher_type'] != 'user':
            AppSettings.logger.critical(f"Unexpected delete pusher_type-type: '{queued_json_payload['pusher_type']}'")
        try:
            deleted_branch_name = queued_json_payload['ref']
        except (IndexError, AttributeError):
            AppSettings.logger.critical(f"Could not determine deleted branch from '{queued_json_payload['ref']}'")
            deleted_branch_name = 'UnknownDeletedBranch'
        except KeyError:
            AppSettings.logger.critical("No commit branch specified")
            deleted_branch_name = 'NoDeletedBranch'
        AppSettings.logger.debug(f"Got deleted_branch_name='{deleted_branch_name}'")
        action_message = deleted_branch_name
        sender_username = queued_json_payload['sender']['username']
        our_identifier = f"'{sender_username}' deleting '{repo_owner_username}/{repo_name}/{deleted_branch_name}'"

    elif queued_json_payload['DCS_event'] == 'fork':
        original_repo_owner_username, original_repo_name = repo_owner_username, repo_name
        repo_owner_username = queued_json_payload['forkee']['owner']['username']
        if queued_json_payload['forkee']['name'] != repo_name:
            AppSettings.logger.debug(f"Changing forked repo name from '{repo_name}' to '{queued_json_payload['forkee']['name']}'")
            repo_name = queued_json_payload['forkee']['name']

        try:
            commit_branch = queued_json_payload['forkee']['default_branch']
        except (IndexError, AttributeError):
            AppSettings.logger.critical(f"Could not determine branch from '{queued_json_payload['forkee']}'")
            commit_branch = 'UnknownCommitBranch'
        except KeyError:
            AppSettings.logger.critical("No branch specified")
            commit_branch = 'NoCommitBranch'
        AppSettings.logger.debug(f"Got forked branch='{commit_branch}'")

        repo_data_url = f"{queued_json_payload['repository']['parent']['html_url']}/archive/{commit_branch}.zip"
        action_message = "fork"

        if 'sender' in queued_json_payload:
            sender_dict = queued_json_payload['sender']
        else:
            sender_dict = {'username': commit['author']['username']}
        sender_username = sender_dict['username']

        our_identifier = f"'{sender_username}' forking '{original_repo_owner_username}/{original_repo_name}' to '{repo_owner_username}{'/'+repo_name if repo_name!=original_repo_name else ''}'"

    else:
        AppSettings.logger.critical(f"Can't handle '{queued_json_payload['DCS_event']}' yet!")

    if commit_branch == default_branch:
        commit_type = 'defaultBranch'
        commit_id = commit_branch
    elif tag_name:
        commit_type = 'tag'
        commit_id = tag_name
    elif commit_branch not in (None, 'UnknownCommitBranch', 'NoCommitBranch'):
        commit_type = 'branch'
        commit_id = commit_branch
    elif queued_json_payload['DCS_event'] == 'delete':
        commit_type = 'delete'
        commit_id = deleted_branch_name
    else:
        commit_type = 'unknown'
        commit_id = None
    commit_id_string = commit_id if commit_id is None else "'"+commit_id+"'"
    AppSettings.logger.debug(f"Got new '{commit_type}' commit_id={commit_id_string} (commit_hash={commit_hash})")
    if repo_data_url:
        AppSettings.logger.debug(f"Got repo_data_url='{repo_data_url}'")


    AppSettings.logger.info(f"Processing job for {our_identifier} for \"{action_message}\"")
    # Seems that statsd 3.3.0 can only handle ASCII chars (not full Unicode)
    ascii_repo_owner_username_bytes = repo_owner_username.encode('ascii', 'replace') # Replaces non-ASCII chars with '?'
    adjusted_repo_owner_username = ascii_repo_owner_username_bytes.decode('utf-8') # Recode as a str
    # ascii_repo_name_bytes = repo_name.encode('ascii', 'replace') # Replaces non-ASCII chars with '?'
    # adjusted_repo_name = ascii_repo_name_bytes.decode('utf-8') # Recode as a str
    stats_client.incr(f'{webhook_stats_prefix}.users.invoked.{adjusted_repo_owner_username}')
    # Using a hyphen as separator as forward slash gets changed to hyphen anyway
    # NOTE: following line removed as stats recording used too much disk space
    # user_projects_invoked_string = f'{job_handler_stats_prefix}.user-projects.invoked.{adjusted_repo_owner_username}--{adjusted_repo_name}'


    if queued_json_payload['DCS_event'] == 'delete':
        job_descriptive_name = f'{our_identifier}'
        handle_branch_delete(base_temp_dir_name, repo_owner_username, repo_name, deleted_branch_name)
    elif commit_id: # for'push' or 'release' or create—we have a repo to process and a page to build
        # Here's our programmed failure (for remotely testing failures)
        if queued_json_payload['DCS_event']=='push' and pusher_username=='Failure' \
        and 'full_name' in pusher_dict and pusher_dict['full_name']=='Push Test':
            deliberateFailureForTesting  # type: ignore
        job_descriptive_name = handle_page_build(base_temp_dir_name, queued_json_payload, redis_connection,
                            commit_type, commit_id, commit_hash, repo_data_url,
                            repo_owner_username, repo_name, source_url_base,
                            our_identifier, our_queue)
    else:
        AppSettings.logger.critical(f"Nothing to process for '{queued_json_payload['DCS_event']}!")


    if prefix and debug_mode_flag:
        AppSettings.logger.debug(f"Temp folder '{base_temp_dir_name}' has been left on disk for debugging!")
    else:
        remove_tree(base_temp_dir_name)  # cleanup
    # AppSettings.logger.info(f"{prefixed_our_name} process_webhook_job() for {job_descriptive_name} is finishing with {build_log_dict}")
    AppSettings.logger.info(f"{prefixed_our_name} process_webhook_job() for {job_descriptive_name} has finished.")
    return job_descriptive_name
#end of process_webhook_job function


def job(queued_json_payload:Dict[str,Any]) -> None:
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
    empty_folder('/tmp/', only_prefix='Door43_') # Stops failed jobs from accumulating in /tmp

    current_job = get_current_job()
    #print(f"Current job: {current_job}") # Mostly just displays the job number and payload
    #print("dir",dir(current_job))
    #   dir ['__class__', '__delattr__', '__dict__', '__dir__', '__doc__', '__eq__',
    #       '__format__', '__ge__', '__getattribute__', '__gt__', '__hash__', '__init__',
    #       '__init_subclass__', '__le__', '__lt__', '__module__', '__ne__', '__new__',
    #       '__reduce__', '__reduce_ex__', '__repr__', '__setattr__', '__sizeof__',
    #       '__str__', '__subclasshook__', '__weakref__', '_args', '_data', '_dependency_id',
    #       '_execute', '_func_name', '_get_status', '_id', '_instance', '_kwargs', '_result',
    #       '_set_status', '_status', '_unpickle_data', 'args', 'cancel', 'cleanup',
    #       'connection', 'create', 'created_at', 'data', 'delete', 'delete_dependents',
    #       'dependency', 'dependent_ids', 'dependents_key', 'dependents_key_for',
    #       'description', 'ended_at', 'enqueued_at', 'exc_info', 'exists', 'fetch', 'func',
    #       'func_name', 'get_call_string', 'get_id', 'get_result_ttl', 'get_status',
    #       'get_ttl', 'id', 'instance', 'is_failed', 'is_finished', 'is_queued', 'is_started',
    #       'key', 'key_for', 'kwargs', 'meta', 'origin', 'perform',
    #       'redis_job_namespace_prefix', 'refresh', 'register_dependency', 'result',
    #       'result_ttl', 'return_value', 'save', 'save_meta', 'set_id', 'set_status',
    #       'started_at', 'status', 'timeout', 'to_dict', 'ttl']
    #for fieldname in current_job.__dict__:
        #print(f"{fieldname}: {current_job.__dict__[fieldname]}")
    #print("id",current_job.id) # Displays job number
    #print("origin",current_job.origin) # Displays queue name
    #print("meta",current_job.meta) # Empty dict

    # AppSettings.logger.info(f"Updating queue statistics…")
    our_queue= Queue(webhook_queue_name, connection=current_job.connection)
    len_our_queue = len(our_queue) # Should normally sit at zero here

    abort_duplicate_flag, job_descriptive_name = check_for_forthcoming_pushes_in_queue(queued_json_payload, our_queue)
    if not abort_duplicate_flag:
        # AppSettings.logger.debug(f"Queue '{webhook_queue_name}' length={len_our_queue}")
        stats_client.gauge(f'"{door43_stats_prefix}.enqueue-job.webhook.queue.length.current', len_our_queue)
        AppSettings.logger.info(f"Updated stats for '{door43_stats_prefix}.enqueue-job.webhook.queue.length.current' to {len_our_queue}")

        #print(f"Got a job from {current_job.origin} queue: {queued_json_payload}")
        #print(f"\nGot job {current_job.id} from {current_job.origin} queue")
        #queue_prefix = 'dev-' if current_job.origin.startswith('dev-') else ''
        #assert queue_prefix == prefix
        try:
            job_descriptive_name = process_webhook_job(queued_json_payload, current_job.connection, our_queue)
        except Exception as e:
            # Catch most exceptions here so we can log them to CloudWatch
            AppSettings.logger.critical(f"{prefixed_our_name} webhook threw an exception while processing:\n{queued_json_payload}\ngetting exception:\n{e}: {traceback.format_exc()}")
            AppSettings.close_logger() # Ensure queued logs are uploaded to AWS CloudWatch
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
            logger2.critical(f"{prefixed_our_name} webhook threw an exception while processing:\n{queued_json_payload}\ngetting exception:\n{e}: {traceback.format_exc()}")
            failure_watchtower_log_handler.close()
            # NOTE: following line removed as stats recording used too much disk space
            # stats_client.gauge(user_projects_invoked_string, 1) # Mark as 'failed'
            stats_client.gauge(project_types_invoked_string, 1) # Mark as 'failed'
            raise e # We raise the exception again so it goes into the failed queue

    elapsed_milliseconds = round((time() - start_time) * 1000)
    stats_client.timing(f'{webhook_stats_prefix}.job.duration', elapsed_milliseconds)
    if elapsed_milliseconds < 2000:
        AppSettings.logger.info(f"{prefixed_our_name} webhook job handling for {job_descriptive_name} completed in {elapsed_milliseconds:,} milliseconds.")
    else:
        AppSettings.logger.info(f"{prefixed_our_name} webhook job handling for {job_descriptive_name} completed in {round(time() - start_time)} seconds.")

    stats_client.incr(f'{webhook_stats_prefix}.jobs.completed')
    AppSettings.close_logger() # Ensure queued logs are uploaded to AWS CloudWatch
# end of job function

# end of webhook.py for door43_enqueue_job
