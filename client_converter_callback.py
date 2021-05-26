from typing import Dict, List, Any, Optional, Tuple
import os
import tempfile
from datetime import datetime

from rq_settings import prefix, debug_mode_flag
from app_settings.app_settings import AppSettings
from general_tools.file_utils import unzip, write_file, remove_tree, remove_file
from general_tools.url_utils import download_file



class LocalJob:
    """
    This is a temporary class to replace the basic functionality of TxJob
    """
    def __init__(self, job_dict:Dict[str,Any]) -> None:
        # self.job_dict = job_dict
        self.job_id = job_dict['job_id']
        self.convert_module = job_dict['convert_module']
        self.repo_owner_username = job_dict['repo_owner_username']
        self.repo_name = job_dict['repo_name']
        self.commit_id = job_dict['commit_id']
        self.output = job_dict['output']
        self.started_at = datetime.strptime(job_dict['started_at'], '%Y-%m-%dT%H:%M:%SZ')
        self.log, self.warnings, self.errors = [], [], []

    def log_message(self, msg:str) -> None:
        self.log.append(msg)
        AppSettings.logger.debug(msg) # DEBUG coz we don't need all these displayed in production mode

    def warnings_message(self, msg:str) -> None:
        self.warnings.append(msg)
        AppSettings.logger.warning(msg)

    def error_message(self, msg:str) -> None:
        self.errors.append(msg)
        AppSettings.logger.error(msg)



