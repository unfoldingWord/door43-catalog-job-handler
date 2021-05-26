# NOTE: This module name and function name are defined by the rq package and our own door43-enqueue-job package
# This code adapted by RJH Sept 2018 from webhook.py

# NOTE: rq_settings.py is executed at program start-up, reads some environment variables, and sets queue name, etc.
#       job() function (at bottom here) is executed by rq package when there is an available entry in the named queue.

# NOTE: a number of other services depend on the outputs of this module,
#           e.g., Door43.org reads its project.json to find the rendered branches/releases,
#                   and build_log.json (i.e., from the door43 bucket, not from the cdn one).
#           (This means that changing variable names in those dicts
#               might have unintended consequences.)


# Python imports
from typing import Dict, List, Tuple, Any, Optional, Union, Literal
import os
from datetime import datetime
import time
import json
import tempfile
import traceback

# Library (PyPI) imports
from rq import get_current_job, Queue
from statsd import StatsClient # Graphite front-end
import requests

# Local imports
from rq_settings import prefix, debug_mode_flag, REDIS_JOB_LIST, callback_queue_name
from app_settings.app_settings import AppSettings
from client_converter_callback import ClientConverterCallback
from client_linter_callback import ClientLinterCallback
from door43_tools.project_deployer import ProjectDeployer
from general_tools.file_utils import write_file, remove_tree



AppSettings(prefix=prefix)
if prefix not in ('', 'dev-'):
    AppSettings.logger.critical(f"Unexpected prefix: '{prefix}' — expected '' or 'dev-'")
door43_stats_prefix = f"door43.{'dev' if prefix else 'prod'}"
job_handler_stats_prefix = f"{door43_stats_prefix}.job-handler" # Can't add .callback here coz we also have .total
callback_stats_prefix = f"{job_handler_stats_prefix}.callback"


# Get the Graphite URL from the environment, otherwise use a local test instance
graphite_url = os.getenv('GRAPHITE_HOSTNAME', 'localhost')
stats_client = StatsClient(host=graphite_url, port=8125)



def verify_expected_job(vej_job_id:str, vej_redis_connection) -> Union[Dict[str,Any], Literal[False]]:
    """
    Check that we have this outstanding job in a REDIS dict
        and delete the REDIS dict entry once we make a match.

    Return the job dict or False
    """
    # vej_job_id = vej_job_dict['job_id']
    # AppSettings.logger.debug(f"verify_expected_job({vej_job_id})")

    outstanding_jobs_dict_bytes = vej_redis_connection.get(REDIS_JOB_LIST) # Gets bytes!!!
    if not outstanding_jobs_dict_bytes:
        AppSettings.logger.error("No expected jobs found in redis store")
        return False
    # AppSettings.logger.debug(f"Got outstanding_jobs_dict_bytes:"
    #                             f" ({len(outstanding_jobs_dict_bytes)}) {outstanding_jobs_dict_bytes}")
    assert isinstance(outstanding_jobs_dict_bytes,bytes)
    outstanding_jobs_dict_json_string = outstanding_jobs_dict_bytes.decode() # bytes -> str
    assert isinstance(outstanding_jobs_dict_json_string,str)
    outstanding_jobs_dict = json.loads(outstanding_jobs_dict_json_string)
    assert isinstance(outstanding_jobs_dict,dict)
    AppSettings.logger.info(f"Currently have {len(outstanding_jobs_dict)}"
                               f" outstanding job(s) in '{REDIS_JOB_LIST}' redis store")
    if vej_job_id not in outstanding_jobs_dict:
        AppSettings.logger.error(f"Not expecting job with id of {vej_job_id}")
        AppSettings.logger.debug(f"Only had job ids: {outstanding_jobs_dict.keys()}")
        return False
    this_job_dict = outstanding_jobs_dict[vej_job_id]

    # We found a match—delete that job from the outstanding list
    AppSettings.logger.debug(f"Found job match for {vej_job_id}")
    del outstanding_jobs_dict[vej_job_id]
    if outstanding_jobs_dict:
        AppSettings.logger.debug(f"Still have {len(outstanding_jobs_dict)}"
                                    f" outstanding job(s) in '{REDIS_JOB_LIST}'")
        # Update the job dict in redis now that this job has been deleted from it
        outstanding_jobs_json_string = json.dumps(outstanding_jobs_dict)
        vej_redis_connection.set(REDIS_JOB_LIST, outstanding_jobs_json_string)
    else: # no outstanding jobs left
        AppSettings.logger.info("Deleting the final outstanding job"
                                  f" in '{REDIS_JOB_LIST}' redis store")
        del_result = vej_redis_connection.delete(REDIS_JOB_LIST)
        # print("  Got redis delete result:", del_result)
        assert del_result == 1 # Should only have deleted one key

    #AppSettings.logger.debug(f"Returning {this_job_dict}")
    return this_job_dict
# end of verify_expected_job


