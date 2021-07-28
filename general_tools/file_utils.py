import os
import shutil
import zipfile
from typing import Optional


def unzip(source_file: str, destination_dir: str) -> None:
    """
    Unzips <source_file> into <destination_dir>.

    :param str source_file: The name of the file to read
    :param str destination_dir: The name of the directory to write the unzipped files

    NOTE: This is UNSAFE if the zipfile comes from an untrusted source
            as it may contain absolute paths outside of the desired folder.
        The zipfile should really be examined first.
    """
    with zipfile.ZipFile(source_file) as zf:
        zf.extractall(destination_dir)


def add_contents_to_zip(zip_file: str, path: str, include_root: bool = False) -> None:
    """
    Zip the contents of <path> into <zip_file>.

    :param str zip_file: The file name of the zip file
    :param str path: Full path of the directory to zip up
    :param bool include_root: If true, the zip file will start with the directory of the path parameter
    """
    path = path.rstrip(os.path.sep)
    if include_root:
        path_start_index = len(os.path.dirname(path)) + 1
    else:
        path_start_index = len(path) + 1
    with zipfile.ZipFile(zip_file, 'a') as zf:
        for root, _dirs, files in os.walk(path):
            for f in files:
                file_path = os.path.join(root, f)
                zf.write(file_path, file_path[path_start_index:])


def add_file_to_zip(zip_file: str, file_name: str, arc_name: Optional[str] = None,
                    compress_type: Optional[str] = None) -> None:
    """
    Zip <file_name> into <zip_file> as <arc_name>.

    :param str zip_file: The file name of the zip file
    :param str file_name: The name of the file to add, including the path
    :param str arc_name: The new name, with directories, of the file, the same as filename if not given
    :param str compress_type:
    """
    with zipfile.ZipFile(zip_file, 'a') as zf:
        zf.write(file_name, arc_name, compress_type)


def empty_folder(folder_path: str, only_prefix: Optional[str] = None) -> None:
    for filename in os.listdir(folder_path):
        if not only_prefix or filename.startswith(only_prefix):
            filepath = os.path.join(folder_path, filename)
            try:
                shutil.rmtree(filepath)
            except OSError:
                os.remove(filepath)