class ClientConverterCallback:

    def __init__(self, job_dict:Dict[str,Any], identifier:str, success:bool,
                        info:List[str], warnings:List[str], errors:List[str], output_dir:str) -> None:
        """
        :param string identifier:
        :param bool success:
        :param list info:
        :param list warnings:
        :param list errors:
        """
        w = f'({len(warnings):,})' if warnings and len(warnings)>10 else str(warnings)
        e = f'({len(errors):,})' if errors and len(errors)>10 else str(errors)
        AppSettings.logger.debug(f"ClientConverterCallback.__init__({job_dict}, id={identifier}, s={success}, i={info}, w={w}, e={e}, od={output_dir})…")
        self.job = LocalJob(job_dict)
        self.identifier = identifier
        self.success = success
        self.log = info if info else []
        self.warnings = warnings if warnings else []
        self.errors = errors if errors else []
        self.temp_dir = output_dir
        self.all_parts_completed = False
    # end of ClientConverterCallback.__init__ function


    def do_post_processing(self) -> Tuple[Optional[str], Dict[str,Any]]:
        AppSettings.logger.debug(f"ClientConverterCallback.do_post_processing()…")
        self.job.ended_at = datetime.utcnow()
        self.job.success = self.success
        for message in self.log:
            self.job.log_message(message)
        for message in self.warnings:
            self.job.warnings_message(message)
        for message in self.errors:
            self.job.error_message(message)
        if self.errors:
            self.job.log_message(f"{self.job.convert_module} function returned with errors.")
        elif self.warnings:
            self.job.log_message(f"{self.job.convert_module} function returned with warnings.")
        else:
            self.job.log_message(f"{self.job.convert_module} function returned successfully.")

        if not self.success or self.job.errors:
            self.job.success = False
            self.job.status = 'failed'
            message = "Conversion failed"
            AppSettings.logger.debug(f"Conversion failed, success: {self.success}, errors: {self.job.errors}")
        elif self.job.warnings:
            self.job.success = True
            self.job.status = 'warnings'
            message = "Conversion successful with warnings."
        else:
            self.job.success = True
            self.job.status = 'success'
            message = "Conversion successful."

        self.job.message = message
        self.job.log_message(message)
        self.job.log_message(f"Finished job {self.job.job_id} at {self.job.ended_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")

        s3_commit_key = f'u/{self.job.repo_owner_username}/{self.job.repo_name}/{self.job.commit_id}'
        # AppSettings.logger.debug(f"Callback for commit = '{s3_commit_key}'")
        upload_key = s3_commit_key

        # Download the ZIP file of the converted files
        converted_zip_url = self.job.output
        converted_zip_file = os.path.join(self.temp_dir, converted_zip_url.rpartition('/')[2])
        remove_file(converted_zip_file)  # make sure old file not present
        download_success = True
        AppSettings.logger.debug(f"Downloading converted zip file from {converted_zip_url} …")
        try:
            download_file(converted_zip_url, converted_zip_file)
        except:
            download_success = False  # if multiple project we note fail and move on
            # if not multiple_project:
            # if prefix and debug_mode_flag:
            #     AppSettings.logger.debug(f"Temp folder '{self.temp_dir}' has been left on disk for debugging!")
            # else:
            #     remove_tree(self.temp_dir)  # cleanup
            if self.job.errors is None:
                self.job.errors = []
            message = f"Missing converted file: {converted_zip_url}"
            AppSettings.logger.debug(message)
            if not self.job.errors or not self.job.errors[0].startswith("No converter "):
                # Missing file error is irrelevant if no conversion was attempted
                self.job.errors.append(message)
        finally:
            AppSettings.logger.debug(f"Download finished, success={download_success}.")

        # self.job.update()

        if download_success:
            # Unzip the archive
            unzip_dirpath = self.unzip_converted_files(converted_zip_file)

            # Upload all files to the cdn_bucket with the key of <user>/<repo_name>/<commit> of the repo
            # This is required for the print function to work
            self.upload_converted_files_to_CDN(upload_key, unzip_dirpath)
        else:
            unzip_dirpath = None # So we have something to return (fail later -- is that an advantage?)

        # TODO: Do we really need this now?
        # Now download the existing build_log.json file, update it and upload it back to S3 as convert_log
        # NOTE: Do we need this -- disabled 25Feb2019
        # build_log_json = self.update_convert_log(s3_commit_key)
        # self.cdn_upload_contents({}, s3_commit_key + '/finished')  # flag finished
        converter_build_log = self.make_our_build_log()
        # print("Got ConPP converter_build_log", converter_build_log)

        # NOTE: Disabled 4Mar2019 coz moved to callback.py
        # results = ClientLinterCallback.deploy_if_conversion_finished(s3_commit_key, self.identifier)
        # if results:
        #     self.all_parts_completed = True
        #     build_log_json = results

        # if prefix and debug_mode_flag:
        #     AppSettings.logger.debug(f"Temp folder '{self.temp_dir}' has been left on disk for debugging!")
        # else:
        #     remove_tree(self.temp_dir)  # cleanup
        return unzip_dirpath, converter_build_log
    # end of ClientConverterCallback.do_post_processing()


    def unzip_converted_files(self, converted_zip_filepath:str) -> str:
        AppSettings.logger.debug(f"ClientConverterCallback.unzip_converted_files({converted_zip_filepath})…")
        unzip_dirpath = tempfile.mkdtemp(prefix='unzip_', dir=self.temp_dir)
        try:
            AppSettings.logger.debug(f"Unzipping {converted_zip_filepath} …")
            unzip(converted_zip_filepath, unzip_dirpath)
        finally:
            AppSettings.logger.debug("Unzip finished.")
        return unzip_dirpath
    # end of ClientConverterCallback.unzip_converted_files function


    @staticmethod
    def upload_converted_files_to_CDN(s3_commit_key:str, unzip_dir:str) -> None:
        """
        Uploads the converted (but not templated) files to the cdn.door43.org bucket

        NOTE: These are used from there by the Print button/function.
        """
        AppSettings.logger.info(f"Uploading converted files from {unzip_dir} to {prefix}CDN {s3_commit_key} …")
        for root, _dirs, files in os.walk(unzip_dir):
            for filename in sorted(files):
                filepath = os.path.join(root, filename)
                key = s3_commit_key + filepath.replace(unzip_dir, '')
                AppSettings.logger.debug(f"Uploading {filename} to {prefix}CDN {key} …")
                AppSettings.cdn_s3_handler().upload_file(filepath, key, cache_time=0)
    # end of ClientConverterCallback.upload_converted_files_to_CDN function


    # NOTE: Do we need this -- disabled 25Feb2019
    # def update_convert_log(self, s3_base_key, part=''):
    #     build_log_json = self.get_build_log(s3_base_key, part)
    #     self.upload_convert_log(build_log_json, s3_base_key, part)
    #     return build_log_json


    def make_our_build_log(self) -> Dict[str,Any]:
        AppSettings.logger.debug(f"ClientConverterCallback.make_our_build_log()…")
        build_log_dict = {}
        build_log_dict['started_at'] = self.job.started_at.strftime('%Y-%m-%dT%H:%M:%SZ') \
                                        if self.job.started_at else None
        build_log_dict['ended_at'] = self.job.ended_at.strftime('%Y-%m-%dT%H:%M:%SZ') \
                                        if self.job.ended_at else None
        build_log_dict['success'] = self.job.success
        build_log_dict['status'] = self.job.status
        build_log_dict['message'] = self.job.message
        build_log_dict['log'] = self.job.log if self.job.log else []
        build_log_dict['warnings'] = self.job.warnings if self.job.warnings else []
        build_log_dict['errors'] = self.job.errors if self.job.errors else []
        return build_log_dict
    # end of ClientConverterCallback.make_our_build_log()


    # NOTE: Do we need this -- disabled 25Feb2019
    # def upload_convert_log(self, build_log_json, s3_base_key, part=''):
    #     if self.job.started_at:
    #         build_log_json['started_at'] = self.job.started_at.strftime('%Y-%m-%dT%H:%M:%SZ')
    #     else:
    #         build_log_json['started_at'] = None
    #     if self.job.ended_at:
    #         build_log_json['ended_at'] = self.job.ended_at.strftime('%Y-%m-%dT%H:%M:%SZ')
    #     else:
    #         build_log_json['ended_at'] = None
    #     build_log_json['success'] = self.job.success
    #     build_log_json['status'] = self.job.status
    #     build_log_json['message'] = self.job.message
    #     if self.job.log:
    #         build_log_json['log'] = self.job.log
    #     else:
    #         build_log_json['log'] = []
    #     if self.job.warnings:
    #         build_log_json['warnings'] = self.job.warnings
    #     else:
    #         build_log_json['warnings'] = []
    #     if self.job.errors:
    #         build_log_json['errors'] = self.job.errors
    #     else:
    #         build_log_json['errors'] = []
    #     build_log_key = self.get_build_log_key(s3_base_key, part, name='convert_log.json')
    #     AppSettings.logger.debug(f"Uploading build log to S3:{AppSettings.cdn_bucket_name}/{build_log_key} …")
    #     # AppSettings.logger.debug('build_log contents: ' + json.dumps(build_log_json))
    #     self.cdn_upload_contents(build_log_json, build_log_key)
    #     return build_log_json

    # def cdn_upload_contents(self, contents:str, key:str) -> None:
    #     AppSettings.logger.debug(f"ClientConverterCallback.cdn_upload_contents({contents}, {key})…")
    #     file_name = os.path.join(self.temp_dir, 'contents.json')
    #     write_file(file_name, contents)
    #     AppSettings.logger.debug(f"Uploading file to S3:{AppSettings.cdn_bucket_name}/{key} …")
    #     AppSettings.cdn_s3_handler().upload_file(file_name, key, cache_time=0)

    # def get_build_log(self, s3_base_key:str) -> Dict[str,Any]:
    #     AppSettings.logger.debug(f"ClientConverterCallback.get_build_log({s3_base_key})…")
    #     build_log_key = f'{s3_base_key}/build_log.json'
    #     # AppSettings.logger.debug('Reading build log from ' + build_log_key)
    #     build_log_json = AppSettings.cdn_s3_handler().get_json(build_log_key)
    #     # AppSettings.logger.debug('build_log contents: ' + json.dumps(build_log_json))
    #     return build_log_json
# end of ClientConverterCallback class