def merge_results_logs(old_build_log:Dict[str,Any], new_file_results:Dict[str,Any],
                                        converter_flag:bool) -> Dict[str,Any]:
    """
    Given a second partial build log file_results,
        combine the log/warnings/errors lists into the first build_log.
    """
    new_file_results_copy = new_file_results.copy() # Sometimes this gets too big
    if 'warnings' in new_file_results_copy and len(new_file_results_copy['warnings']) > 10:
        new_file_results_copy['warnings'] = f"{new_file_results_copy['warnings'][:5]} …… {new_file_results_copy['warnings'][-5:]}"
    AppSettings.logger.debug(f"Callback.merge_results_logs(…, {new_file_results_copy}, converter_flag={converter_flag})…")
    # AppSettings.logger.debug(f"Callback.merge_results_logs({old_build_log}, {file_results}, {converter_flag})…")
    # saved_build_log = old_build_log.copy()
    if not old_build_log:
        AppSettings.logger.debug(f"Callback.merge_results_logs() about to return file_results={new_file_results}")
        return new_file_results
    if new_file_results:
        # The following four lines modify old_build_log as a side-effect!
        merge_dicts_lists(old_build_log, new_file_results, 'message')
        merge_dicts_lists(old_build_log, new_file_results, 'log')
        merge_dicts_lists(old_build_log, new_file_results, 'warnings')
        merge_dicts_lists(old_build_log, new_file_results, 'errors')
        if converter_flag \
        and 'success' in new_file_results \
        and new_file_results['success'] is False:
            old_build_log['success'] = new_file_results['success']
    # if build_log == saved_build_log:
    #     AppSettings.logger.debug(f"Callback.merge_results_logs() about to return build_log WITHOUT CHANGES\n\n")
    # else:
    #     AppSettings.logger.debug(f"Callback.merge_results_logs() about to return build_log={build_log}\n\n")
    return old_build_log
# end of merge_results_logs function


def merge_dicts_lists(old_build_log:Dict[str,Any], new_file_results:Dict[str,Any], key:str) -> None:
    """
    Used for merging log dicts from various sub-processes.

    build_log is a dict
    new_file_results is a dict
    value is a key (string) for the lists that will be merged if in both dicts

    Alters first parameter old_build_log in place.
    """
    # AppSettings.logger.debug(f"Callback.merge_dicts_lists({build_log}, {new_file_results}, '{key}')…")
    # saved_build_log = build_log.copy()
    if key in new_file_results:
        value = new_file_results[key]
        if value:
            # assert isinstance(value, (list, str)) # Oh, it can be str for 'message'!
            if (key in old_build_log) and (old_build_log[key]):
                old_build_log[key] += value # Concatenate 2nd list to the build_log one
            else:
                old_build_log[key] = value
    # if old_build_log == saved_build_log:
    #     AppSettings.logger.debug(f"Callback.merge_dicts_lists(…, {key}) returning with UNCHANGED BUILD_LOG\n")
    # else:
    #     AppSettings.logger.debug(f"Callback.merge_dicts_lists(…, {key}) returning with build_log={build_log}\n")
# end of merge_dicts_lists function


def get_jobID_from_commit_buildLog(project_folder_key:str, ix:int, commit_id:str) -> Optional[str]:
    """
    Look for build_log.json in the Door43 bucket
        and extract the job_id from it.

    NOTE: It seems like old builds also put build_log.json in the CDN bucket
            but the new ones don't seem to have that.

    Return None if anything fails.
    """
    file_key = f'{project_folder_key}{commit_id}/build_log.json'
    try:
        file_content = AppSettings.door43_s3_handler() \
                    .resource.Object(bucket_name=AppSettings.door43_bucket_name, key=file_key) \
                    .get()['Body'].read().decode('utf-8')
        json_content = json.loads(file_content)
        return json_content['job_id']
    except Exception as e:
        AppSettings.logger.critical(f"get_jobID_from_commit_buildLog threw an exception while getting {prefix}D43 {ix:,} '{file_key}': {e}")
        return None
# end of get_jobID_from_commit_buildLog function


def clear_commit_directory_from_bucket(s3_bucket_handler, s3_commit_key: str) -> None:
    """
    Clear out and remove the commit directory from the requested bucket for this project revision.
    """
    AppSettings.logger.debug(f"Clearing objects from commit directory '{s3_commit_key}' in {s3_bucket_handler.bucket_name} bucket…")
    s3_bucket_handler.bucket.objects.filter(Prefix=s3_commit_key).delete()
# end of clear_commit_directory_from_bucket function


def get_list_from_Gitea(Gitea_url:str) -> List[str]:
    """
    Send a GET request to the URL
        and return the list from the returned JSON.
    """
    AppSettings.logger.debug(f"get_list_from_Gitea({Gitea_url})…")

    this_list = []
    response:Optional[requests.Response]
    try:
        # AppSettings.logger.debug(f"GETting list from '{Gitea_url}'…")
        response = requests.get(Gitea_url)
    except requests.exceptions.ConnectionError as e:
        AppSettings.logger.critical(f"get_list_from_Gitea connection error: {e}")
        response = None
    if response:
        # AppSettings.logger.info(f"response.status_code = {response.status_code}, response.reason = {response.reason}")
        # AppSettings.logger.debug(f"response.headers = {response.headers}")
        try:
            this_list = response.json()
            # AppSettings.logger.info(f"response_json = {this_list}")
            assert isinstance( this_list, list) # Should be a list of dicts
        except json.decoder.JSONDecodeError:
            AppSettings.logger.info("No valid list response JSON found")
            AppSettings.logger.debug(f"response.text = {response.text}")
        if response.status_code != 200:
            AppSettings.logger.critical(f"Failed to submit list request to Gitea:"
                                        f" {response.status_code}={response.reason}")
    else: # no response
        error_msg = "Submission of list request to Gitea got no response"
        AppSettings.logger.critical(error_msg)
        # raise Exception(error_msg) # So we go into the FAILED queue and monitoring system

    # AppSettings.logger.debug(f"  Returning this_list={this_list}")
    return this_list
