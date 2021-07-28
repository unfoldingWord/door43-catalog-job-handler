import os.path
import shutil
import tempfile
import unittest
import zipfile

from general_tools import file_utils


class FileUtilsTests(unittest.TestCase):

    def setUp(self):
        """Runs before each test."""
        self.tmp_dir = ""
        self.tmp_dir1 = ""
        self.tmp_dir2 = ""

    def tearDown(self):
        """
        Runs after each test.

        Delete temp dirs
        """
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        shutil.rmtree(self.tmp_dir1, ignore_errors=True)
        shutil.rmtree(self.tmp_dir2, ignore_errors=True)

    def test_unzip(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='Door43_test_file_utils_')
        zip_file = os.path.join(self.tmp_dir, 'foo.zip')

        _, self.tmp_file = tempfile.mkstemp(prefix='Door43_test_')
        with open(self.tmp_file, "w") as tmpf:
            tmpf.write("hello world")

        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.write(self.tmp_file, os.path.basename(self.tmp_file))

        file_utils.unzip(zip_file, self.tmp_dir)
        with open(os.path.join(self.tmp_dir, os.path.basename(self.tmp_file))) as outf:
            self.assertEqual(outf.read(), "hello world")

    def test_add_contents_to_zip(self):
        self.tmp_dir1 = tempfile.mkdtemp(prefix='Door43_test_file_utils_')
        zip_file = os.path.join(self.tmp_dir1, 'foo.zip')

        self.tmp_dir2 = tempfile.mkdtemp(prefix='Door43_test_file_utils_')
        tmp_file = os.path.join(self.tmp_dir2, 'foo.txt')
        with open(tmp_file, "w") as tmpf:
            tmpf.write("hello world")

        with zipfile.ZipFile(zip_file, "w"):
            pass  # create empty archive
        file_utils.add_contents_to_zip(zip_file, self.tmp_dir2)

        with zipfile.ZipFile(zip_file, "r") as zf:
            with zf.open(os.path.relpath(tmp_file, self.tmp_dir2), "r") as f:
                self.assertEqual(f.read().decode("ascii"), "hello world")

    def test_add_file_to_zip(self):
        self.tmp_dir1 = tempfile.mkdtemp(prefix='Door43_test_file_utils_')
        zip_file = os.path.join(self.tmp_dir1, 'foo.zip')

        _, self.tmp_file = tempfile.mkstemp(prefix='Door43_test_')
        with open(self.tmp_file, "w") as tmpf:
            tmpf.write("hello world")

        with zipfile.ZipFile(zip_file, "w"):
            pass  # create empty archive
        file_utils.add_file_to_zip(zip_file, self.tmp_file, os.path.basename(self.tmp_file))

        with zipfile.ZipFile(zip_file, "r") as zf:
            with zf.open(os.path.basename(self.tmp_file), "r") as f:
                self.assertEqual(f.read().decode("ascii"), "hello world")

    @staticmethod
    def paths_equal(path1, path2):
        return os.path.normpath(path1) == os.path.normpath(path2)
