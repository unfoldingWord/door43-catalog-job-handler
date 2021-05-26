from typing import Dict, List, Any
import os
import tempfile
import time
from datetime import datetime

from rq_settings import prefix, debug_mode_flag
from app_settings.app_settings import AppSettings
from general_tools.file_utils import write_file, remove_tree



class ClientLinterCallback:

    def __init__(self, job_dict:Dict[str,Any], identifier:str, success:bool,
                        info:List[str], warnings:List[str], errors:List[str]) -> None:
        """
        :param identifier: either
                    job_id/part_count/part_id/book if multi-part job
                        or
                    job_id if single job
        :param bool success:
        :param list info:
        :param list warnings:
        :param list errors:
        :param s3_results_key: format
                    u/user/repo/commid_id if single part
                        or
                    u/user/repo/commid_id/part_id if multi-part job
        """
        w = f'({len(warnings):,})' if warnings and len(warnings)>10 else str(warnings)
        e = f'({len(errors):,})' if errors and len(errors)>10 else str(errors)
        AppSettings.logger.debug(f"ClientLinterCallback.__init__({job_dict}, id={identifier}, s={success}, i={info}, w={w}, e={e})…")
        self.job_dict = job_dict
        self.identifier = identifier
        self.success = success
        self.log = info if info else []
        self.warnings = warnings if warnings else []
        self.errors = errors if errors else []
        self.all_parts_completed = False
        self.multipart = False

        # self.temp_dir = tempfile.mkdtemp(suffix='',
        #                     prefix='Door43_linter_callback_' + datetime.utcnow().strftime('%Y-%m-%d_%H:%M:%S_'))
        # self.s3_results_key = s3_results_key
        self.job = None

    def do_post_processing(self) -> Dict[str,Any]:
        AppSettings.logger.debug(f"ClientLinterCallback.do_post_processing()…")
        if not self.identifier:
            error = 'No identifier found'
            AppSettings.logger.error(error)
            raise Exception(error)

        # if not self.s3_results_key:
        #     error = f"No s3_results_key found for identifier = {self.identifier}"
        #     AppSettings.logger.error(error)
        #     raise Exception(error)

        id_parts = self.identifier.split('/')
        self.multipart = len(id_parts) > 3
        if self.multipart:
            raise Exception("Unsupported")
            # NOTE: Disabled 4Mar2019 coz unused
            # part_count, part_id, book = id_parts[1:4]
            # AppSettings.logger.debug('Multiple project, part {0} of {1}, linted book {2}'.
            #                  format(part_id, part_count, book))
            # s3__master_results_key = '/'.join(self.s3_results_key.split('/')[:-1])
        else:
            AppSettings.logger.debug('Single project')
            # NOTE: Disabled 4Mar2019 coz unused
            # s3__master_results_key = self.s3_results_key

        build_log = {
            'identifier': self.identifier,
            'success': self.success,
            'multipart_project': self.multipart,
            'log': self.log,
            'warnings': self.warnings,
            'errors': self.errors,
            # 's3_commit_key': self.s3_results_key
        }

        if not self.success:
            msg = "Linter failed for identifier: " + self.identifier
            build_log['warnings'].append(msg)
            AppSettings.logger.error(msg)
        else:
            AppSettings.logger.debug(f"Linter {self.identifier} had success with"
                                        f" {len(self.warnings)} warnings: {', '.join(self.warnings[:5])} …")

        has_warnings = len(build_log['warnings']) > 0
        if has_warnings:
            msg = f"Linter {self.identifier} has Warnings!"
            build_log['log'].append(msg)
        else:
            msg = f"Linter {self.identifier} completed with no warnings"
            build_log['log'].append(msg)

        # NOTE: Do we need this -- disabled 25Feb2019
        # ClientLinterCallback.upload_build_log(build_log, 'lint_log.json', self.temp_dir, self.s3_results_key)

        # NOTE: Do we need this -- disabled 4Mar2019 since linting is always done first
        # results = ClientLinterCallback.deploy_if_conversion_finished(s3__master_results_key, self.identifier)
        # if results:
        #     self.all_parts_completed = True
        #     build_log = results

        # if prefix and debug_mode_flag:
        #     AppSettings.logger.debug(f"Temp folder '{self.temp_dir}' has been left on disk for debugging!")
        # else:
        #     remove_tree(self.temp_dir)  # cleanup
        AppSettings.db_close()
        return build_log
    # end of do_post_processing()
# end of ClientLinterCallback class