# end of get_list_from_Gitea function


def get_current_branch_names_list(repo_owner_username:str, repo_name:str) -> List[str]:
    """
    Ask Gitea for a list of all current branches.
    """
    AppSettings.logger.debug(f"get_current_branch_names_list({repo_owner_username}, {repo_name})…")

    current_branch_list = get_list_from_Gitea(f'https://git.door43.org/api/v1/repos/{repo_owner_username}/{repo_name}/branches')
    current_branch_names_list = [this_dict['name'] for this_dict in current_branch_list]
    AppSettings.logger.info(f"Returning current_branch_names_list={current_branch_names_list}")
    return current_branch_names_list
# end of get_current_branch_names_list function


def get_current_tag_names_list(repo_owner_username:str, repo_name:str) -> List[str]:
    """
    Ask Gitea for a list of all current release tags.
    """
    AppSettings.logger.debug(f"get_current_tag_names_list({repo_owner_username}, {repo_name})…")

    current_tag_list = get_list_from_Gitea(f'https://git.door43.org/api/v1/repos/{repo_owner_username}/{repo_name}/releases')
    current_tag_names_list = [this_dict['tag_name'] for this_dict in current_tag_list]
    AppSettings.logger.info(f"Returning current_tag_names_list={current_tag_names_list}")
    return current_tag_names_list
# end of get_current_tag_names_list function


