import os
import json
import re

from app_settings.app_settings import AppSettings


def txt2md(rootdir='.'):
    """
    Converts JSON txt files to markdown files in the given folder
        and deletes the original files
    """
    processedCount = 0
    for dir, _subdir, files in os.walk(rootdir):
        for fname in files:
            filepath = os.path.join(dir, fname)

            if os.path.splitext(fname)[1] == '.txt':
                with open(filepath, 'r') as data_file:
                    # if content of the file starts from the valid json character
                    # then it's a json file
                    content = data_file.read()

                    if re.match(r'^\[|^\{', content):
                        try:
                            data = json.loads(content)
                            md = ''
                            for elm in data:
                                if 'title' in elm and 'body' in elm:
                                    md += f"# {elm['title']}\n\n" \
                                        + f"{elm['body']}\n\n"

                            md_filepath = re.sub(r'\.txt$', '.md', filepath)
                            with open(md_filepath, 'w') as md_file:
                                md_file.write(md)

                            processedCount += 1
                        except BaseException as e:
                            AppSettings.logger.debug(f"Error: {e}")

                if os.path.isfile(filepath):
                    os.remove(filepath)

    return processedCount
# end of txt2md


# def txt2usfm(rootdir='.'):
#     """
#     Renames USFM txt files to usfm files in the given folder.
#     """
#     processedCount = 0
#     for dir, _subdir, files in os.walk(rootdir):
#         for fname in files:
#             filepath = os.path.join(dir, fname)

#             if os.path.splitext(fname)[1] == '.txt':
#                 with open(filepath, 'r') as data_file:
#                     # if content of the file starts from the valid usfm chapter or verse tag
#                     # then it's a usfm file
#                     if re.match(r'^[\s]*\\c|^[\s]*\\v', data_file.read()):
#                         processedCount += 1

#                 if processedCount and os.path.isfile(filepath):
#                     usfm_filepath = re.sub(r'\.txt$', '.usfm', filepath)
#                     os.rename(filepath, usfm_filepath)

#     return processedCount
# # end of txt2usfm