def remove_excess_commits(commits_list:list, repo_owner_username:str, repo_name:str) -> List[Dict[str,Any]]:
    """
    Given a list of commits (oldest first),
        remove the unnecessary ones from the list
        and DELETE THE files from S3!

    Written: Aug 2019
        This was especially important as we moved from hash numbers
            to tag and branch names.

    NOTE: Gitea before 1.11 had a bug where it didn't always notify of deleted branches.
        Also, the dev- chain is not always enabled, so doesn't get all notifications anyway.
        So RJH added code in March 2020 to check for now non-existent branches.
    """
    MIN_WANTED_COMMITS = 1
    # Lowered from 2,400 to 500  20Dec19—not sure why ru_gl/ru_tq_2lv kept getting timeout errors
    MAX_ALLOWED_REMOVED_FOLDERS = 500 # Don't want to get job timeouts—typically can do 3500+ in 600s
                                       #    at least project.json will slowly get smaller if we limit this.
                                       # Each commit hash to be deleted has three folders to remove.
    AppSettings.logger.debug(f"remove_excess_commits({len(commits_list)}={commits_list}, {repo_owner_username}, {repo_name})…")

    current_branch_names_list = get_current_branch_names_list(repo_owner_username, repo_name)
    current_tag_names_list = get_current_tag_names_list(repo_owner_username, repo_name)

    project_folder_key = f'u/{repo_owner_username}/{repo_name}/'
    new_commits:List[Dict[str,Any]] = []
    removed_folder_count = 0
    # Process it backwards in case we want to count how many we have as we go
    for n, commit in enumerate( reversed(commits_list) ):
        # if DELETE_ENABLED or len(new_commits) < MAX_DEBUG_DISPLAYS: # don't clutter logs too much
        AppSettings.logger.debug(f" Investigating {commit['type']} '{commit['id']}' commit (already have {len(new_commits)} — want min of {MIN_WANTED_COMMITS})")
        # elif len(new_commits) == MAX_DEBUG_DISPLAYS: # don't clutter logs too much
            # AppSettings.logger.debug("  Logging suppressed for remaining hashes…")
        deleted_flag = False
        if len(new_commits) >= MIN_WANTED_COMMITS \
        and removed_folder_count < MAX_ALLOWED_REMOVED_FOLDERS:
            if commit['type'] in ('hash','artifact',): # but not 'unknown'—can delete old master branches
                # Delete the commit hash folders from both CDN and D43 buckets
                commit_key = f"{project_folder_key}{commit['id']}"
                AppSettings.logger.info(f"  {n:,} Removing {prefix} CDN & D43 '{commit['type']}' '{commit['id']}' commits! …")
                # AppSettings.logger.info(f"  {n:,} Removing {prefix}CDN '{commit['type']}' '{commit['id']}' commit! …")
                clear_commit_directory_from_bucket(AppSettings.cdn_s3_handler(), commit_key)
                removed_folder_count += 1
                # AppSettings.logger.info(f"  {n:,} Removing {prefix}D43 '{commit['type']}' '{commit['id']}' commit! …")
                clear_commit_directory_from_bucket(AppSettings.door43_s3_handler(), commit_key)
                removed_folder_count += 1
                # Delete the pre-convert .zip file (available on Download button) from its bucket
                if commit['job_id']:
                    zipFile_key = f"preconvert/{commit['job_id']}.zip"
                    AppSettings.logger.info(f"  {n:,} Removing {prefix}PreConvert '{commit['type']}' '{zipFile_key}' file! …")
                    clear_commit_directory_from_bucket(AppSettings.pre_convert_s3_handler(), zipFile_key)
                    removed_folder_count += 1
                else: # don't know the job_id (or the zip file was already deleted)
                    AppSettings.logger.warning(f" {n:,} No job_id so pre-convert zip file not deleted.")
                # Setup redirects (so users don't get 404 errors from old saved links)
                old_repo_key = f"{project_folder_key}{commit['id']}"
                latest_repo_key = f"/{project_folder_key}{new_commits[-1]['id']}" # Must start with /
                AppSettings.logger.info(f"  {n:,} Redirecting {old_repo_key} and {old_repo_key}/index.html to {latest_repo_key} …")
                AppSettings.door43_s3_handler().redirect(key=old_repo_key, location=latest_repo_key)
                AppSettings.door43_s3_handler().redirect(key=f'{old_repo_key}/index.html', location=latest_repo_key)
                deleted_flag = True
            elif commit['type'] == 'branch' and current_branch_names_list:
                # Some branches may have been deleted without us being informed
                branch_name = commit['id']
                AppSettings.logger.debug(f"Checking branch '{branch_name}' against {current_branch_names_list}…")
                if branch_name not in current_branch_names_list:
                    commit_key = f"{project_folder_key}{commit['id']}"
                    AppSettings.logger.info(f"  {n:,} Removing {prefix} CDN & D43 '{branch_name}' branch! …")
                    # AppSettings.logger.info(f"  {n:,} Removing {prefix}CDN '{branch_name}' branch! …")
                    clear_commit_directory_from_bucket(AppSettings.cdn_s3_handler(), commit_key)
                    removed_folder_count += 1
                    # AppSettings.logger.info(f"  {n:,} Removing {prefix}D43 '{branch_name}' branch! …")
                    clear_commit_directory_from_bucket(AppSettings.door43_s3_handler(), commit_key)
                    removed_folder_count += 1
                    # Delete the pre-convert .zip file (available on Download button) from its bucket
                    if commit['job_id']:
                        zipFile_key = f"preconvert/{commit['job_id']}.zip"
                        AppSettings.logger.info(f"  {n:,} Removing {prefix}PreConvert '{commit['type']}' '{zipFile_key}' file! …")
                        clear_commit_directory_from_bucket(AppSettings.pre_convert_s3_handler(), zipFile_key)
                        removed_folder_count += 1
                    else: # don't know the job_id (or the zip file was already deleted)
                        AppSettings.logger.warning(f" {n:,} No job_id so pre-convert zip file not deleted.")
                    # Setup redirects (so users don't get 404 errors from old saved links)
                    old_repo_key = f"{project_folder_key}{branch_name}"
                    latest_repo_key = f"/{project_folder_key}{new_commits[-1]['id']}" # Must start with /
                    AppSettings.logger.info(f"  {n:,} Redirecting {old_repo_key} and {old_repo_key}/index.html to {latest_repo_key} …")
                    AppSettings.door43_s3_handler().redirect(key=old_repo_key, location=latest_repo_key)
                    AppSettings.door43_s3_handler().redirect(key=f'{old_repo_key}/index.html', location=latest_repo_key)
                    deleted_flag = True
            elif commit['type'] == 'tag' and current_tag_names_list:
                # Some branches may have been deleted without us being informed
                tag_name = commit['id']
                AppSettings.logger.debug(f"Checking tag '{tag_name}' against {current_tag_names_list}…")
                if tag_name not in current_tag_names_list:
                    commit_key = f"{project_folder_key}{commit['id']}"
                    AppSettings.logger.info(f"  {n:,} Removing {prefix} CDN & D43 '{tag_name}' release! …")
                    # AppSettings.logger.info(f"  {n:,} Removing {prefix}CDN '{tag_name}' release! …")
                    clear_commit_directory_from_bucket(AppSettings.cdn_s3_handler(), commit_key)
                    removed_folder_count += 1
                    # AppSettings.logger.info(f"  {n:,} Removing {prefix}D43 '{tag_name}' release! …")
                    clear_commit_directory_from_bucket(AppSettings.door43_s3_handler(), commit_key)
                    removed_folder_count += 1
                    # Delete the pre-convert .zip file (available on Download button) from its bucket
                    if commit['job_id']:
                        zipFile_key = f"preconvert/{commit['job_id']}.zip"
                        AppSettings.logger.info(f"  {n:,} Removing {prefix}PreConvert '{commit['type']}' '{zipFile_key}' file! …")
                        clear_commit_directory_from_bucket(AppSettings.pre_convert_s3_handler(), zipFile_key)
                        removed_folder_count += 1
                    else: # don't know the job_id (or the zip file was already deleted)
                        AppSettings.logger.warning(f" {n:,} No job_id so pre-convert zip file not deleted.")
                    # Setup redirects (so users don't get 404 errors from old saved links)
                    old_repo_key = f"{project_folder_key}{tag_name}"
                    latest_repo_key = f"/{project_folder_key}{new_commits[-1]['id']}" # Must start with /
                    AppSettings.logger.info(f"  {n:,} Redirecting {old_repo_key} and {old_repo_key}/index.html to {latest_repo_key} …")
                    AppSettings.door43_s3_handler().redirect(key=old_repo_key, location=latest_repo_key)
                    AppSettings.door43_s3_handler().redirect(key=f'{old_repo_key}/index.html', location=latest_repo_key)
                    deleted_flag = True
        if not deleted_flag:
            AppSettings.logger.debug("  Keeping this one.")
            new_commits.insert(0, commit) # Insert at beginning to get the order correct again
    if removed_folder_count > 9:
        len_new_commits = len(new_commits)
        AppSettings.logger.info(f"{removed_folder_count:,} commit folders deleted and redirected. (Returning {len_new_commits:,} commit{'' if len_new_commits==1 else 's'}).")
    return new_commits
# end of remove_excess_commits


def update_project_file(build_log:Dict[str,Any], output_dirpath:str) -> None:
    """
    project.json is read by the Javascript in door43.org/js/project-page-functions.js
        The commits are used to update the Revision list in the left side-bar.

    Changed March 2020 to read project.json from door43 bucket (not cdn bucket).
        (The updated file gets written to both buckets.)
    """
    build_log_copy = build_log.copy() # Sometimes this gets too big
    if 'warnings' in build_log_copy and len(build_log_copy['warnings']) > 10:
        build_log_copy['warnings'] = f"{build_log_copy['warnings'][:5]} …… {build_log_copy['warnings'][-5:]}"
    AppSettings.logger.debug(f"Callback.update_project_file({build_log_copy}, output_dir={output_dirpath})…")

    commit_id = build_log['commit_id']
    repo_owner_username = build_log['repo_owner_username'] # was 'repo_owner'
    repo_name = build_log['repo_name']
    project_folder_key = f'u/{repo_owner_username}/{repo_name}/'
    project_json_key = f'{project_folder_key}project.json'
    project_json = AppSettings.door43_s3_handler().get_json(project_json_key)
    project_json['user'] = repo_owner_username
    project_json['repo'] = repo_name
    project_json['repo_url'] = f'https://{AppSettings.gogs_url}/{repo_owner_username}/{repo_name}'
    current_commit = {
        'id': commit_id,
        'job_id': build_log['job_id'],
        'type': build_log['commit_type'],
        'created_at': build_log['created_at'],
        'status': build_log['status'],
        'success': build_log['success'],
        # 'started_at': None,
        # 'ended_at': None
    }
    if build_log['commit_hash']:
        current_commit['commit_hash'] = build_log['commit_hash']
    # if 'started_at' in build_log:
    #     current_commit['started_at'] = build_log['started_at']
    # if 'ended_at' in build_log:
    #     current_commit['ended_at'] = build_log['ended_at']

    def is_hash(commit_str:str) -> bool:
        """
        Checks to see if this looks like a hexadecimal (abbreviated to 10 chars) hash
        """
        if len(commit_str) != 10: return False
        for char in commit_str:
            if char not in 'abcdef1234567890': return False
        return True

    if 'commits' not in project_json:
        project_json['commits'] = []
    AppSettings.logger.info(f"Rebuilding commits list (currently {len(project_json['commits']):,}) for project.json…")
    commits:List[Dict[str,Any]] = []
    no_job_id_count = 0
    for ix, c in enumerate(project_json['commits']):
        AppSettings.logger.debug(f"  Looking at {len(commits)}/ '{c['id']}'. Is current commit={c['id'] == commit_id}…")
        # if c['id'] == commit_id: # the old entry for the current commit id
            # Why did this code ever get in here in callback!!!! (Deletes pre-convert folder when it shouldn't!)
            # zip_file_key = f"preconvert/{current_commit['job_id']}.zip"
            # AppSettings.logger.info(f"  Removing obsolete {prefix}pre-convert '{current_commit['type']}' '{commit_id}' {zip_file_key} …")
            # try:
            #     clear_commit_directory_from_bucket(AppSettings.pre_convert_s3_handler(), zip_file_key)
            # except Exception as e:
            #     AppSettings.logger.critical(f"  Remove obsolete pre-convert zipfile threw an exception while attempted to delete '{zip_file_key}': {e}")
            # Not appended to commits here coz it happens below instead
        if c['id'] != commit_id: # a different commit from the current one
            if 'job_id' not in c: # Might be able to remove this eventually
                c['job_id'] = get_jobID_from_commit_buildLog(project_folder_key, ix, c['id'])
                # Returned job id might have been None
                if not c['job_id']: no_job_id_count += 1
            if 'type' not in c: # Might be able to remove this eventually
                c['type'] = 'hash' if is_hash(c['id']) \
                      else 'artifact' if c['id']in ('latest','OhDear') \
                      else 'unknown'
            commits.append(c)
    if no_job_id_count > 10:
        len_commits = len(commits)
        AppSettings.logger.info(f"{no_job_id_count} job ids were unable to be found. Have {len_commits} historical commit{'' if len_commits==1 else 's'}.")
    commits.append(current_commit)

    cleaned_commits = remove_excess_commits(commits, repo_owner_username, repo_name)
    if len(cleaned_commits) < len(commits): # Then we removed some
        # Save a dated (coz this could happen more than once) backup of the project.json file
        save_project_filename = f"project.save.{datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')}.json"
        save_project_filepath = os.path.join(output_dirpath, save_project_filename)
        write_file(save_project_filepath, project_json)
        save_project_json_key = f'{project_folder_key}{save_project_filename}'
        # Don't need to save this twice (March 2020)
        # AppSettings.cdn_s3_handler().upload_file(save_project_filepath, save_project_json_key, cache_time=100)
        AppSettings.door43_s3_handler().upload_file(save_project_filepath, save_project_json_key, cache_time=100)
    # Now save the updated project.json file in both places
    project_json['commits'] = cleaned_commits
    project_filepath = os.path.join(output_dirpath, 'project.json')
    write_file(project_filepath, project_json)
    AppSettings.cdn_s3_handler().upload_file(project_filepath, project_json_key, cache_time=1)
    AppSettings.door43_s3_handler().upload_file(project_filepath, project_json_key, cache_time=1)
# end of update_project_file function


# user_projects_invoked_string = 'user-projects.invoked.unknown--unknown'
project_types_invoked_string = f'{job_handler_stats_prefix}.types.invoked.unknown'
def process_callback_job(pc_prefix:str, queued_json_payload:Dict[str,Any], redis_connection):
    """
    The job info is retrieved from REDIS and matched/checked
    The converted file(s) are downloaded
    Templating is done
    The results are uploaded to the S3 CDN bucket
    The final log is uploaded to the S3 CDN bucket
    The pages are then deployed.

    The given payload will be appended to the 'failed' queue
        if an exception is thrown in this module.
    """
    # global user_projects_invoked_string
    global project_types_invoked_string
    str_payload = str(queued_json_payload)
    str_payload_adjusted = str_payload if len(str_payload)<1500 \
                            else f'{str_payload[:1000]} …… {str_payload[-500:]}'
    AppSettings.logger.info(f"CALLBACK {pc_prefix+' ' if pc_prefix else ''}processing: {str_payload_adjusted}")

    # Check that this is an expected callback job
    if 'job_id' not in queued_json_payload:
        error = "Callback job has no 'job_id' field"
        AppSettings.logger.critical(error)
        raise Exception(error)
    # job_id = queued_json_payload['job_id']
    matched_job_dict = verify_expected_job(queued_json_payload['job_id'], redis_connection)
    # NOTE: The above function also deletes the matched job entry,
    #   so this means callback cannot be successfully retried if it fails below
    if not matched_job_dict:
        error = f"No waiting job found for {queued_json_payload}"
        AppSettings.logger.critical(error)
        raise Exception(error)
    matched_job_dict_copy = matched_job_dict.copy() # Sometimes this gets too big
    if 'preprocessor_warnings' in matched_job_dict_copy and len(matched_job_dict_copy['preprocessor_warnings']) > 10:
        matched_job_dict_copy['preprocessor_warnings'] = f"{matched_job_dict_copy['preprocessor_warnings'][:5]} …… {matched_job_dict_copy['preprocessor_warnings'][-5:]}"
    AppSettings.logger.debug(f"Got matched_job_dict: {matched_job_dict_copy}")
    job_descriptive_name = f"{matched_job_dict['resource_type']}({matched_job_dict['input_format']})"

    this_job_dict = queued_json_payload.copy()
    # Get needed fields that we saved but didn't submit to or receive back from tX
    for fieldname in ('repo_owner_username', 'repo_name', 'commit_id', 'commit_hash',
                                        'input_format', 'door43_webhook_received_at'):
        if prefix and debug_mode_flag: assert fieldname not in this_job_dict
        this_job_dict[fieldname] = matched_job_dict[fieldname]
    # Remove unneeded fields that we saved or received back from tX
    for fieldname in ('callback',):
        if fieldname in this_job_dict:
            del this_job_dict[fieldname]

    if 'preprocessor_warnings' in matched_job_dict:
        # AppSettings.logger.debug(f"Got {len(matched_job_dict['preprocessor_warnings'])}"
        #                            f" remembered preprocessor_warnings: {matched_job_dict['preprocessor_warnings']}")
        # Prepend preprocessor results to linter warnings
        # total_warnings = len(matched_job_dict['preprocessor_warnings']) + len(queued_json_payload['linter_warnings'])
        queued_json_payload['linter_warnings'] = matched_job_dict['preprocessor_warnings'] \
                                               + queued_json_payload['linter_warnings']
        # AppSettings.logger.debug(f"Now have {len(queued_json_payload['linter_warnings'])}"
        #                             f" linter_warnings: {queued_json_payload['linter_warnings']}")
        # assert len(queued_json_payload['linter_warnings']) == total_warnings
        del matched_job_dict['preprocessor_warnings'] # No longer required

    if 'identifier' in queued_json_payload:
        # NOTE: The identifier we send to the tX Job Handler system is a complex string, not a job_id
        identifier = queued_json_payload['identifier']
        AppSettings.logger.debug(f"Got identifier from queued_json_payload: {identifier}.")
        job_descriptive_name = f'{identifier} {job_descriptive_name}'
    if 'identifier' in matched_job_dict: # overrides
        identifier = matched_job_dict['identifier']
        AppSettings.logger.debug(f"Got identifier from matched_job_dict: {identifier}.")
    else:
        identifier = queued_json_payload['job_id']
        AppSettings.logger.debug(f"Got identifier from job_id: {identifier}.")
    # NOTE: following line removed as stats recording used too much disk space
    # user_projects_invoked_string = matched_job_dict['user_projects_invoked_string'] \
    #                     if 'user_projects_invoked_string' in matched_job_dict \
    #                     else f'??{identifier}??'
    project_types_invoked_string = f"{job_handler_stats_prefix}.types.invoked.{matched_job_dict['resource_type']}"

    # NOTE: matched_job_dict gets merged into build_log below
    matched_job_dict['log'] = []
    matched_job_dict['warnings'] = []
    matched_job_dict['errors'] = []

    our_temp_dir = tempfile.mkdtemp(suffix='',
                     prefix='Door43_callback_' + datetime.utcnow().strftime('%Y-%m-%d_%H:%M:%S_'))

    # We get the adapted tx-manager calls to do our work for us
    # It doesn't actually matter which one we do first I think
    AppSettings.logger.info("Running linter post-processing…")
    url_part2 = f"u/{this_job_dict['repo_owner_username']}/{this_job_dict['repo_name']}/{this_job_dict['commit_id']}"
    clc = ClientLinterCallback(this_job_dict, identifier,
                               queued_json_payload['linter_success'],
                               queued_json_payload['linter_info'] if 'linter_info' in queued_json_payload else None,
                               queued_json_payload['linter_warnings'],
                               queued_json_payload['linter_errors'] if 'linter_errors' in queued_json_payload else None,
                                )
                            #    s3_results_key=url_part2)
    linter_log = clc.do_post_processing()
    build_log = merge_results_logs(matched_job_dict, linter_log, converter_flag=False)
    AppSettings.logger.info("Running converter post-processing…")
    ccc = ClientConverterCallback(this_job_dict, identifier,
                                  queued_json_payload['converter_success'],
                                  queued_json_payload['converter_info'],
                                  queued_json_payload['converter_warnings'],
                                  queued_json_payload['converter_errors'],
                                  our_temp_dir)
    unzip_dir, converter_log = ccc.do_post_processing()
    final_build_log = merge_results_logs(build_log, converter_log, converter_flag=True)

    if final_build_log['errors']:
        if final_build_log['warnings']:
            # print(f"Had {len(final_build_log['errors'])} errors")
            # print(f"Had {len(final_build_log['warnings'])} warnings")
            # Prepend the errors to the warnings so they display on Door43.org
            final_build_log['warnings'] = final_build_log['errors'] + final_build_log['warnings']
            # print(f"Now have {len(final_build_log['warnings'])} warnings")
        final_build_log['status'] = 'errors'
    elif final_build_log['warnings']:
        print(f"Had {len(final_build_log['warnings']):,} warnings")
        final_build_log['status'] = 'warnings'
    else:
        final_build_log['status'] = 'success'
    if final_build_log['warnings']:
        final_build_log['warnings'].append(f"{len(final_build_log['warnings']):,} total preprocessor and linter warnings")
    final_build_log['success'] = queued_json_payload['converter_success']
    final_build_log['ended_at'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    update_project_file(final_build_log, our_temp_dir)
    # NOTE: The following is disabled coz it's done (again) later by the deployer
    # upload_build_log(final_build_log, 'build_log.json', output_dir, url_part2, cache_time=600)

    if unzip_dir is None:
        AppSettings.logger.critical("Unable to deploy because file download failed previously")
        deployed = False
    else:
        # Now deploy the new pages (was previously a separate AWS Lambda call)
        AppSettings.logger.info(f"Deploying to the website (convert status='{final_build_log['status']}')…")
        deployer = ProjectDeployer(unzip_dir, our_temp_dir)
        deployer.deploy_revision_to_door43(final_build_log) # Does templating and uploading
        deployed = True

    if prefix and debug_mode_flag:
        AppSettings.logger.debug(f"Temp folder '{our_temp_dir}' has been left on disk for debugging!")
    else:
        remove_tree(our_temp_dir)

    # Finishing off
    str_final_build_log = str(final_build_log)
    str_final_build_log_adjusted = str_final_build_log if len(str_final_build_log)<1500 \
                            else f'{str_final_build_log[:1000]} …… {str_final_build_log[-500:]}'
    AppSettings.logger.info(f"Door43-Job-Handler process_callback_job() for {job_descriptive_name} is finishing with {str_final_build_log_adjusted}")
    if 'echoed_from_production' in matched_job_dict and matched_job_dict['echoed_from_production']:
        AppSettings.logger.info("This job was ECHOED FROM PRODUCTION (for dev- chain testing)!")
        AppSettings.logger.info("  (Use https://git.door43.org/tx-manager-test-data/echo_prodn_to_dev_off/settings/hooks/44079 to turn that off.)")
    if deployed:
        AppSettings.logger.info(f"{'Should become available' if final_build_log['success'] is True or final_build_log['success']=='True' or final_build_log['status'] in ('success', 'warnings') else 'Would be'}"
                               f" at https://{AppSettings.door43_bucket_name.replace('dev-door43','dev.door43')}/{url_part2}/")
        if final_build_log['success'] is True or final_build_log['success']=='True' or final_build_log['status'] in ('success', 'warnings'):
            AppSettings.logger.debug(f"  or uncached at http://{AppSettings.door43_bucket_name}.s3-website-us-west-2.amazonaws.com/{url_part2}/")
    return job_descriptive_name, matched_job_dict['door43_webhook_received_at']
#end of process_callback_job function



def job(queued_json_payload:Dict[str,Any]) -> None:
    """
    This function is called by the rq package to process a job in the queue(s).
        (Don't rename this function.)

    The job is removed from the queue before the job is started,
        but if the job throws an exception or times out (timeout specified in enqueue process)
            then the job gets added to the 'failed' queue.
    """
    AppSettings.logger.info("Door43-Job-Handler received a callback" + (" (in debug mode)" if debug_mode_flag else ""))
    start_time = time.time()
    stats_client.incr(f'{callback_stats_prefix}.jobs.attempted')

    current_job = get_current_job()
    #print(f"Current job: {current_job}") # Mostly just displays the job number and payload
    #print("id",current_job.id) # Displays job number
    #print("origin",current_job.origin) # Displays queue name
    #print("meta",current_job.meta) # Empty dict

    # AppSettings.logger.info(f"Updating queue statistics…")
    our_queue= Queue(callback_queue_name, connection=current_job.connection)
    len_our_queue = len(our_queue) # Should normally sit at zero here
    # AppSettings.logger.debug(f"Queue '{callback_queue_name}' length={len_our_queue}")
    stats_client.gauge(f'"{door43_stats_prefix}.enqueue-job.callback.queue.length.current', len_our_queue)
    AppSettings.logger.info(f"Updated stats for '{door43_stats_prefix}.enqueue-job.callback.queue.length.current' to {len_our_queue}")

    #print(f"Got a job from {current_job.origin} queue: {queued_json_payload}")
    #print(f"\nGot job {current_job.id} from {current_job.origin} queue")
    #queue_prefix = 'dev-' if current_job.origin.startswith('dev-') else ''
    #assert queue_prefix == prefix
    try:
        job_descriptive_name, door43_webhook_received_at = \
                process_callback_job(prefix, queued_json_payload, current_job.connection)
    except Exception as e:
        # Catch most exceptions here so we can log them to CloudWatch
        prefixed_name = f"{prefix}Door43_Callback"
        AppSettings.logger.critical(f"{prefixed_name} threw an exception while processing: {queued_json_payload}")
        AppSettings.logger.critical(f"{e}: {traceback.format_exc()}")
        AppSettings.close_logger() # Ensure queued logs are uploaded to AWS CloudWatch
        # Now attempt to log it to an additional, separate FAILED log
        import logging
        from boto3 import Session
        from watchtower import CloudWatchLogHandler
        logger2 = logging.getLogger(prefixed_name)
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
                                                    stream_name=prefixed_name)
        logger2.addHandler(failure_watchtower_log_handler)
        logger2.setLevel(logging.DEBUG)
        logger2.info(f"Logging to AWS CloudWatch group '{log_group_name}' using key '…{aws_access_key_id[-2:]}'.")
        logger2.critical(f"{prefixed_name} threw an exception while processing: {queued_json_payload}")
        logger2.critical(f"{e}: {traceback.format_exc()}")
        failure_watchtower_log_handler.close()
        # NOTE: following line removed as stats recording used too much disk space
        # stats_client.gauge(user_projects_invoked_string, 1) # Mark as 'failed'
        stats_client.gauge(project_types_invoked_string, 1) # Mark as 'failed'
        raise e # We raise the exception again so it goes into the failed queue

    elapsed_milliseconds = round((time.time() - start_time) * 1000)
    stats_client.timing(f'{callback_stats_prefix}.job.duration', elapsed_milliseconds)
    if elapsed_milliseconds < 2000:
        AppSettings.logger.info(f"{prefix}Door43 callback handling for {job_descriptive_name} completed in {elapsed_milliseconds:,} milliseconds.")
    else:
        AppSettings.logger.info(f"{prefix}Door43 callback handling for {job_descriptive_name} completed in {round(time.time() - start_time)} seconds.")

    # Calculate total elapsed time for the job
    total_elapsed_time = datetime.utcnow() - \
                         datetime.strptime(door43_webhook_received_at,
                                           '%Y-%m-%dT%H:%M:%SZ')
    AppSettings.logger.info(f"{prefix}Door43 total job for {job_descriptive_name} completed in {round(total_elapsed_time.total_seconds())} seconds.")
    stats_client.timing(f'{job_handler_stats_prefix}.total.job.duration', round(total_elapsed_time.total_seconds() * 1000))

    # NOTE: following line removed as stats recording used too much disk space
    # stats_client.gauge(user_projects_invoked_string, 0) # Mark as 'succeeded'
    stats_client.gauge(project_types_invoked_string, 0) # Mark as 'succeeded'
    stats_client.incr(f'{callback_stats_prefix}.jobs.succeeded')
    AppSettings.close_logger() # Ensure queued logs are uploaded to AWS CloudWatch
# end of job function

# end of callback.py for door43_enqueue_job
