# Python imports
from typing import Dict, List, Set, Tuple, Any, Optional
import os
import re
import json
import tempfile
from glob import glob
from shutil import copy, copytree
from urllib.request import urlopen
from urllib.error import HTTPError
import unicodedata

# Local imports
from rq_settings import prefix, debug_mode_flag
from app_settings.app_settings import AppSettings
from door43_tools.bible_books import BOOK_NUMBERS, BOOK_NAMES, BOOK_CHAPTER_VERSES
from general_tools.file_utils import write_file, read_file, make_dir, unzip, remove_file, remove_tree
from general_tools.url_utils import get_url, download_file
from resource_container.ResourceContainer import RC
from preprocessors.converters import txt2md


USFM_BOOK_IDENTIFIERS:Set[str] = {x.upper() for x in BOOK_NAMES.keys()}



def do_preprocess(repo_subject:str, repo_owner:str, commit_url:str, rc:RC,
                                        repo_dir:str, output_dir:str) -> Tuple[int, List[str]]:
    if repo_subject == 'Open_Bible_Stories':
        AppSettings.logger.info(f"do_preprocess: using ObsPreprocessor for '{repo_subject}'…")
        preprocessor = ObsPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject in ('OBS_Study_Notes','OBS_Study_Questions',
                            'OBS_Translation_Notes','OBS_Translation_Questions'):
        AppSettings.logger.info(f"do_preprocess: using ObsNotesPreprocessor for '{repo_subject}'…")
        preprocessor = ObsNotesPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject in ('Bible','Aligned_Bible', 'Greek_New_Testament','Hebrew_Old_Testament'):
        AppSettings.logger.info(f"do_preprocess: using BiblePreprocessor for '{repo_subject}'…")
        preprocessor = BiblePreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject == 'Translation_Academy':
        AppSettings.logger.info(f"do_preprocess: using TaPreprocessor for '{repo_subject}'…")
        preprocessor = TaPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject == 'Translation_Questions':
        AppSettings.logger.info(f"do_preprocess: using TqPreprocessor for '{repo_subject}'…")
        preprocessor = TqPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject == 'Translation_Words':
        AppSettings.logger.info(f"do_preprocess: using TwPreprocessor for '{repo_subject}'…")
        preprocessor = TwPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject in ('Translation_Notes', 'TSV_Translation_Notes'):
        AppSettings.logger.info(f"do_preprocess: using TnPreprocessor for '{repo_subject}'…")
        preprocessor = TnPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    elif repo_subject in ('Greek_Lexicon','Hebrew-Aramaic_Lexicon'):
        AppSettings.logger.info(f"do_preprocess: using LexiconPreprocessor for '{repo_subject}'…")
        preprocessor = LexiconPreprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)
    else:
        AppSettings.logger.warning(f"do_preprocess: using generic Preprocessor for '{repo_subject}' resource: {rc.resource.identifier} …")
        preprocessor = Preprocessor(commit_url, rc, repo_owner, repo_dir, output_dir)

    # So now lets actually run our chosen preprocessor and do the work
    num_files_written, warnings_list = preprocessor.run()

    MAX_WARNINGS = 1000
    if len(warnings_list) > MAX_WARNINGS:  # sanity check so we don't overflow callback size limits
        new_warnings_list = warnings_list[:MAX_WARNINGS-11]
        new_warnings_list.append("…………………………")
        new_warnings_list.extend(warnings_list[-9:])
        msg = f"Preprocessor warnings reduced from {len(warnings_list):,} to {len(new_warnings_list):,}"
        AppSettings.logger.debug(f"Linter {msg}")
        new_warnings_list.append(msg)
        warnings_list = new_warnings_list

    return num_files_written, warnings_list
# end of do_preprocess()



class Preprocessor:
    # NOTE: Both of these lists are used for CASE-SENSITIVE comparisons
    ignoreDirectories = ['.apps', '.git', '.github', '00']
    ignoreFiles = ['.DS_Store', 'reference.txt', 'title.txt', 'LICENSE.md', 'README.md', 'README.rst']


    def __init__(self, commit_url:str, rc:RC, repo_owner:str, source_dir:str, output_dir:str) -> None:
        """
        :param URLString commit_url:    URL of this commit on DCS—used for fixing links
        :param RC rc:
        :param string source_dir:
        :param string output_dir:
        """
        self.commit_url = commit_url
        self.rc = rc
        self.repo_owner = repo_owner
        self.source_dir = source_dir  # Local directory
        self.output_dir = output_dir  # Local directory
        self.num_files_written = 0
        self.messages:List[str] = [] # { Messages only display if there's warnings or errors
        self.errors:List[str] = []   # { Errors float to the top of the list
        self.warnings:List[str] = [] # {    above warnings

        # Check that we had a manifest (or equivalent) file
        # found_manifest = False
        # for some_filename in ('manifest.yaml','manifest.json','package.json','project.json','meta.json',):
        #     if os.path.isfile(os.path.join(source_dir,some_filename)):
        #         found_manifest = True; break
        # if not found_manifest:
        if not self.rc.loadeded_manifest_file:
            self.warnings.append("Possible missing manifest file in project folder")

        # Write out the new manifest file based on the resource container
        write_file(os.path.join(self.output_dir, 'manifest.yaml'), self.rc.as_dict())


    def run(self) -> Tuple[int, List[str]]:
        """
        Default Preprocessor

        Case #1: Project path is a file, then we copy the file over to the output dir
        Case #2: It's a directory of files, so we copy them over to the output directory
        Case #3: The project path is multiple chapters, so we piece them together
        """
        AppSettings.logger.debug(f"Default preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        for idx, project in enumerate(self.rc.projects):
            project_path = os.path.join(self.source_dir, project.path)

            if os.path.isfile(project_path):
                # Case #1: Project path is a file, then we copy the file over to the output dir
                AppSettings.logger.debug(f"Default preprocessor case #1: Copying single file for '{project.identifier}' …")
                if project.identifier.lower() in BOOK_NUMBERS:
                    filename = f'{BOOK_NUMBERS[project.identifier.lower()]}-{project.identifier.upper()}.{self.rc.resource.file_ext}'
                else:
                    filename = f'{str(idx + 1).zfill(2)}-{project.identifier}.{self.rc.resource.file_ext}'
                copy(project_path, os.path.join(self.output_dir, filename))
                self.num_files_written += 1
            else:
                # Case #2: It's a directory of files, so we copy them over to the output directory
                AppSettings.logger.debug(f"Default preprocessor case #2: Copying files for '{project.identifier}' …")
                files = glob(os.path.join(project_path, f'*.{self.rc.resource.file_ext}'))
                if files:
                    for file_path in files:
                        output_file_path = os.path.join(self.output_dir, os.path.basename(file_path))
                        if os.path.isfile(file_path) and not os.path.exists(output_file_path) \
                                and os.path.basename(file_path) not in self.ignoreFiles:
                            copy(file_path, output_file_path)
                            self.num_files_written += 1
                else:
                    # Case #3: The project path is multiple chapters, so we piece them together
                    AppSettings.logger.debug(f"Default preprocessor case #3: piecing together chapters for '{project.identifier}' …")
                    chapters = self.rc.chapters(project.identifier)
                    AppSettings.logger.debug(f"Merging chapters in '{project.identifier}' …")
                    if chapters:
                        text = ''
                        for chapter in chapters:
                            text = self.mark_chapter(project.identifier, chapter, text)
                            for chunk in self.rc.chunks(project.identifier, chapter):
                                text = self.mark_chunk(project.identifier, chapter, chunk, text)
                                text += read_file(os.path.join(project_path, chapter, chunk))+"\n\n"
                        if project.identifier.lower() in BOOK_NUMBERS:
                            filename = f'{BOOK_NUMBERS[project.identifier.lower()]}-{project.identifier.upper()}.{self.rc.resource.file_ext}'
                        else:
                            filename = f'{str(idx+1).zfill(2)}-{project.identifier}.{self.rc.resource.file_ext}'
                        write_file(os.path.join(self.output_dir, filename), text)
                        self.num_files_written += 1
        if self.num_files_written == 0:
            AppSettings.logger.error(f"Default preprocessor didn't write any files")
            self.errors.append("No source files discovered")
        else:
            AppSettings.logger.debug(f"Default preprocessor wrote {self.num_files_written} files with {len(self.errors)} errors and {len(self.warnings)} warnings")
        AppSettings.logger.debug(f"Default preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of Preprocessor.run()


    def mark_chapter(self, ident:int, chapter:str, text:str) -> str:
        return text  # default does nothing to text


    def mark_chunk(self, ident:int, chapter:str, chunk:str, text:str) -> str:
        return text  # default does nothing to text


    def get_book_list(self):
        return None


    def check_and_clean_title(self, title_text:str, ref:str) -> str:
        """
        """
        if title_text.lstrip() != title_text:
            self.warnings.append(f"{ref}: Unexpected whitespace at beginning of {title_text!r}")
        if title_text.rstrip() != title_text:
            # We will ignore a single final newline
            if title_text[-1]=='\n' and title_text[:-1].rstrip() != title_text[:-1]:
                self.warnings.append(f"{ref}: Unexpected whitespace at end of {title_text!r}")
        title_text = title_text.strip()
        if '  ' in title_text:
            self.warnings.append(f"{ref}: Doubled spaces in '{title_text}'")

        if not title_text:
            self.warnings.append(f"{ref}: Missing title text")

        for char in '.[]:"':
            if char in title_text:
                self.warnings.append(f"{ref}: Unexpected '{char}' in '{title_text}'")

        self.check_punctuation_pairs(title_text, ref)
        return title_text
    # end of Preprocessor.check_and_clean_title function


    def check_punctuation_pairs(self, some_text:str, ref:str, allow_close_parenthesis_points=False) -> None:
        """
        Check matching number of pairs.

        If closing parenthesis is used for points, e.g., 1) This point.
            then set the optional flag.

        Copied here from linter.py 23Mar2020.
        """
        punctuation_pairs_to_check = (('(',')'), ('[',']'), ('{','}'), ('**_','_**'))

        found_any_paired_chars = False
        # found_mismatch = False
        for pairStart,pairEnd in punctuation_pairs_to_check:
            pairStartCount = some_text.count(pairStart)
            pairEndCount   = some_text.count(pairEnd)
            if pairStartCount or pairEndCount:
                found_any_paired_chars = True
            if pairStartCount > pairEndCount:
                self.warnings.append(f"{ref}: Possible missing closing '{pairEnd}' — found {pairStartCount:,} '{pairStart}' but {pairEndCount:,} '{pairEnd}'")
                # found_mismatch = True
            elif pairEndCount > pairStartCount:
                if allow_close_parenthesis_points:
                    # possible_points_list = re.findall(r'\s\d\) ', some_text)
                    # if possible_points_list: print("possible_points_list", possible_points_list)
                    possible_point_count = len(re.findall(r'\s\d\) ', some_text))
                    pairEndCount -= possible_point_count
                if pairEndCount > pairStartCount: # still
                    self.warnings.append(f"{ref}: Possible missing opening '{pairStart}' — found {pairStartCount:,} '{pairStart}' but {pairEndCount:,} '{pairEnd}'")
                    # found_mismatch = True
        if found_any_paired_chars: # and not found_mismatch:
            # Double-check the nesting
            lines = some_text.split('\n')
            nestingString = ''
            line_number = 1
            for ix, char in enumerate(some_text):
                if char in '({[':
                    nestingString += char
                elif char in ')}]':
                    if char == ')': wanted_start_char = '('
                    elif char == '}': wanted_start_char = '{'
                    elif char == ']': wanted_start_char = '['
                    if nestingString and nestingString[-1] == wanted_start_char:
                        nestingString = nestingString[:-1] # Close off successful match
                    else: # not the closing that we expected
                        if char==')' \
                        and ix>0 and some_text[ix-1].isdigit() \
                        and ix<len(some_text)-1 and some_text[ix+1] in ' \t':
                            # This could be part of a list like 1) ... 2) ...
                            pass # Just ignore this—at least they'll still get the above mismatched count message
                        else:
                            locateString = f" after recent '{nestingString[-1]}'" if nestingString else ''
                            self.warnings.append(f"{ref} line {line_number:,}: Possible nesting error—found unexpected '{char}'{locateString} near {lines[line_number-1]}")
                elif char == '\n':
                    line_number += 1
            if nestingString: # handle left-overs
                reformatted_nesting_string = "'" + "', '".join(nestingString) + "'"
                self.warnings.append(f"{ref}: Seem to have the following unclosed field(s): {reformatted_nesting_string}")
        # NOTE: Notifying all those is probably overkill,
        #  but never mind (it might help detect multiple errors)

        # These are markdown specific checks, but hopefully shouldn't hurt to be done for all strings
        # They don't seem to be picked up by the markdown linter libraries for some reason.
        for field,regex in ( # Put longest ones first
                        # Seems that the fancy ones (commented out) don't find occurrences at the start (or end?) of the text
                        ('___', r'___'),
                        # ('___', r'[^_]___[^_]'), # three underlines
                        ('***', r'\*\*\*'),
                        # ('***', r'[^\*]\*\*\*[^\*]'), # three asterisks
                        ('__', r'__'),
                        # ('__', r'[^_]__[^_]'), # two underlines
                        ('**', r'\*\*'),
                        # ('**', r'[^\*]\*\*[^\*]'), # two asterisks
                    ):
            count = len(re.findall(regex, some_text)) # Finds all NON-OVERLAPPING matches
            if count:
                # print(f"check_punctuation_pairs found {count} of '{field}' at {ref} in '{some_text}'")
                if (count % 2) != 0:
                    # print(f"{ref}: Seem to have have mismatched '{field}' pairs in '{some_text}'")
                    content_snippet = some_text if len(some_text) < 85 \
                                        else f"{some_text[:40]} …… {some_text[-40:]}"
                    self.warnings.append(f"{ref}: Seem to have have mismatched '{field}' pairs in '{content_snippet}'")
                    break # Only want one warning per text
    # end of Preprocessor.check_punctuation_pairs function
# end of Preprocessor class



class ObsPreprocessor(Preprocessor):
    # def __init__(self, *args, **kwargs) -> None:
    #     super(ObsPreprocessor, self).__init__(*args, **kwargs)

    @staticmethod
    def get_chapters(project_path:str) -> List[Dict[str,Any]]:
        chapters:List[Dict[str,Any]] = []
        for chapter in sorted(os.listdir(project_path)):
            if os.path.isdir(os.path.join(project_path, chapter)) and chapter not in ObsPreprocessor.ignoreDirectories:
                chapters.append({
                    'id': chapter,
                    'title': ObsPreprocessor.get_chapter_title(project_path, chapter),
                    'reference': ObsPreprocessor.get_chapter_reference(project_path, chapter),
                    'frames': ObsPreprocessor.get_chapter_frames(project_path, chapter)
                })
        return chapters


    @staticmethod
    def get_chapter_title(project_path:str, chapter) -> str:
        """
        Get a chapter title.
        if the title file does not exist, it will hand back the number with a period only.
        """
        title_filepath = os.path.join(project_path, chapter, 'title.txt')
        if os.path.exists(title_filepath):
            # title = self.check_and_clean_title(read_file(title_filepath), f'{chapter}/title/txt')
            title = read_file(title_filepath).strip()
        else:
            title = chapter.lstrip('0') + '. '
        return title


    @staticmethod
    def get_chapter_reference(project_path:str, chapter:str) -> str:
        """Get the chapters reference text"""
        reference_file = os.path.join(project_path, chapter, 'reference.txt')
        reference = ''
        if os.path.exists(reference_file):
            contents = read_file(reference_file)
            reference = contents.strip()
        return reference


    @staticmethod
    def get_chapter_frames(project_path:str, chapter:str) -> List[Dict[str,Any]]:
        frames:List[Dict[str,Any]] = []
        chapter_dir = os.path.join(project_path, chapter)
        for frame in sorted(os.listdir(chapter_dir)):
            if frame not in ObsPreprocessor.ignoreFiles:
                text = read_file(os.path.join(project_path, chapter, frame))
                frames.append({
                    'id': chapter + '-' + frame.strip('.txt'),
                    'text': text
                })
        return frames


    def is_chunked(self, project) -> bool:
        chapters = self.rc.chapters(project.identifier)
        if chapters and len(chapters):
            chunks = self.rc.chunks(project.identifier, chapters[0])
            for chunk in chunks:
                if os.path.basename(chunk) in ['title.txt', 'reference.txt', '01.txt']:
                    return True
        return False


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"Obs preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        for project in self.rc.projects:
            AppSettings.logger.debug(f"OBS preprocessor: Copying markdown files for '{project.identifier}' …")
            project_path = os.path.join(self.source_dir, project.path)
            # Copy all the markdown files in the project root directory to the output directory
            for file_path in glob(os.path.join(project_path, '*.md')):
                output_file_path = os.path.join(self.output_dir, os.path.basename(file_path))
                if os.path.isfile(file_path) and not os.path.exists(output_file_path) \
                        and os.path.basename(file_path) not in self.ignoreFiles:
                    copy(file_path, output_file_path)
                    self.num_files_written += 1
            if self.is_chunked(project):
                for chapter in self.get_chapters(project_path):
                    markdown = f"# {chapter['title']}\n\n"
                    for frame in chapter['frames']:
                        markdown += f"![Frame {frame.get('id')}](https://cdn.door43.org/obs/jpg/360px/obs-en-{frame.get('id')}.jpg)\n\n"
                        markdown += frame['text'] + '\n\n'
                    markdown += f"_{chapter['reference']}_\n"
                    output_file = os.path.join(self.output_dir, f"{chapter.get('id')}.md")
                    write_file(output_file, markdown)
                    self.num_files_written += 1
            else:
                for chapter in self.rc.chapters(project.identifier):
                    f = None
                    if os.path.isfile(os.path.join(project_path, chapter, '01.md')):
                        f = os.path.join(project_path, chapter, '01.md')
                    elif os.path.isfile(os.path.join(project_path, chapter, 'intro.md')):
                        f = os.path.join(project_path, chapter, 'intro.md')
                    if f:
                        copy(f, os.path.join(self.output_dir, f'{chapter}.md'))
                        self.num_files_written += 1
        if self.num_files_written == 0:
            AppSettings.logger.error(f"OBS preprocessor didn't write any markdown files")
            self.errors.append("No OBS source files discovered")
        else:
            AppSettings.logger.debug(f"OBS preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")
        AppSettings.logger.debug(f"OBS preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of ObsPreprocessor run()
# end of class ObsPreprocessor



class ObsNotesPreprocessor(Preprocessor):
    def __init__(self, *args, **kwargs) -> None:
        super(ObsNotesPreprocessor, self).__init__(*args, **kwargs)
        self.section_container_id = 1
        self.frame_cache = {}
        self.need_to_check_quotes = False
        self.preload_dir = tempfile.mkdtemp(prefix='tX_OBSNotes_linter_preload_')


    def preload_original_text_archive(self, name:str, zip_url:str) -> bool:
        """
        Fetch and unpack the OBS zip file.

        Adapted April 2020 from TN version below

        Sets self.OBS_content_folderpath

        Returns a True/False success flag
        """
        AppSettings.logger.info(f"OBSNotes preprocessorpreload_original_text_archive({name}, {zip_url})…")
        zip_path = os.path.join(self.preload_dir, f'{name}.zip')
        try:
            download_file(zip_url, zip_path)
            unzip(zip_path, self.preload_dir)
            remove_file(zip_path)
        except Exception as e:
            AppSettings.logger.error(f"Unable to download {zip_url}: {e}")
            self.warnings.append(f"Unable to download '{name}' from {zip_url}")
            return False

        # Find the actual content directory
        for something1 in os.listdir(self.preload_dir):
            path1 = os.path.join(self.preload_dir,something1)
            if os.path.isdir(path1) and something1.endswith('_obs'): # Assume it's the right dir
                for something2 in os.listdir(os.path.join(self.preload_dir,something1)):
                    path2 = os.path.join(self.preload_dir,something1,something2)
                    if os.path.isdir(path2) and something2=='content': # It's the content dir
                        self.OBS_content_folderpath = path2
                        print("Content folder is", self.OBS_content_folderpath)
                        return True
                    else: print(f"What was '{something2}' in {path1}???")
            else: print(f"What was '{something1}' in {self.preload_dir}???")

        return False
    # end of ObsNotesPreprocessor.preload_original_text_archive function

    def get_quoted_version(self) -> None:
        """
        See if manifest has relationships back to original language versions
        Compares with the unfoldingWord version if possible
          otherwise falls back to the Door43Catalog version

        Adapted April 2020 from TN version below

        Sets self.need_to_check_quotes to True if successful.
        """
        AppSettings.logger.debug("OBSNotes preprocessor get_quoted_version()…")
        rels = self.rc.resource.relation
        if isinstance(rels, list):
            for rel in rels:
                if 'en/obs' in rel:
                    if '?v=' not in rel:
                        self.warnings.append(f"No OBS version number specified in manifest: '{rel}'")
                    version = rel[rel.find('?v=')+3:]
                    url = f"https://git.door43.org/unfoldingWord/en_obs/archive/v{version}.zip"
                    successFlag = self.preload_original_text_archive('obs', url)
                    if not successFlag: # Try the Door43 Catalog version
                        url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/obs.zip"
                        successFlag = self.preload_original_text_archive('obs', url)
                    if successFlag:
                        self.messages.append(f"Note: Using {url} for checking OBS quotes against.")
                        self.need_to_check_quotes = True
        elif rels:
            AppSettings.logger.debug(f"OBS notes preprocessor get_quoted_version expected a list not {rels!r}")

        if not self.need_to_check_quotes:
            self.warnings.append("Unable to find/load original language (OBS) sources for comparing snippets against.")
    # end of ObsNotesPreprocessor.get_quoted_version()


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"OBSNotes preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")

        # print("repo_name", self.rc.repo_name)
        if self.rc.repo_name.endswith('-tn') or self.rc.repo_name.endswith('-sn'): # Don't do this for 'Questions' projects
            self.get_quoted_version() # Sets self.need_to_check_quotes

        for project in self.rc.projects:
            AppSettings.logger.debug(f"OBSNotes preprocessor: Copying folders and files for project '{project.identifier}' …")
            content_folder_path = os.path.join(self.source_dir, 'content/')
            if not os.path.isdir(content_folder_path):
                self.warnings.append(f"Unable to find 'contents/' folder for '{project.identifier}'")
                continue
            for story_number in range(0, 50+1): # Includes optional story "0" = introduction
                story_number_string = str(story_number).zfill(2)
                story_folder_path = os.path.join(content_folder_path, f'{story_number_string}/')
                markdown = toc_contents = ""
                if os.path.isdir(story_folder_path):
                    # AppSettings.logger.debug(f"Story {story_number}/ found {story_folder_path}")
                    for filename in sorted(os.listdir(story_folder_path)):
                        story_filepath = os.path.join(story_folder_path, filename)
                        if filename.endswith('.md'):
                            story_file_contents = read_file(story_filepath)
                            file_basename = filename[:-3] # Remove the .md
                            markdown += f'\n# <a id="{story_number_string}-{file_basename}"/> {story_number_string}-{file_basename}\n\n{story_file_contents}\n\n'
                            toc_contents += f'  - title: "{story_number_string}-{file_basename}"\n    link: {story_number_string}-{file_basename}\n\n'
                        else:
                            self.warnings.append(f"Unexpected '{filename}' file in 'content/{story_number_string}/'")
                else: # no content/{story_number_string}/ folder
                    story_filepath = os.path.join(content_folder_path, f'{story_number_string}.md')
                    if os.path.isfile(story_filepath):
                        # AppSettings.logger.debug(f"Story {story_number}/ found {story_filepath}")
                        markdown = read_file(story_filepath)
                        title = story_number_string # default
                        if markdown:
                            title = markdown.split('\n',1)[0] # Get the first line
                            if title.startswith("# "): title = title[2:] # Get rid of leading hash
                        print(f"{title=}")
                        # toc_contents += f'  - title: "{title}"\n    link: {story_number_string}\n\n'
                    elif story_number != 0:
                        self.warnings.append(f"Unable to find story {story_number_string} text")
                if markdown:
                    # rc_count = markdown.count('rc://')
                    # if rc_count: print(f"Story number {story_number_string} has {rc_count} 'rc://' links")
                    # double_bracket_count = markdown.count('[[')
                    # if double_bracket_count: print(f"Story number {story_number_string} has {double_bracket_count} '[[…]]' links")
                    if self.need_to_check_quotes:
                        self.check_embedded_quotes( story_number, markdown )
                    markdown = self.fix_OBSNotes_links(markdown, self.repo_owner)
                    rc_count = markdown.count('rc://')
                    if rc_count:
                        msg = f"Story number {story_number_string} still has {rc_count} 'rc://' links!"
                        AppSettings.logger.error(msg)
                        self.warnings.append(msg)
                    write_file(os.path.join(self.output_dir,f'{story_number_string}.md'), markdown)
                    self.num_files_written += 1

                # OBSNotes: Create a toc.yaml file and write it to output dir so they can be used to
                #       generate the ToC on door43.org
                if toc_contents:
                    toc_contents = "title: Table of Contents\nsections:\n" + toc_contents
                    toc_filepath = os.path.join(self.output_dir, f'{story_number_string}-toc.yaml')
                    write_file(toc_filepath,toc_contents)

        if self.num_files_written == 0:
            AppSettings.logger.error("OBSNotes preprocessor didn't write any markdown files")
            self.errors.append("No OBSNotes source files discovered")
        else:
            AppSettings.logger.debug(f"OBSNotes preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        # Delete temp folder
        if prefix and debug_mode_flag:
            AppSettings.logger.debug(f"Temp folder '{self.preload_dir}' has been left on disk for debugging!")
        else:
            remove_tree(self.preload_dir)

        AppSettings.logger.debug(f"OBSNotes preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of ObsNotesPreprocessor run()


    def get_story_frame( self, story_number:str, frame_number:str ) -> str:
        """
        Story and frame number strings should be two digits
            frame_number can be '00' for title (otherwise an integer string like '05')

        OBS files are in self.OBS_content_folderpath
        """
        # AppSettings.logger.debug(f"OBSNotes preprocessor get_story_frame({story_number}, {frame_number})…")

        key = f'{story_number}-{frame_number}'
        if key in self.frame_cache:
            return self.frame_cache[key]

        with open(os.path.join(self.OBS_content_folderpath,f'{story_number}.md')) as obs_file:
            return_text = ''
            in_frame = False
            for j, line in enumerate(obs_file):
                if line and line[-1]=='\n': line = line[:-1] # Remove trailing nl
                if not line: continue

                if frame_number=='00' and j==0: return line

                if 'OBS Image' in line: # images are BEFORE text
                    if in_frame: # we're up to the next frame
                        return_text = return_text.rstrip() # Get rid of the final nl
                        assert return_text
                        self.frame_cache[key] = return_text
                        return return_text
                    if f'-{story_number}-{frame_number}.jpg' in line:
                        in_frame = True
                elif line.startswith('_'): # These are the references at the end
                    if in_frame: # we're up to the next frame
                        return_text = return_text.rstrip() # Get rid of the final nl
                        assert return_text
                        self.frame_cache[key] = return_text
                        return return_text
                elif in_frame:
                    return_text += line + '\n' # Will add an extra nl at end
    # end of ObsNotesPreprocessor get_story_frame function


    def check_embedded_quotes( self, story_number:str, markdown_text:str ) -> None:
        # AppSettings.logger.debug(f"OBSNotes preprocessor check_embedded_quotes({story_number}, {len(markdown_text):,})…")

        story_number = frame_number = None
        for j, line in enumerate(markdown_text.split('\n'), start=1):
            if not line: continue # Just skip blank lines
            # print( j, line )
            if line.startswith('# <a'):
                line_endstuff = line.rstrip()[-5:]
                story_number, frame_number = line_endstuff.split('-')
            elif line.startswith('# '):
                quote = line[2:]
                frame_text = self.get_story_frame(story_number, frame_number)
                qid = f'line {j} in {story_number}-{frame_number}'

                # TODO: A Bible story from (en_obs-tn): What about translated languages ???
                if 'Bible story' not in quote \
                and 'General Information' not in quote \
                and 'Connecting Statement' not in quote:
                    if '...' in quote:
                        # AppSettings.logger.debug(f"Bad ellipse characters in {qid} '{quoteField}'")
                        self.warnings.append(f"Should use proper ellipse character in \"{qid}\": '{quote}'")

                    if '…' in quote:
                        quoteBits = quote.split('…')
                        if ' …' in quote or '… ' in quote:
                            AppSettings.logger.debug(f"Unexpected space(s) beside ellipse in \"{qid}\": '{quote}'")
                            self.warnings.append(f"Unexpected space(s) beside ellipse character in \"{qid}\": '{quote}'")
                    elif '...' in quote: # Yes, we still actually allow this
                        quoteBits = quote.split('...')
                        if ' ...' in quote or '... ' in quote:
                            AppSettings.logger.debug(f"Unexpected space(s) beside ellipse characters in \"{qid}\": '{quote}'")
                            self.warnings.append(f"Unexpected space(s) beside ellipse characters in \"{qid}\": '{quote}'")
                    else:
                        quoteBits = None

                    if quoteBits:
                        numQuoteBits = len(quoteBits)
                        if numQuoteBits >= 2:
                            for index in range(numQuoteBits):
                                if quoteBits[index] not in frame_text: # this is what we really want to catch
                                    # If the quote has multiple parts, create a description of the current part
                                    if index == 0: description = 'beginning'
                                    elif index == numQuoteBits-1: description = 'end'
                                    else: description = f"middle{index if numQuoteBits>3 else ''}"
                                    # AppSettings.logger.debug(f"Unable to find {qid} '{quoteBits[index]}' ({description}) in '{verse_text}' ({ref})")
                                    self.warnings.append(f"Unable to find \"{qid}\": {description} of '<em>{quote}</em>' <b>in</b> <em>{frame_text}</em>")
                        else: # < 2
                            self.warnings.append(f"Ellipsis without surrounding snippet in \"{qid}\": '{quote}'")
                    elif quote not in frame_text:
                        self.warnings.append(f"Unable to find '<em>{quote}</em>' from line {j} in {story_number}-{frame_number} text: <em>{frame_text}</em>")
    # end of ObsNotesPreprocessor check_embedded_quotes function


    def fix_OBSNotes_links(self, content:str, repo_owner:str) -> str:
        """
        OBS Translation Notes contain links to translationAcademy

        (OBS Translation Questions don't seem to have any links)
        """
        # convert tA RC links, e.g. rc://en/ta/man/translate/figs-euphemism
        #   => https://git.door43.org/{repo_owner}/en_ta/translate/figs-euphemism/01.md
        content = re.sub(r'rc://([^/]+)/ta/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/\1_ta/src/branch/master/\3/01.md',
                         content,
                         flags=re.IGNORECASE)
        return content
    # end of ObsNotesPreprocessor fix_OBSNotes_links(content)
# end of class ObsNotesPreprocessor



class BiblePreprocessor(Preprocessor):
    def __init__(self, *args, **kwargs) -> None:
        super(BiblePreprocessor, self).__init__(*args, **kwargs)
        self.book_filenames:List[str] = []
        self.RC_links:List[tuple] = []
    # end of BiblePreprocessor.__init__ function


    def get_book_list(self) -> List[str]:
        self.book_filenames.sort()
        return self.book_filenames
    # end of BiblePreprocessor.get_book_list()


    def remove_closed_w_field(self, B:str, C:str, V:str, line:str, marker:str, text:str) -> str:
        """
        Extract words out of either \\w or \\+w fields
        """
        assert marker in ('w', '+w')

        ixW = text.find(f'\\{marker} ')
        while ixW != -1:
            ixEnd = text.find(f'\\{marker}*', ixW)
            if ixEnd != -1:
                field = text[ixW+len(marker)+2:ixEnd]
                # AppSettings.logger.debug(f"Cleaning \\w field: {field!r} from '{line}'")
                bits = field.split('|')
                adjusted_field = bits[0]
                # AppSettings.logger.debug(f"Adjusted field to: {adjusted_field!r}")
                text = text[:ixW] + adjusted_field + text[ixEnd+len(marker)+2:]
                # AppSettings.logger.debug(f"Adjusted line to: '{text}'")
            else:
                AppSettings.logger.error(f"Missing \\{marker}* in {B} {C}:{V} line: '{line}'")
                self.errors.append(f"{B} {C}:{V} - Missing \\{marker}* closure")
                text = text.replace(f'\\{marker} ', '', 1) # Attempt to limp on
            ixW = text.find(f'\\{marker} ', ixW) # Might be another one
        return text
    # end of BiblePreprocessor.remove_closed_w_field function


    def check_clean_write_USFM_file(self, file_name:str, file_contents:str) -> None:
        """
        Checks (creating warnings) and cleans the USFM text as it writes it.

        Also saves a list of RC links for later checking
            (from inside \\w fields of original Heb/Grk texts,
                e.g., x-tw="rc://*/tw/dict/bible/names/paul)

        TODO: Check/Remove some of this code once tC export is fixed
        TODO: Remove most of this once tX Job Handler handles full USFM3
        """
        # AppSettings.logger.debug(f"check_clean_write_USFM_file( {file_name}, {file_contents[:500]+('…' if len(file_contents)>500 else '')!r} )")
        assert file_name.endswith('.usfm') or file_name.endswith('.USFM')

        # Replacing this code:
        # write_file(file_name, file_contents)
        # return

        # Make sure the directory exists
        make_dir(os.path.dirname(file_name))

        # Clean the USFM
        main_filename_part = file_name[:-5] # Remove .usfm from end
        if main_filename_part.endswith('_book'): main_filename_part = main_filename_part[:-5]
        B = main_filename_part[-3:].upper() # Extract book abbreviation from somepath/nn-BBB.usfm
        if B not in USFM_BOOK_IDENTIFIERS:
            error_msg = f"Unable to determine book code -- got {B!r}"
            AppSettings.logger.critical(error_msg)
            self.errors.append(error_msg)

        has_USFM3_line = '\\usfm 3' in file_contents
        preadjusted_file_contents = file_contents
        needs_global_check = False

        # Check for GIT conflicts
        for conflict_chars in ('<<<<<<<', '>>>>>>>', '======='): # 7-chars in each one
            if conflict_chars in file_contents:
                error_msg = f"{B} - There appears to be {file_contents.count(conflict_chars)} unresolved conflicts in USFM file (See '{conflict_chars}')"
                AppSettings.logger.critical(error_msg)
                self.errors.append(error_msg)
                break # Only want one error per file

        # Check illegal characters -- gives errors
        for illegal_chars in ('\\\\', '**',):
            if illegal_chars in file_contents:
                count = file_contents.count( illegal_chars )
                error_msg = f"{B} - {'One' if count==1 else count} unexpected '{illegal_chars}' in USFM file"
                AppSettings.logger.error(error_msg)
                self.errors.append(error_msg)

        # Check unusual characters -- gives warnings
        for unusual_chars in ('␣', '  ',
                            ' .', ' ,', ' :', ' ?', ' !',
                            '..',       '::', '??', '!!', # NOTE: doubled commas can occur inside \w fields
                            ):
            if unusual_chars in file_contents:
                count = file_contents.count( unusual_chars )
                error_msg = f"{B} - {'One' if count==1 else count} unusual '{unusual_chars.replace(' ','␣')}' in USFM file"
                AppSettings.logger.error(error_msg)
                self.warnings.append(error_msg)

        # Check USFM pairs
        for opener,closer in (
                                # Character formatting
                                ('\\add ', '\\add*'),
                                ('\\addpn ', '\\addpn*'),
                                ('\\bd ', '\\bd*'),
                                ('\\bdit ', '\\bdit*'),
                                ('\\bk ', '\\bk*'),
                                ('\\dc ', '\\dc*'),
                                ('\\em ', '\\em*'),
                                ('\\fig ', '\\fig*'),
                                ('\\it ', '\\it*'),
                                ('\\k ', '\\k*'),
                                ('\\nd ', '\\nd*'),
                                ('\\ndx ', '\\ndx*'),
                                ('\\no ', '\\no*'),
                                ('\\ord ', '\\ord*'),
                                ('\\pn ', '\\pn*'),
                                ('\\pro ', '\\pro*'),
                                ('\\qt ', '\\qt*'),
                                ('\\sc ', '\\sc*'),
                                ('\\sig ', '\\sig*'),
                                ('\\sls ', '\\sls*'),
                                ('\\tl ', '\\tl*'),
                                ('\\w ', '\\w*'),
                                ('\\wg ', '\\wg*'),
                                ('\\wh ', '\\wh*'),
                                ('\\wj ', '\\wj*'),

                                ('\\ca ', '\\ca*'),
                                ('\\va ', '\\va*'),

                                ('\\f ', '\\f*'),
                                ('\\x ', '\\x*'),
                             ):
            cnt1, cnt2 = file_contents.count(opener), file_contents.count(closer)
            if cnt1 != cnt2:
                error_msg = f"{B} - Mismatched '{opener}' ({cnt1:,}) and '{closer}' ({cnt2:,}) field counts"
                AppSettings.logger.error(error_msg)
                self.errors.append(error_msg)
            cnt = file_contents.count(f'{opener}{closer}') + file_contents.count(f'{opener} {closer}')
            if cnt:
                error_msg = f"{B} - {'One' if cnt==1 else cnt} empty '{opener}{closer}' field{'' if cnt==1 else 's'}"
                AppSettings.logger.error(error_msg)
                self.warnings.append(error_msg)

        # Find and warn about (useless) paragraph formatting before a section break, etc.
        #                                                   (probably should be after break)
        for marker1,marker2,thisRE in (
                                        ('p', 's', r'\\p *\n*?\\s'),
                                        ('p', 'ts', r'\\p *\n*?\\ts'),
                                        ('p', 'q', r'\\p *\n*?\\q'),
                                        ('p', 'p', r'\\p *\n*?\\p'),
                                        ('p', 'r', r'\\p *\n*?\\r'),
                                        ('p', 'd', r'\\p *\n*?\\d'),
                                        ('m', 's', r'\\m *\n*?\\s'),
                                        ('m', 'ts', r'\\m *\n*?\\ts'),
                                        ('m', 'q', r'\\m *\n*?\\q'),
                                        ('m', 'p', r'\\m *\n*?\\p'),
                                        ('m', 'r', r'\\m *\n*?\\r'),
                                        ('m', 'd', r'\\m *\n*?\\d'),
                                        ('q', 's', r'\\q *\n*?\\s'),
                                        ('q', 'ts', r'\\q *\n*?\\ts'),
                                        ('q', 'p', r'\\q *\n*?\\p'),
                                        ('q', 'q', r'\\q *\n*?\\q'),
                                        ('q', 'r', r'\\q *\n*?\\r'),
                                        ('q', 'd', r'\\q *\n*?\\d'),
                                        ('q1', 's', r'\\q1 *\n*?\\s'),
                                        ('q1', 'ts', r'\\q1 *\n*?\\ts'),
                                        ('q1', 'p', r'\\q1 *\n*?\\p'),
                                        ('q1', 'q', r'\\q1 *\n*?\\q'),
                                        ('q1', 'r', r'\\q1 *\n*?\\r'),
                                        ('q1', 'd', r'\\q1 *\n*?\\d'),
                                      ):
            bad_count = len(re.findall(thisRE, preadjusted_file_contents))
            if bad_count:
                s_suffix = '' if bad_count==1 else 's'
                self.warnings.append(f"{B} - {'One' if bad_count==1 else bad_count} useless \\{marker1} marker{s_suffix} before \\{marker2} marker{s_suffix}")

        # Check USFM3 pairs
        for opener,closer in ( # NOTE: These are in addition to the USFM2 ones above
                                # See http://ubsicap.github.io/usfm/master/about/releasenotes.html
                                # Character formatting
                                ('\\fw ', '\\fw*'),
                                ('\\jmp ', '\\jmp*'),
                                ('\\lik ', '\\lik*'),
                                ('\\litl ', '\\litl*'),
                                ('\\liv ', '\\liv*'),
                                ('\\png ', '\\png*'),
                                ('\\rb ', '\\rb*'),
                                ('\\sup ', '\\sup*'),
                                ('\\wa ', '\\wa*'),
                                ('\\xop ', '\\xop*'),
                                ('\\xta ', '\\xta*'),
                                # Milestones
                                ('\\qt-s\\*', '\\qt-e\\*'), # NOTE: Will this still work if it has attributes?
                                ('\\qt1-s\\*', '\\qt1-e\\*'), # NOTE: Will this still work if it has attributes?
                                ('\\qt2-s\\*', '\\qt2-e\\*'), # NOTE: Will this still work if it has attributes?
                                ('\\k-s ', '\\k-e\\*'),
                                ('\\ts-s\\*', '\\ts-e\\*'),
                                ('\\zaln-s ', '\\zaln-e\\*'),
                                ):
            cnt1, cnt2 = file_contents.count(opener), file_contents.count(closer)
            if cnt1 != cnt2:
                error_msg = f"{B} - Mismatched '{opener}' ({cnt1:,}) and '{closer}' ({cnt2:,}) field counts"
                AppSettings.logger.error(error_msg)
                self.warnings.append(error_msg)

        for pmarker in ('p','m','q','q1','q2'):
            thisRE = r'\\v \d{1,3}\s*?\\' + pmarker + ' '
            bad_count = len(re.findall(thisRE, preadjusted_file_contents))
            if bad_count:
                s_suffix = '' if bad_count==1 else 's'
                self.warnings.append(f"{B} - {'One' if bad_count==1 else bad_count} unexpected \\{pmarker} marker{s_suffix} immediately following verse number")

        # Remove translation chunk milestones
        preadjusted_file_contents = preadjusted_file_contents.replace('\\ts\\*\n', '').replace('\\ts\\*', '')

        if '\\s5' in file_contents: # These are deprecated
            warning_msg = f"{B} - \\s5 fields should be coded as \\ts\\* milestones"
            AppSettings.logger.warning(warning_msg)
            self.warnings.append(warning_msg)

        C = V = '0'
        for line in file_contents.split('\n'):
            if line.startswith('\\c '): C, V = line[3:], '0'
            elif line.startswith('\\v '):
                ixSpace = line[3:].find(' ')
                V = line[3:3+ixSpace]
            elif line.startswith('\\s5') and line[3:] and not line[3:].isspace():
                self.errors.append(f"{B} {C}:{V} - unexpected text '{line[3:]}' on \\s5 line")
            elif line.startswith('\\ts\\*') and line[5:] and not line[5:].isspace():
                self.errors.append(f"{B} {C}:{V} - unexpected text '{line[5:]}' on \\ts\\* line")

        if has_USFM3_line: # Issue any global USFM3 warnings
            # Do some global deletions to make things easier
            preadjusted_file_contents = re.sub(r'\\k-s ([^\\]+?)\\\*', '', preadjusted_file_contents) # Remove \k start milestones
            preadjusted_file_contents = re.sub(r'\\zaln-s ([^\\]+?)\\\*', '', preadjusted_file_contents) # Remove \zaln start milestones
            preadjusted_file_contents = preadjusted_file_contents.replace('\\k-e\\*', '') # Remove self-closing keyterm milestones
            preadjusted_file_contents = preadjusted_file_contents.replace('\\zaln-e\\*','') # Remove \zaln end milestones

            # Then do line-by-line changes
            needs_new_line = False
            adjusted_file_contents = ''
            C = V = ''
            for line in preadjusted_file_contents.split('\n'):
                if not line: continue # Ignore blank lines
                # AppSettings.logger.debug(f"Processing line: {line!r}")

                # Get C,V for debug messages
                if line.startswith('\\c '):
                    C = line[3:]
                elif line.startswith('\\v '):
                    V = line[3:].split(' ')[0]

                adjusted_line = line
                if '\\k' in adjusted_line: # Delete these unclosed (bad USFM) fields still in some files
                    # AppSettings.logger.debug(f"Processing user-defined line: {line}")
                    ix = adjusted_line.find('\\k-s ')
                    if ix == -1:
                        ix = adjusted_line.find('\\k-s') # Without expected trailing space
                    if ix != -1:
                        AppSettings.logger.error(f"Non-closed \\k-s milestone in {B} {C}:{V} adjusted line: '{adjusted_line}'")
                        self.warnings.append(f"{B} {C}:{V} - Non-closed \\k-s milestone")
                        if '\\w ' in line:
                            ixW = adjusted_line.find('\\w ', ix+4) # See if \\w field follows \\k-s???
                            if ixW != -1: # Yip, there's word(s) on the end
                                AppSettings.logger.debug(f"With {ix} {ixW} at {B} {C}:{V} adjusted line: '{adjusted_line}'")
                                assert ix < ixW # This code expects the \\k-s to be before the \\w
                                adjusted_line = adjusted_line[:ix] + adjusted_line[ixW:]
                            else:
                                adjusted_line = adjusted_line[:ix] # Remove k-s field right up to end of line
                        else:
                            adjusted_line = adjusted_line[:ix] # Remove k-s field right up to end of line
                if '\\k-s' in adjusted_line:
                    AppSettings.logger.error(f"Remaining \\k-s in {B} {C}:{V} adjusted line: '{adjusted_line}'")
                    self.warnings.append(f"{B} {C}:{V} - Remaining \\k-s field")
                if '\\k-e' in adjusted_line:
                    AppSettings.logger.error(f"Remaining \\k-e in {B} {C}:{V} adjusted line: '{adjusted_line}'")
                    self.warnings.append(f"{B} {C}:{V} - Remaining \\k-e field")

                # Find and save any RC links (from inside \w fields)
                if (match := re.search(r'x-tw="(.+?)"', line)):
                    # print(f"Found RC link {match.group(1)} at {B} {C}:{V}")
                    self.RC_links.append( (B,C,V, 'tW', match.group(1)) )

                # Remove any \w fields (just leaving the word)
                adjusted_line = self.remove_closed_w_field(B, C, V, line, 'w', adjusted_line)
                # Remove any \+w fields (just leaving the word)
                adjusted_line = self.remove_closed_w_field(B, C, V, line, '+w', adjusted_line)
                # Be careful not to mess up on \wj
                # assert '\\w ' not in adjusted_line and '\\w\t' not in adjusted_line and '\\w\n' not in adjusted_line
                # assert '\\w*' not in adjusted_line
                for illegal_sequence in ('\\w ', '\\w\t', '\\w\n',
                                         '\\+w ', '\\+w\t', '\\+w\n', ):
                    if illegal_sequence in adjusted_line:
                        AppSettings.logger.error(f"Missing \\w* in {B} {C}:{V} line: '{line}'")
                        self.errors.append(f"{B} {C}:{V} - Unprocessed '{illegal_sequence}' in line")
                        adjusted_line = adjusted_line.replace(illegal_sequence, '') # Attempt to limp on
                if adjusted_line != line: # it's non-blank and it changed
                    # if 'EPH' in file_name:
                        #  AppSettings.logger.debug(f"Adjusted {B} {C}:{V} \\w line from {line!r} to {adjusted_line!r}")
                    adjusted_file_contents += ('' if adjusted_line.startswith('\\v ') or adjusted_line.startswith('\\f ')
                                                    else ' ') \
                                                + adjusted_line
                    needs_new_line = True
                else: # the line didn't change (no \k \w or \z fields encountered)
                    if needs_new_line:
                        adjusted_file_contents += '\n'
                        needs_new_line = False
                        needs_global_check = True
                    # Copy across unchanged lines
                    adjusted_file_contents += line + '\n'


        else: # Not marked as USFM3
            # old code to handle bad tC USFM
            # First do global fixes to bad tC USFM
            # Hide good \q# markers
            preadjusted_file_contents = re.sub(r'\\q([1234acdmrs]?)\n', r'\\QQQ\1\n', preadjusted_file_contents) # Hide valid \q# markers
            # Invalid \q… markers
            preadjusted_file_contents, n1 = re.subn(r'\\q([^ 1234acdmrs])', r'\\q \1', preadjusted_file_contents) # Fix bad USFM \q without following space
            # \q markers with following text but missing the space in-betweeb
            preadjusted_file_contents, n2 = re.subn(r'\\(q[1234])([^ ])', r'\\\1 \2', preadjusted_file_contents) # Fix bad USFM \q without following space
            if n1 or n2: self.errors.append(f"{B} - {n1+n2:,} badly formed \\q markers")
            # Restore good \q# markers
            preadjusted_file_contents = re.sub(r'\\QQQ([1234acdmrs]?)\n', r'\\q\1\n', preadjusted_file_contents) # Repair valid \q# markers

            # Hide empty \p markers
            preadjusted_file_contents = re.sub(r'\\p\n', r'\\PPP\n', preadjusted_file_contents) # Hide valid \p markers
            # Invalid \p… markers -- allowed pc ph(#) pi(#) pm po pr pe(riph)
            preadjusted_file_contents, n = re.subn(r'\\p([^ chimore])', r'\\p \1', preadjusted_file_contents) # Fix bad USFM \p without following space
            if n: self.errors.append(f"{B} - {n:,} badly formed \\p markers")
            # Restore empty \p markers
            preadjusted_file_contents = re.sub(r'\\PPP\n', r'\\p\n', preadjusted_file_contents) # Repair valid \p markers

            # Then do other global clean-ups
            ks_count = preadjusted_file_contents.count('\\k-s\\*')
            if not ks_count:
                ks_count = preadjusted_file_contents.count('\\k-s')
            ke_count = preadjusted_file_contents.count('\\k-e\\*')
            if not ke_count:
                ke_count = preadjusted_file_contents.count('\\k-e')
            zs_count = preadjusted_file_contents.count('\\zaln-s')
            ze_count = preadjusted_file_contents.count('\\zaln-e')
            if ks_count or zs_count or zs_count or ze_count: # Assume it's USFM3
                if not has_USFM3_line:
                    self.warnings.append(f"{B} - '\\usfm 3.0' line seems missing")
            close_count = preadjusted_file_contents.count('\\*')
            expected_close_count = ks_count + ke_count + zs_count + ze_count
            if close_count < expected_close_count:
                self.warnings.append(f"{B} - {expected_close_count-close_count:,} unclosed \\k or \\zaln milestone markers")
            preadjusted_file_contents = preadjusted_file_contents.replace('\\k-e\\*', '') # Remove self-closing keyterm milestones
            preadjusted_file_contents = preadjusted_file_contents.replace('\\zaln-e\\*', '') # Remove self-closing alignment milestones
            if preadjusted_file_contents != file_contents:
                needs_global_check = True


            # Then do line-by-line changes
            needs_new_line = False
            adjusted_file_contents = ''
            C = V = ''
            for line in preadjusted_file_contents.split('\n'):
                if not line: continue # Ignore blank lines
                # AppSettings.logger.debug(f"Processing line: {line!r}")

                # Get C,V for debug messages
                if line.startswith('\\c '):
                    C = line[3:]
                elif line.startswith('\\v '):
                    V = line[3:].split(' ')[0]

                adjusted_line = line
                if '\\k' in adjusted_line: # Delete these fields
                    # TODO: These milestone fields in the source texts should be self-closing
                    # AppSettings.logger.debug(f"Processing user-defined line: {line}")
                    ix = adjusted_line.find('\\k-s')
                    if ix != -1:
                        adjusted_line = adjusted_line[:ix] # Remove k-s field right up to end of line
                assert '\\k-s' not in adjusted_line
                assert '\\k-e' not in adjusted_line
                # HANDLE FAULTY USFM IN UGNT
                if '\\w ' in adjusted_line and adjusted_line.endswith('\\w'):
                    AppSettings.logger.warning(f"Attempting to fix \\w error in {B} {C}:{V} line: '{line}'")
                    adjusted_line += '*' # Try a change to a closing marker

                # Remove \w fields (just leaving the word)
                adjusted_line = self.remove_closed_w_field(B, C, V, line, 'w', adjusted_line)
                # Remove \+w fields (just leaving the word)
                adjusted_line = self.remove_closed_w_field(B, C, V, line, '+w', adjusted_line)
                # Don't mess up on \wj
                for illegal_sequence in ('\\w ', '\\w\t', '\\w\n',
                                         '\\+w ', '\\+w\t', '\\+w\n', ):
                    if illegal_sequence in adjusted_line:
                        AppSettings.logger.error(f"Unclosed '{illegal_sequence}' in {B} {C}:{V} line: '{line}'")
                        self.warnings.append(f"{B} {C}:{V} - Unprocessed '{illegal_sequence}' in line")
                        adjusted_line = adjusted_line.replace(illegal_sequence, '') # Attempt to limp on
                # assert '\\w*' not in adjusted_line
                if '\\z' in adjusted_line: # Delete these user-defined fields
                    # TODO: These milestone fields in the source texts should be self-closing
                    # AppSettings.logger.debug(f"Processing user-defined line: {line}")
                    ix = adjusted_line.find('\\zaln-s')
                    if ix != -1:
                        adjusted_line = adjusted_line[:ix] # Remove zaln-s field right up to end of line
                if '\\z' in adjusted_line:
                    AppSettings.logger.error(f"Remaining \\z in {B} {C}:{V} adjusted line: '{adjusted_line}'")
                    self.warnings.append(f"{B} {C}:{V} - Remaining \\z field")
                if not adjusted_line: # was probably just a \zaln-s milestone with nothing else
                    continue
                if adjusted_line != line: # it's non-blank and it changed
                    # if 'EPH' in file_name:
                        #  AppSettings.logger.debug(f"Adjusted {B} {C}:{V} \\w line from {line!r} to {adjusted_line!r}")
                    adjusted_file_contents += ' ' + adjusted_line
                    needs_new_line = True
                    continue
                assert adjusted_line == line # No \k \w or \z fields encountered

                if needs_new_line:
                    adjusted_file_contents += '\n'
                    needs_new_line = False
                    needs_global_check = True

                # Copy across unchanged lines
                adjusted_file_contents += line + '\n'

        if needs_global_check: # Do some file-wide clean-up
            # AppSettings.logger.debug(f"Doing global fixes for {B} …")
            adjusted_file_contents = re.sub(r'([^\n])\\v ', r'\1\n\\v ', adjusted_file_contents) # Make sure \v goes onto separate line
            adjusted_file_contents = adjusted_file_contents.replace('\n ',' ') # Move lines starting with space up to the previous line
            adjusted_file_contents = adjusted_file_contents.replace('\n\\va ',' \\va ') # Move lines starting with \va up to the previous line
            adjusted_file_contents = re.sub(r'\n([,.;:?])', r'\1', adjusted_file_contents) # Bring leading punctuation up onto the previous line
            adjusted_file_contents = re.sub(r'([^\n])\\s5', r'\1\n\\s5', adjusted_file_contents) # Make sure \s5 goes onto separate line
            while '\n\n' in adjusted_file_contents:
                adjusted_file_contents = adjusted_file_contents.replace('\n\n','\n') # Delete blank lines
            adjusted_file_contents = adjusted_file_contents.replace(' ," ',', "') # Fix common tC quotation punctuation mistake
            adjusted_file_contents = adjusted_file_contents.replace(",' ",",' ") # Fix common tC quotation punctuation mistake
            adjusted_file_contents = adjusted_file_contents.replace(' " ',' "') # Fix common tC quotation punctuation mistake
            adjusted_file_contents = adjusted_file_contents.replace(" ' "," '") # Fix common tC quotation punctuation mistake

        # Write the modified USFM
        if prefix and debug_mode_flag:
            if '\\w ' in adjusted_file_contents or '\\w\t' in adjusted_file_contents or '\\w\n' in adjusted_file_contents:
                AppSettings.logger.debug(f"Writing {file_name}: {adjusted_file_contents}")
            assert '\\w ' not in adjusted_file_contents and '\\w\t' not in adjusted_file_contents and '\\w\n' not in adjusted_file_contents # Raise error
        with open(file_name, 'wt', encoding='utf-8') as out_file:
            out_file.write(adjusted_file_contents)
    # end of BiblePreprocessor.check_clean_write_USFM_file function


    def clean_copy(self, source_pathname: str, destination_pathname: str) -> None:
        """
        Cleans the USFM file as it copies it.

        Note that check_clean_write_USFM_file() also does many checks (creates warnings)
            on the USFM data as it cleans and writes it.
        """
        # AppSettings.logger.debug(f"clean_copy( {source_pathname}, {destination_pathname} )")

        # Replacing this code:
        # copy(source_pathname, destination_pathname)
        # return

        with open(source_pathname, 'rt') as in_file:
            source_contents = in_file.read()
        self.check_clean_write_USFM_file(destination_pathname, source_contents)
    # end of BiblePreprocessor.clean_copy function


    def process_RC_links(self):
        """
        Process the RC links that have been stored in self.RC_links.
        """
        num_links = len(self.RC_links)
        AppSettings.logger.info(f"process_RC_links for {num_links:,} links…")
        done_type_error = False
        cached_links = set()
        handled_count = 0
        for B,C,V, link_type,link_text in self.RC_links:
            handled_count += 1
            if handled_count % 1_000 == 0:
                AppSettings.logger.info(f"  Handled {handled_count:,} links = {handled_count*100 // num_links}% (Got {len(cached_links):,} in cache)")
            # AppSettings.logger.debug(f"Got {B} {C}:{V} {link_type}={link_text}")
            if not link_type == 'tW':
                if not done_type_error:
                    err_msg = f"{B} {C}:{V} - Unexpected '{link_type}' error '{link_text}'"
                    AppSettings.logger.error(err_msg)
                    self.warnings.append(err_msg)
                    done_type_error = True
                    continue
            if not link_text.startswith('rc://*/tw/dict/bible/'):
                err_msg = f"{B} {C}:{V} - Bad {link_type} link format: '{link_text}'"
                AppSettings.logger.error(err_msg)
                self.errors.append(err_msg)
                continue
            link_word = link_text[21:]
            if link_word in cached_links: # we've already checked it
                # print(f"Found '{link_word}' in cache")
                continue
            # TODO: How can we know what the language should be ???
            link_url = f'https://git.door43.org/unfoldingWord/en_tw/raw/branch/master/bible/{link_word}.md'
            # file_contents = get_url(url)
            try:
                with urlopen(link_url) as f:
                    file_start_byte = f.read(1) # Only need to download/read one byte from the file
            except HTTPError:
                cached_links.add(link_word) # So we only get one error per link_word
                err_msg = f"{B} {C}:{V} - Missing {link_type} file for '{link_word}' expected at {link_url[8:]}" # Skip https:// part
                AppSettings.logger.error(err_msg)
                self.warnings.append(err_msg)
                continue
            # print(url, repr(file_start_byte))
            if file_start_byte == b'#': # Expected start of markdown file
                cached_links.add(link_word)
                # if not len(cached_links) % 100:
                #     AppSettings.logger.info(f"  Got {len(cached_links)} links in cache")
            else:
                cached_links.add(link_word) # So we only get one error per link_word
                err_msg = f"{B} {C}:{V} - Possible bad {link_type} file: '{link_text}'"
                AppSettings.logger.error(err_msg)
                self.warnings.append(err_msg)
        # Not needed any more—empty the list to mark them as "processed"
        self.RC_links = []
    # end of BiblePreprocessor.clean_copy function


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"Bible preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        for idx, project in enumerate(self.rc.projects):
            project_path = os.path.join(self.source_dir, project.path)
            file_format = '{0}-{1}.usfm'

            # Case #1: The project path is a file, and thus is one book of the Bible, copy to standard filename
            # AppSettings.logger.debug(f"Bible preprocessor case #1: Copying single Bible file for '{project.identifier}' …")
            if os.path.isfile(project_path):
                if project.identifier.lower() in BOOK_NUMBERS:
                    filename = file_format.format(BOOK_NUMBERS[project.identifier.lower()], project.identifier.upper())
                else:
                    filename = file_format.format(str(idx+1).zfill(2), project.identifier.upper())
                self.clean_copy(project_path, os.path.join(self.output_dir, filename))
                self.book_filenames.append(filename)
                self.num_files_written += 1
            else:
                # Case #2: Project path is a dir with one or more USFM files, is one or more books of the Bible
                AppSettings.logger.debug(f"Bible preprocessor case #2: Copying Bible files for '{project.identifier}' …")
                usfm_files = glob(os.path.join(project_path, '*.usfm'))
                if usfm_files:
                    for usfm_path in usfm_files:
                        book_code = os.path.splitext(os.path.basename(usfm_path))[0].split('-')[-1].lower()
                        if book_code in BOOK_NUMBERS:
                            filename = file_format.format(BOOK_NUMBERS[book_code], book_code.upper())
                        else:
                            filename = f'{os.path.splitext(os.path.basename(usfm_path))[0]}.usfm'
                        output_file_path = os.path.join(self.output_dir, filename)
                        if os.path.isfile(usfm_path) and not os.path.exists(output_file_path):
                            self.clean_copy(usfm_path, output_file_path)
                        self.book_filenames.append(filename)
                        self.num_files_written += 1
                else:
                    # Case #3: Project path is a dir with one or more chapter dirs with chunk & title files
                    AppSettings.logger.debug(f"Bible preprocessor case #3: Combining Bible chapter files for '{project.identifier}' …")
                    chapters = self.rc.chapters(project.identifier)
                    # print("chapters3:", chapters)
                    if chapters:
                        #          Piece the USFM file together
                        front_title_file = os.path.join(project_path, 'front', 'title.txt')
                        # print("title_file1", front_title_file)
                        if os.path.isfile(front_title_file):
                            book_title = self.check_and_clean_title(read_file(front_title_file), 'front/title.txt')
                            print(f"book_title1a = '{book_title}'")
                        else:
                            book_title = self.check_and_clean_title(project.title, 'project.title')
                            print(f"book_title1b = '{book_title}'")
                        # if not title: # yet—old tx-manager code
                        #     title_file = os.path.join(project_path, chapters[0], 'title.txt')
                        #     print("title_file2", title_file)
                        #     if os.path.isfile(title_file):
                        #         title = read_file(title_file)
                        #         title = re.sub(r' \d+$', '', title).strip()
                        #         print("title2", title)
                        #     else:
                        #         title = project.title
                        #         print("title3", title)
                        # if not title: # still—can this code ever execute???
                        #     title_file = os.path.join(project_path, 'title.txt')
                        #     if os.path.isfile(title_file):
                        #         title = read_file(os.path.join(project_path, 'title.txt'))
                        #         print("title4", title)
                        usfm = f"""
\\id {project.identifier.upper()} {self.rc.resource.title}
\\ide UTF-8
\\h {book_title}
\\toc1 {book_title}
\\toc2 {book_title}
\\toc3 {book_title}
\\mt {book_title}
"""
                        # print(f"Chapters: {chapters}")
                        for chapter in chapters: # can include 'front'
                            if chapter in self.ignoreDirectories:
                                continue
                            chapter_num = chapter.lstrip('0')
                            chunks = self.rc.chunks(project.identifier, chapter)
                            if not chunks:
                                continue
                            try: first_chunk = read_file(os.path.join(project_path, chapter, chunks[0]))
                            except Exception as e:
                                self.errors.append(f"Error reading {chapter}/{chunks[0]}: {e}")
                                continue
                            usfm = f'{usfm}\n'
                            if chapter_num.isdigit():
                                if int(chapter_num) == 1:
                                    if os.path.isfile(os.path.join(project_path, chapter, 'title.txt')):
                                        complete_translated__chapter_title = self.check_and_clean_title(read_file(os.path.join(project_path, chapter, 'title.txt')), 'chapter/title.txt')
                                        translated__chapter_title = re.sub(r' \d+$', '', complete_translated__chapter_title).strip()
                                        usfm += f'\\cl {translated__chapter_title}\n'
                                if f'\\c {chapter_num}' not in first_chunk:
                                    AppSettings.logger.error(f"Needed to add '\\c {chapter_num}' marker")
                                    self.errors.append(f"Needed to add '\\c {chapter_num}' marker")
                                usfm += f'\\c {chapter_num}\n'
                                # Following code doesn't work coz structure of HTML renderer
                                #   doesn't allow \\cl fields AFTER the chapter number
                                # if os.path.isfile(os.path.join(project_path, chapter, 'title.txt')):
                                #     complete_translated__chapter_title = read_file(os.path.join(project_path, chapter, 'title.txt'))
                                #     # translated__chapter_title = re.sub(r' \d+$', '', complete_translated__chapter_title).strip()
                                #     if not complete_translated__chapter_title[-1].isdigit():
                                #         AppSettings.logger.error(f"Translated chapter {chapter_num} line seems wrong: '{complete_translated__chapter_title}'")
                                #         self.errors.append(f"Translated chapter {chapter_num} line seems wrong: '{complete_translated__chapter_title}'")
                                #     usfm += f'\\cl {complete_translated__chapter_title}\n'
                            for chunk in chunks:
                                if chunk in self.ignoreFiles:
                                    continue
                                chunk_num = os.path.splitext(chunk)[0].lstrip('0')
                                try: chunk_content = read_file(os.path.join(project_path, chapter, chunk))
                                except Exception as e:
                                    self.errors.append(f"Error reading {chapter}/{chunk}: {e}")
                                    continue
                                chunk_content = chunk_content.replace(f'\\c {chapter_num}', '') # Remove their chapter number coz we added put it in
                                # print(f"Chunk {chunk_num}: {chunk_content}")
                                if f'\\v {chunk_num} ' not in chunk_content:
                                    chunk_content = f'\\v {chunk_num} {chunk_content}'
                                usfm += f'{chunk_content.lstrip()}\n'
                        if project.identifier.lower() in BOOK_NUMBERS:
                            filename = file_format.format(BOOK_NUMBERS[project.identifier.lower()],
                                                          project.identifier.upper())
                        else:
                            filename = file_format.format(str(idx + 1).zfill(2), project.identifier.upper())
                        # print(f"Pre-cleaned USFM was: {usfm}")
                        self.check_clean_write_USFM_file(os.path.join(self.output_dir, filename), usfm)
                        self.book_filenames.append(filename)
                        self.num_files_written += 1
        if self.num_files_written == 0:
            AppSettings.logger.error(f"Bible preprocessor didn't write any usfm files")
            self.errors.append("No Bible source files discovered")
        else:
            AppSettings.logger.debug(f"Bible preprocessor wrote {self.num_files_written} usfm files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        if self.RC_links:
            self.process_RC_links()

        AppSettings.logger.debug(f"Bible preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        # AppSettings.logger.debug(f"Bible preprocessor returning {self.warnings if self.warnings else True}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of BiblePreprocessor.run()
# end of class BiblePreprocessor



class TaPreprocessor(Preprocessor):
    manual_title_map = {
        'checking': 'Checking Manual',
        'intro': 'Introduction to translationAcademy',
        'process': 'Process Manual',
        'translate': 'Translation Manual'
    }


    def __init__(self, *args, **kwargs) -> None:
        super(TaPreprocessor, self).__init__(*args, **kwargs)
        self.section_container_id = 1
        self.need_to_check_quotes = False
        self.loaded_file_path = None
        self.loaded_file_contents = None
        self.preload_dir = tempfile.mkdtemp(prefix='tX_tA_linter_preload_')


    def get_title(self, project, link:str, alt_title:Optional[str]=None) -> str:
        proj = None
        project_config = project.config()
        if project_config and link in project_config:
            proj = project
        else:
            for p in self.rc.projects:
                p_config = p.config()
                if p_config and link in p_config:
                    proj = p
        if proj:
            title_filepath = os.path.join(self.source_dir, proj.path, link, 'title.md')
            if os.path.isfile(title_filepath):
                return self.check_and_clean_title(read_file(title_filepath), f'{proj.path}/{link}/title.md')
        if alt_title:
            return alt_title
        else:
            return link.replace('-', ' ').title()


    def get_ref(self, project, link:str) -> str:
        project_config = project.config()
        if project_config and link in project_config:
            return f'#{link}'
        for p in self.rc.projects:
            p_config = p.config()
            if p_config and link in p_config:
                return f'{p.identifier}.html#{link}'
        return f'#{link}'


    def get_question(self, project, slug:str) -> str:
        subtitle_file = os.path.join(self.source_dir, project.path, slug, 'sub-title.md')
        if os.path.isfile(subtitle_file):
            return self.check_and_clean_title(read_file(subtitle_file), f'{project.path}/{slug}/sub-title.md')


    def get_content(self, project, slug):
        content_filepath = os.path.join(self.source_dir, project.path, slug, '01.md')
        if os.path.isfile(content_filepath):
            content = read_file(content_filepath)
            self.check_punctuation_pairs(content, f'{project.path}/{slug}/01.md')
            return content


    def compile_ta_section(self, project, section, level):
        """
        Recursive section markdown creator

        :param project:
        :param dict section:
        :param int level:
        :return:
        """
        # if prefix and debug_mode_flag:
        #     AppSettings.logger.debug(f"{'  '*level}compile_ta_section for '{section['title']}' {level=} …")

        if 'link' in section:
            link = section['link']
        else:
            link = f'section-container-{self.section_container_id}'
            self.section_container_id = self.section_container_id + 1
        try:
            markdown = f"""{'#' * level} <a id="{link}"/>{self.get_title(project, link, section['title'])}\n\n"""
        except KeyError: # probably missing section title
            msg = f"Title seems missing for '{project.identifier}' level {level} '{link}'"
            AppSettings.logger.warning(msg)
            self.warnings.append(msg)
            markdown = f"""{'#' * level} <a id="{link}"/>MISSING TITLE???\n\n"""

        if 'link' in section:
            top_box = ""
            bottom_box = ""
            question = self.get_question(project, link)
            if question:
                # TODO: Shouldn't text like this be translated ???
                top_box += f"This page answers the question: *{question}*\n\n"
            config = project.config()
            if config and link in config:
                if 'dependencies' in config[link] and config[link]['dependencies']:
                    top_box += 'In order to understand this topic, it would be good to read:\n\n'
                    for dependency in config[link]['dependencies']:
                        top_box += '  * *[{0}]({1})*\n'.\
                            format(self.get_title(project, dependency), self.get_ref(project, dependency))
                if 'recommended' in config[link] and config[link]['recommended']:
                    bottom_box += 'Next we recommend you learn about:\n\n'
                    for recommended in config[link]['recommended']:
                        bottom_box += '  * *[{0}]({1})*\n'.\
                            format(self.get_title(project, recommended), self.get_ref(project, recommended))
            if top_box:
                markdown += f'<div class="top-box box" markdown="1">\n{top_box}\n</div>\n\n'
            content = self.get_content(project, link)
            if content:
                markdown += f'{content}\n\n'
            if bottom_box:
                markdown += f'<div class="bottom-box box" markdown="1">\n{bottom_box}\n</div>\n\n'
            markdown += '---\n\n'  # horizontal rule
        if 'sections' in section:
            if section['sections']:
                for subsection in section['sections']:
                    subsection_markdown = self.compile_ta_section(project, subsection, level + 1)
                    if self.need_to_check_quotes:
                        try: self.check_embedded_quotes(f"{project.identifier}/{section['title']}", subsection['title'], subsection_markdown)
                        except Exception as e:
                            msg = f"{project.identifier} {subsection} Unable to check embedded quotes: {e}"
                            AppSettings.logger.warning(msg)
                            self.warnings.append(msg)
                    markdown += subsection_markdown
            else: # why is it empty? probably user error
                msg = f"'sections' seems empty for '{project.identifier}' toc.yaml: '{section['title']}'"
                AppSettings.logger.warning(msg)
                self.warnings.append(msg)
        return markdown
    # end of TaPreprocessor.compile_ta_section(self, project, section, level)


    def preload_translated_text_archive(self, name:str, zip_url:str) -> bool:
        """
        Fetch and unpack the Hebrew/Greek zip file.

        Returns a True/False success flag
        """
        AppSettings.logger.info(f"preload_translated_text_archive({name}, {zip_url})…")

        zip_path = os.path.join(self.preload_dir, f'{name}.zip')
        try:
            download_file(zip_url, zip_path)
            unzip(zip_path, self.preload_dir)
            remove_file(zip_path)
        except Exception as e:
            AppSettings.logger.error(f"Unable to download {zip_url}: {e}")
            self.warnings.append(f"Unable to download '{name}' from {zip_url}")
            return False
        # AppSettings.logger.debug(f"Got {name} files: {os.listdir(self.preload_dir)}")
        return True
    # end of TaPreprocessor.preload_translated_text_archive function


    def get_quoted_versions(self) -> None:
        """
        See if TA manifest has relationships back to translations

        Compares with the unfoldingWord version if possible
          otherwise falls back to the Door43Catalog version

        Sets self.need_to_check_quotes to True if successful.
        """
        AppSettings.logger.debug("tA preprocessor get_quoted_versions()…")

        rels = self.rc.resource.relation
        if isinstance(rels, list):
            for rel in rels:
                if 'en/ult' in rel:
                    if '?v=' in rel:
                        version = rel[rel.find('?v=')+3:]
                    else:
                        AppSettings.logger.debug(f"No ULT version number specified in manifest: '{rel}'")
                        version = None
                    url = f"https://git.door43.org/unfoldingWord/en_ult/archive/v{version}.zip" \
                        if version else 'https://git.door43.org/unfoldingWord/en_ult/archive/master.zip'
                    successFlag = self.preload_translated_text_archive('ult', url)
                    if not successFlag: # Try the Door43 Catalog version
                        url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/en_ult.zip" \
                            if version else 'https://git.door43.org/unfoldingWord/en_ult/archive/master.zip'
                        successFlag = self.preload_translated_text_archive('ult', url)
                    if successFlag:
                        extra = '' if version else ' (No version number specified in manifest.)'
                        self.messages.append(f"Note: Using {url} for checking ULT quotes against.{extra}")
                        self.need_to_check_quotes = True
                if 'en/ust' in rel:
                    if '?v=' in rel:
                        version = rel[rel.find('?v=')+3:]
                    else:
                        AppSettings.logger.debug(f"No UST version number specified in manifest: '{rel}'")
                        version = None
                    url = f"https://git.door43.org/unfoldingWord/en_ust/archive/v{version}.zip" \
                        if version else 'https://git.door43.org/unfoldingWord/en_ust/archive/master.zip'
                    successFlag = self.preload_translated_text_archive('ust', url)
                    if not successFlag: # Try the Door43 Catalog version
                        url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/en_ust.zip" \
                            if version else 'https://git.door43.org/unfoldingWord/en_ust/archive/master.zip'
                        successFlag = self.preload_translated_text_archive('ust', url)
                    if successFlag:
                        extra = '' if version else ' (No version number specified in manifest.)'
                        self.messages.append(f"Note: Using {url} for checking UST quotes against.{extra}")
                        self.need_to_check_quotes = True
                # if 'en/tn' in rel:
                #     if '?v=' in rel:
                #         version = rel[rel.find('?v=')+3:]
                #     else:
                #         AppSettings.logger.debug(f"No TN version number specified in manifest: '{rel}'")
                #         version = None
                #     url = f"https://git.door43.org/unfoldingWord/en_ust/archive/v{version}.zip" \
                #         if version else 'https://git.door43.org/unfoldingWord/en_tn/archive/master.zip'
                #     successFlag = self.preload_translated_text_archive('tn', url)
                #     if not successFlag: # Try the Door43 Catalog version
                #         url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/en_tn.zip" \
                #             if version else 'https://git.door43.org/unfoldingWord/en_tn/archive/master.zip'
                #         successFlag = self.preload_translated_text_archive('tn', url)
                #     if successFlag:
                #         extra = '' if version else ' (No version number specified in manifest.)'
                #         self.warnings.append(f"Note: Using {url} for checking TN quotes against.{extra}")
                #         self.need_to_check_quotes = True
                # if 'en/tw' in rel:
                #     if '?v=' in rel:
                #         version = rel[rel.find('?v=')+3:]
                #     else:
                #         AppSettings.logger.debug(f"No TW version number specified in manifest: '{rel}'")
                #         version = None
                #     url = f"https://git.door43.org/unfoldingWord/en_ust/archive/v{version}.zip" \
                #         if version else 'https://git.door43.org/unfoldingWord/en_tw/archive/master.zip'
                #     successFlag = self.preload_translated_text_archive('tw', url)
                #     if not successFlag: # Try the Door43 Catalog version
                #         url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/en_tw.zip" \
                #             if version else 'https://git.door43.org/unfoldingWord/en_tw/archive/master.zip'
                #         successFlag = self.preload_translated_text_archive('tw', url)
                #     if successFlag:
                #         extra = '' if version else ' (No version number specified in manifest.)'
                #         self.warnings.append(f"Note: Using {url} for checking TW quotes against.{extra}")
                #         self.need_to_check_quotes = True
        elif rels:
            AppSettings.logger.debug(f"tA preprocessor get_quoted_versions expected a list not {rels!r}")

        if not self.need_to_check_quotes:
            self.warnings.append("Unable to find/load translated sources for comparing tA snippets against.")
    # end of TaPreprocessor.get_quoted_versions()


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"tA preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")

        self.get_quoted_versions() # Sets self.need_to_check_quotes

        for idx, project in enumerate(self.rc.projects):
            AppSettings.logger.debug(f"tA preprocessor: Copying files for '{project.identifier}' …")
            self.section_container_id = 1
            toc = self.rc.toc(project.identifier)
            if project.identifier in self.manual_title_map:
                title = self.manual_title_map[project.identifier]
            else:
                title = f'{project.identifier.title()} Manual'
            markdown = f'# {title}\n\n'
            if toc:
                for section in toc['sections']:
                    markdown += self.compile_ta_section(project, section, 2)
            markdown = self.fix_tA_links(markdown, self.repo_owner)
            output_file = os.path.join(self.output_dir, f'{str(idx+1).zfill(2)}-{project.identifier}.md')
            write_file(output_file, markdown)
            self.num_files_written += 1

            # tA: Copy the toc and config.yaml file to the output dir so they can be used to
            #       generate the ToC on door43.org
            toc_file = os.path.join(self.source_dir, project.path, 'toc.yaml')
            if os.path.isfile(toc_file):
                copy(toc_file, os.path.join(self.output_dir, f'{str(idx+1).zfill(2)}-{project.identifier}-toc.yaml'))
            config_file = os.path.join(self.source_dir, project.path, 'config.yaml')
            if os.path.isfile(config_file):
                copy(config_file, os.path.join(self.output_dir, f'{str(idx+1).zfill(2)}-{project.identifier}-config.yaml'))
            elif project.path!='./':
                self.warnings.append(f"Possible missing config.yaml file in {project.path} folder")
        if self.num_files_written == 0:
            AppSettings.logger.error("tA preprocessor didn't write any markdown files")
            self.errors.append("No tA source files discovered")
        else:
            AppSettings.logger.debug(f"tA preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        # Delete temp folder
        if prefix and debug_mode_flag:
            AppSettings.logger.debug(f"Temp folder '{self.preload_dir}' has been left on disk for debugging!")
        else:
            remove_tree(self.preload_dir)

        AppSettings.logger.debug(f"tA preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of TaPreprocessor run()


    # TODO: What about the quotes with <sup> verse numbers </sup> included???
    compiled_re_quoted_verse = re.compile(r'“(.+?)” \(([123 A-Za-z]+?) (\d{1,3}):(\d{1,3}) (ULT|UST)\)')
    compiled_re_unquoted_verse = re.compile(r'^(?:> )?([^“”>]+?) \(([123 A-Za-z]+?) (\d{1,3}):(\d{1,3}) (ULT|UST)\)',
                                                                    flags=re.MULTILINE)
    compiled_re_quoted_verses = re.compile(r'“(.+?)” \(([123 A-Za-z]+?) (\d{1,3}):(\d{1,3})-(\d{1,3}) (ULT|UST)\)')
    compiled_re_unquoted_verses = re.compile(r'^(?:> )?([^“”>]+?) \(([123 A-Za-z]+?) (\d{1,3}):(\d{1,3})-(\d{1,3}) (ULT|UST)\)',
                                                                    flags=re.MULTILINE)
    def check_embedded_quotes(self, project_id:str, section_id:str, content:str) -> None:
        """
        Find any quoted portions in a markdown section and
            check that they can indeed be found in the quoted translations.
        """
        # display_content = content.replace('\n', ' ')
        # display_content = f'{display_content[:30]}……{display_content[-30:]}'
        # AppSettings.logger.debug(f"check_embedded_quotes({project_id}, {section_id}, {display_content})…")

        # Match and check single verses
        start_index = 0
        while (match := TaPreprocessor.compiled_re_quoted_verse.search(content, start_index)):
            # print(f"Match1a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match1b: {match.groups()}")
            quoteField, bookname,C,V, version_abbreviation = match.groups()
            start_index = match.end() # For next loop

            qid = f"{project_id}/{section_id}"
            # ref = f'{version_abbreviation} {bookname} {C}:{V}'
            self.check_embedded_quote(qid, bookname,C,V, version_abbreviation, quoteField)
        start_index = 0
        while (match := TaPreprocessor.compiled_re_unquoted_verse.search(content, start_index)):
            # print(f"Match2a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match2b: {match.groups()}")
            quoteField, bookname,C,V, version_abbreviation = match.groups()
            start_index = match.end() # For next loop

            qid = f"{project_id}/{section_id}"
            # ref = f'{version_abbreviation} {bookname} {C}:{V}'
            self.check_embedded_quote(qid, bookname,C,V, version_abbreviation, quoteField)

        # Match and check bridged verses (in the same chapter)
        start_index = 0
        while (match := TaPreprocessor.compiled_re_quoted_verses.search(content, start_index)):
            # print(f"Match3a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match3b: {match.groups()}")
            quoteField, bookname,C,V1,V2, version_abbreviation = match.groups()
            start_index = match.end() # For next loop

            qid = f"{project_id}/{section_id}"
            V = f'{V1}-{V2}'
            self.check_embedded_quote(qid, bookname,C,V, version_abbreviation, quoteField)
        start_index = 0
        while (match := TaPreprocessor.compiled_re_unquoted_verses.search(content, start_index)):
            # print(f"Match4a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match4b: {match.groups()}")
            quoteField, bookname,C,V1,V2, version_abbreviation = match.groups()
            start_index = match.end() # For next loop

            qid = f"{project_id}/{section_id}"
            V = f'{V1}-{V2}'
            self.check_embedded_quote(qid, bookname,C,V, version_abbreviation, quoteField)
    # end of TaPreprocessor.check_embedded_quotes function


    def check_embedded_quote(self, qid:str, bookname:str, C:str, V:str, version_abbreviation:str, quoteField:str) -> None:
        """
        Check that the quoted portion can indeed be found in the quoted translation.
        """
        # AppSettings.logger.debug(f"check_embedded_quote({qid}, {bookname}, {C}:{V}, {version_abbreviation}, {quoteField})…")

        ref = f'{version_abbreviation} {bookname} {C}:{V}'
        # full_qid = f"'{qid}' {ref}"
        quoteField = quoteField.replace('*', '').replace('_', '') # Remove emphasis from quoted text

        verse_text = self.get_passage(bookname,C,V, version_abbreviation)
        if not verse_text:
            AppSettings.logger.error(f"Can't get verse text for {bookname} {C}:{V} {version_abbreviation}!")
            return # nothing else we can do here

        if '...' in quoteField:
            # AppSettings.logger.debug(f"Bad ellipse characters in {qid} '{quoteField}'")
            self.warnings.append(f"Should use proper ellipse character in \"{qid}\": '{quoteField}'")

        if '…' in quoteField:
            quoteBits = quoteField.split('…')
            if ' …' in quoteField or '… ' in quoteField:
                AppSettings.logger.debug(f"Unexpected space(s) beside ellipse in \"{qid}\": '{quoteField}'")
                self.warnings.append(f"Unexpected space(s) beside ellipse character in \"{qid}\": '{quoteField}'")
        elif '...' in quoteField: # Yes, we still actually allow this
            quoteBits = quoteField.split('...')
            if ' ...' in quoteField or '... ' in quoteField:
                AppSettings.logger.debug(f"Unexpected space(s) beside ellipse characters in \"{qid}\": '{quoteField}'")
                self.warnings.append(f"Unexpected space(s) beside ellipse characters in \"{qid}\": '{quoteField}'")
        else:
            quoteBits = None

        if quoteBits:
            numQuoteBits = len(quoteBits)
            if numQuoteBits >= 2:
                for index in range(numQuoteBits):
                    if quoteBits[index] not in verse_text: # this is what we really want to catch
                        # If the quote has multiple parts, create a description of the current part
                        if index == 0: description = 'beginning'
                        elif index == numQuoteBits-1: description = 'end'
                        else: description = f"middle{index if numQuoteBits>3 else ''}"
                        # AppSettings.logger.debug(f"Unable to find {qid} '{quoteBits[index]}' ({description}) in '{verse_text}' ({ref})")
                        self.warnings.append(f"Unable to find \"{qid}\": {description} of <em>{quoteField}</em> <b>in</b> <em>{verse_text}</em> ({ref})")
            else: # < 2
                self.warnings.append(f"Ellipsis without surrounding snippet in \"{qid}\": '{quoteField}'")
        elif quoteField not in verse_text:
            # AppSettings.logger.debug(f"Unable to find {qid} '{quoteField}' in '{verse_text}' ({ref})")
            extra_text = " (contains No-Break Space shown as '⍽')" if '\u00A0' in quoteField else ""
            if extra_text: quoteField = quoteField.replace('\u00A0', '⍽')
            self.warnings.append(f"Unable to find \"{qid}\": <em>{quoteField}</em> {extra_text} <b>in</b> <em>{verse_text}</em> ({ref})")
    # end of TaPreprocessor.check_embedded_quote function


    def get_passage(self, bookname:str, C:str,V:str, version_abbreviation:str) -> str:
        """
        Get the information for the given verse(s) out of the appropriate book file.

        Handle verse bridges (within the same chapter).

        Also removes milestones and extra word (\\w) information
        """
        # AppSettings.logger.debug(f"get_passage({bookname}, {C}:{V}, {version_abbreviation})…")

        B = bookname.replace(' ','').replace('Judges','JDG')[:3].upper()
        B = B.replace('SON','SNG').replace('EZE','EZK').replace('JOE','JOL').replace('NAH','NAM')
        B = B.replace('MAR','MRK').replace('JOH','JHN').replace('PHI','PHP').replace('JAM','JAS')
        B = B.replace('1JO','1JN').replace('2JO','2JN').replace('3JO','3JN')
        try: book_number = BOOK_NUMBERS[B.lower()]
        except KeyError: # how can this happen?
            AppSettings.logger.error(f"Unable to find book number for '{bookname} ({B}) {C}:{V}' in get_passage()")
            book_number = 0

        V1 = V2 = V
        if '-' in V: V1, V2 = V.split('-')

        language_code = 'en'
        version_code = f'{language_code}_{version_abbreviation.lower()}'

        book_path = os.path.join(self.preload_dir, f'{version_code}/{book_number}-{B}.usfm')
        # print("book_path", book_path)
        if not os.path.isfile(book_path):
            AppSettings.logger.info(f"Non-existent {book_path}")
            return None
        if self.loaded_file_path != book_path:
            # It's not cached already
            # AppSettings.logger.debug(f"Loading text from {book_path}…")
            with open(book_path, 'rt') as book_file:
                self.loaded_file_contents = book_file.read()
            self.loaded_file_path = book_path
            # Do some initial cleaning and convert to lines
            self.loaded_file_contents = self.loaded_file_contents \
                                            .replace('\\ts\\*','').replace('\\s5','') \
                                            .replace('\\zaln-e\\*','') \
                                            .replace('\\p ', '').replace('\\p\n', '') \
                                            .replace('\\q ', '').replace('\\q\n', '') \
                                            .replace('\\q1 ', '').replace('\\q1\n', '') \
                                            .replace('\\q2 ', '').replace('\\q2\n', '') \
                                            .split('\n')
        # print("loaded_file_contents", self.loaded_file_contents[:2], '……', self.loaded_file_contents[-2:])
        found_chapter = found_verse = False
        verseText = ''
        V2int = int(V2)
        for book_line in self.loaded_file_contents:
            if not found_chapter and book_line == f'\\c {C}':
                found_chapter = True
                continue
            # TODO: Complain about our USFM formatting around \\m
            if found_chapter and not found_verse \
            and (book_line.startswith(f'\\v {V1}') or book_line.startswith(f'\\m \\v {V1}') or book_line.startswith(f'\\m  \\v {V1}')):
                found_verse = True
                if book_line.startswith('\\m '):
                    book_line = book_line[3:].lstrip() # Remove \\m and following space(s)
                book_line = book_line[3+len(V1):] # Delete (start) verse number so test below doesn't fail

            if found_verse:
                if book_line.startswith('\\c '):
                    break # Don't go into the next chapter
                if book_line.startswith('\\v '):
                    ixSp = book_line.find(' ', 3)
                    verse_num_string = book_line[3:] if ixSp==-1 else book_line[3:ixSp]
                    Vint = int(verse_num_string)
                    if Vint > V2int:
                        break # Don't go past the (last) verse

                while True: # Remove self-closed \zaln-s fields
                    ixs = book_line.find('\\zaln-s ')
                    if ixs == -1: break # None / no more
                    ixe = book_line.find('\\*')
                    if ixe == -1:
                        self.errors.append(f"{B} {C}:{V} Missing closing part of {book_line[ixs:]}")
                    book_line = f'{book_line[:ixs]}{book_line[ixe+2:]}' # Remove \zaln-s field
                verseText += ('' if book_line.startswith('\\f ') else ' ') + book_line
        if V1 != V2: # then the text might contain verse numbers
            verseText = re.sub(r'\\v \d{1,3}', '', verseText) # Remove them
        # verseText = verseText.strip().replace('  ', ' ')
        # print(f"Got verse text1: '{verseText}'")

        # Remove \w fields (just leaving the actual Bible text words)
        ixW = verseText.find('\\w ')
        while ixW != -1:
            ixEnd = verseText.find('\\w*', ixW)
            if ixEnd != -1:
                field = verseText[ixW+3:ixEnd]
                bits = field.split('|')
                adjusted_field = bits[0]
                verseText = verseText[:ixW] + adjusted_field + verseText[ixEnd+3:]
            else:
                AppSettings.logger.error(f"Missing \\w* in {B} {C}:{V} verseText: '{verseText}'")
                verseText = verseText.replace('\\w ', '', 1) # Attempt to limp on
            ixW = verseText.find('\\w ', ixW+1) # Might be another one
        # print(f"Got verse text2: '{verseText}'")

        # Remove footnotes
        verseText = re.sub(r'\\f (.+?)\\f\*', '', verseText)
        # Remove alternative versifications
        verseText = re.sub(r'\\va (.+?)\\va\*', '', verseText)
        verseText = re.sub(r'\\ca (.+?)\\ca\*', '', verseText)
        # print(f"Got verse text3: '{verseText}'")

        # Final clean-up (shouldn't be necessary, but just in case)
        return verseText.strip().replace('  ', ' ')
    # end of TaPreprocessor.get_passage function


    def fix_tA_links(self, content:str, repo_owner:str) -> str:
        """
        For tA
        """
        # convert RC links, e.g. rc://en/tn/help/1sa/16/02
        #                           => https://git.door43.org/{repo_owner}/en_tn/1sa/16/02.md
        content = re.sub(r'rc://([^/]+)/([^/]+)/([^/]+)/([^\s\\p{P})\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/\1_\2/src/branch/master/\4.md', content, flags=re.IGNORECASE)
        # fix links to other sections within the same manual (only one ../ and a section name)
        # e.g. [Section 2](../section2/01.md) => [Section 2](#section2)
        content = re.sub(r'\]\(\.\./([^/)]+)/01.md\)', r'](#\1)', content)
        # fix links to other manuals (two ../ and a manual name and a section name)
        # e.g. [how to translate](../../translate/accurate/01.md) => [how to translate](translate.html#accurate)
        for idx, project in enumerate(self.rc.projects):
            pattern = re.compile(r'\]\(\.\./\.\./{0}/([^/)]+)/01.md\)'.format(project.identifier))
            replace = r']({0}-{1}.html#\1)'.format(str(idx+1).zfill(2), project.identifier)
            content = re.sub(pattern, replace, content)
        # fix links to other sections that just have the section name but no 01.md page (preserve http:// links)
        # e.g. See [Verbs](figs-verb) => See [Verbs](#figs-verb)
        content = re.sub(r'\]\(([^# :/)]+)\)', r'](#\1)', content)
        # convert URLs to links if not already
        content = re.sub(r'([^"(\[])((http|https|ftp)://[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])', r'\1[\2](\2)',
                         content, flags=re.IGNORECASE)
        # URLS wth just www at the start, no http
        content = re.sub(r'([^A-Z0-9"(/])(www\.[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])', r'\1[\2](http://\2)',
                         content, flags=re.IGNORECASE)
        return content
    # end of TaPreprocessor fix_tA_links(content)
# end of class TaPreprocessor



class TqPreprocessor(Preprocessor):

    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"tQ preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        index_json = {
            'titles': {},
            'chapters': {},
            'book_codes': {}
        }
        headers_re = re.compile('^(#+) +(.+?) *#*$', flags=re.MULTILINE)
        for project in self.rc.projects:
            AppSettings.logger.debug(f"tQ preprocessor: Combining chapters for '{project.identifier}' …")
            if project.identifier in BOOK_NAMES:
                markdown = ''
                book = project.identifier.lower()
                html_file = f'{BOOK_NUMBERS[book]}-{book.upper()}.html'
                index_json['book_codes'][html_file] = book
                name = BOOK_NAMES[book]
                index_json['titles'][html_file] = name
                chapter_dirs = sorted(glob(os.path.join(self.source_dir, project.path, '*')))
                markdown += f'# <a id="tq-{book}"/> {name}\n\n'
                index_json['chapters'][html_file]:List[str] = []
                if chapter_dirs:
                    for chapter_dir in chapter_dirs:
                        # AppSettings.logger.debug(f"tQ preprocessor: Processing {chapter_dir} for '{project.identifier}' {book} …")
                        chapter = os.path.basename(chapter_dir)
                        if chapter in self.ignoreFiles or chapter == 'manifest.json':
                            # NOTE: Would it have been better to check for file vs folder here (and ignore files) ???
                            continue

                        chunk_txt_filepaths = sorted(glob(os.path.join(chapter_dir, '*.txt')))
                        # If there are JSON txt files in chapter folders, convert them to md format (and delete the original)
                        #   (These are created by tS)
                        if chunk_txt_filepaths:
                            txt2md(chapter_dir)
                            # convertedCount = txt2md(chapter_dir)
                            # if convertedCount:
                            #     AppSettings.logger.debug(f"tQ preprocessor: Converted {convertedCount} txt files in {chapter} to JSON")

                        link = f'tq-chapter-{book}-{chapter.zfill(3)}'
                        index_json['chapters'][html_file].append(link)
                        markdown += f"""## <a id="{link}"/> {name} {chapter.lstrip('0')}\n\n"""
                        chunk_filepaths = sorted(glob(os.path.join(chapter_dir, '*.md')))
                        if chunk_filepaths:
                            for chunk_idx, chunk_filepath in enumerate(chunk_filepaths):
                                # AppSettings.logger.debug(f"tQ preprocessor: Processing {chunk_file} in {chapter_dir} for '{project.identifier}' {book} …")
                                start_verse = os.path.splitext(os.path.basename(chunk_filepath))[0].lstrip('0')
                                if chunk_idx < len(chunk_filepaths)-1:
                                    try:
                                        end_verse = str(int(os.path.splitext(os.path.basename(chunk_filepaths[chunk_idx+1]))[0])-1)
                                    except ValueError:
                                        # Can throw a ValueError if chunk is not an integer, e.g., '5&8' or contains \u00268 (ɨ)
                                        initial_string = os.path.splitext(os.path.basename(chunk_filepaths[chunk_idx+1]))[0]
                                        msg = f"{book} {chapter} had a problem handling '{initial_string}'"
                                        AppSettings.logger.critical(msg)
                                        self.warnings.append(msg)
                                        # TODO: The following is probably not the best/right thing to do???
                                        end_verse = BOOK_CHAPTER_VERSES[book][chapter.lstrip('0')]
                                else:
                                    try:
                                        end_verse = BOOK_CHAPTER_VERSES[book][chapter.lstrip('0')]
                                    except KeyError:
                                        AppSettings.logger.critical(f"{book} does not normally contain chapter '{chapter}'")
                                        self.warnings.append(f"{book} does not normally contain chapter '{chapter}'")
                                        # TODO: The following is probably not the best/right thing to do???
                                        end_verse = '199'
                                link = f'tq-chunk-{book}-{str(chapter).zfill(3)}-{str(start_verse).zfill(3)}'
                                markdown += '### <a id="{0}"/>{1} {2}:{3}{4}\n\n'.\
                                    format(link, name, chapter.lstrip('0'), start_verse,
                                        '-'+end_verse if start_verse != end_verse else '')
                                try: text = read_file(chunk_filepath) + '\n\n'
                                except Exception as e:
                                    self.errors.append(f"Error reading {os.path.basename(chunk_filepath)}: {e}")
                                    continue
                                text = headers_re.sub(r'\1### \2', text)  # This will bump any header down 3 levels
                                markdown += text
                        else: # no chunk files
                            msg = f"No .md chunk files found in {book} {chapter} folder"
                            AppSettings.logger.warning(msg)
                            self.warnings.append(msg)
                else: # no chapter dirs
                    msg = f"No chapter folders found in {book} folder"
                    AppSettings.logger.warning(msg)
                    self.errors.append(msg)
                file_path = os.path.join(self.output_dir, f'{BOOK_NUMBERS[book]}-{book.upper()}.md')
                write_file(file_path, markdown)
                self.num_files_written += 1
            else:
                AppSettings.logger.debug(f'TqPreprocessor: extra project found: {project.identifier}')

        if self.num_files_written == 0:
            AppSettings.logger.error(f"tQ preprocessor didn't write any markdown files")
            self.errors.append("No tQ source files discovered")
        else:
            AppSettings.logger.debug(f"tQ preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        # Write out TQ index.json
        output_file = os.path.join(self.output_dir, 'index.json')
        write_file(output_file, index_json)
        AppSettings.logger.debug(f"tQ preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of TqPreprocessor run()
# end of class TqPreprocessor



class TwPreprocessor(Preprocessor):
    section_titles = {
        'kt': 'Key Terms',
        'names': 'Names',
        'other': 'Other'
    }


    def __init__(self, *args, **kwargs) -> None:
        super(TwPreprocessor, self).__init__(*args, **kwargs)
        self.content_cache = {}
    # end of TwPreprocessor.__init__ function


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"tW preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        index_json = {
            'titles': {},
            'chapters': {},
            'book_codes': {}
            }

        # Handle a specific non-conformant tW output (JSON in .txt files in 01/ folder) by ts-desktop
        dir_list = os.listdir(self.source_dir)
        if len(dir_list)>=3 \
        and '01' in dir_list and 'LICENSE.md' in dir_list and 'manifest.json' in dir_list:
            # TODO: Is the above the best way to detect these types of repos (One has .DS_Store file also)
            # Handle tW json (.txt) files containing "title" and "body" fields
            AppSettings.logger.info(f"tW preprocessor moving to '01' folder (had {dir_list})…")
            assert len(self.rc.projects) == 1
            project = self.rc.projects[0]
            AppSettings.logger.debug(f"tW preprocessor 01: Copying files for '{project.identifier}' …")
            # AppSettings.logger.debug(f"tW preprocessor 01: project.path='{project.path}'")
            # Collect all the JSON MD data from the text files into dictionaries
            term_text = {}
            section = 'other' # since we don't have any other info to set this inteligently (e.g., KTs, names)
            key = f'{section}.html'
            index_json['titles'][key] = self.section_titles[section]
            index_json['chapters'][key] = {}
            index_json['book_codes'][key] = section
            term_files = sorted(glob(os.path.join(self.source_dir, '01/', '*.txt')))
            for term_filepath in term_files:
                # These .txt files actually contain JSON (which contains markdown)
                AppSettings.logger.debug(f"tW preprocessor 01: processing '{term_filepath}' …")
                term = os.path.splitext(os.path.basename(term_filepath))[0]
                try: text = read_file(term_filepath)
                except Exception as e:
                    self.errors.append(f"Error reading {os.path.basename(term_filepath)}: {e}")
                    continue
                try:
                    json_data = json.loads(text)
                except json.decoder.JSONDecodeError as e:
                    # Clean-up the filepath for display (mostly removing /tmp folder names)
                    adjusted_filepath = '/'.join(term_filepath.split('/')[6:]) #.replace('/./','/')
                    error_message = f"Badly formed tW json file '{adjusted_filepath}': {e}"
                    AppSettings.logger.error(error_message)
                    self.errors.append(error_message)
                    json_data = {}
                if json_data:
                    unit_count = 0
                    title = body_text = None
                    for tw_unit in json_data:
                        if 'title' in tw_unit and 'body' in tw_unit:
                            title = self.check_and_clean_title(tw_unit['title'], f'{term}.txt: {tw_unit}')
                            if not title:
                                self.warnings.append(f"Missing tW title in {term}.txt: {tw_unit}")
                            elif '\n' in title:
                                self.warnings.append(f"Badly formatted tW title in {term}.txt: {title!r}")
                            raw_body_text = tw_unit['body']
                            if not raw_body_text:
                                self.warnings.append(f"Missing tW body in {term}.txt: {tw_unit}")
                            body_text = f'### <a id="{term}"/>{title}\n\n{raw_body_text}'
                            unit_count += 1
                        else:
                            self.warnings.append(f"Unexpected tW unit in {term}.txt: {tw_unit}")
                    assert unit_count == 1 # Only expect one title/body set I think
                    index_json['chapters'][key][term] = title
                    term_text[term] = body_text
                    self.check_punctuation_pairs(body_text, f'{section}/{term}', allow_close_parenthesis_points=True)
                else:
                    error_message = f"No tW json data found in file '{adjusted_filepath}'"
                    AppSettings.logger.error(error_message)
                    self.errors.append(error_message)
            # Now process the dictionaries to sort terms by title and add to markdown
            markdown = ''
            titles = index_json['chapters'][key]
            terms_sorted_by_title = sorted(titles, key=lambda i: titles[i].lower())
            for term in terms_sorted_by_title:
                # Less efficient to call fix_tW_links for each term here, but it helps us to know which file any errors are in
                fixed_markdown = self.fix_tW_links(term_text[term], section, term, self.repo_owner)
                if markdown:
                    markdown += '<hr>\n\n'
                markdown += f'{fixed_markdown}\n\n'
            markdown = f'# <a id="tw-section-{section}"/>{self.section_titles[section]}\n\n{markdown}'
            # markdown = self.fix_tW_links(markdown, section, self.repo_owner)
            output_file = os.path.join(self.output_dir, f'{section}.md')
            write_file(output_file, markdown)
            self.num_files_written += 1
            config_file = os.path.join(self.source_dir, project.path, 'config.yaml')
            if os.path.isfile(config_file):
                copy(config_file, os.path.join(self.output_dir, 'config.yaml'))
            elif project.path!='./':
                self.warnings.append(f"Possible missing config.yaml file in {project.path} folder")

            # Write out TW index.json
            output_file = os.path.join(self.output_dir, 'index.json')
            write_file(output_file, index_json)

        else: # handle tW markdown files
            title_re = re.compile('^# +(.*?) *#*$', flags=re.MULTILINE)
            headers_re = re.compile('^(#+) +(.+?) *#*$', flags=re.MULTILINE)
            for project in self.rc.projects:
                AppSettings.logger.debug(f"tW preprocessor 02: Copying files for '{project.identifier}' …")
                term_text = {}
                section_dirs = sorted(glob(os.path.join(self.source_dir, project.path, '*')))
                for section_dir in section_dirs:
                    section = os.path.basename(section_dir)
                    if section not in self.section_titles:
                        continue
                    key = f'{section}.html'
                    index_json['titles'][key] = self.section_titles[section]
                    index_json['chapters'][key] = {}
                    index_json['book_codes'][key] = section
                    term_files = sorted(glob(os.path.join(section_dir, '*.md')))
                    for term_filepath in term_files:
                        AppSettings.logger.debug(f"tW preprocessor 02: processing '{term_filepath}' …")
                        term = os.path.splitext(os.path.basename(term_filepath))[0]
                        try: text = read_file(term_filepath)
                        except Exception as e:
                            self.errors.append(f"Error reading {os.path.basename(term_filepath)}: {e}")
                            continue
                        if title_re.search(text):
                            title = title_re.search(text).group(1)
                            text = title_re.sub(r'# <a id="{0}"/>\1 #'.format(term), text)  # inject the term by the title
                        else:
                            title = os.path.splitext(os.path.basename(term_filepath))[0]  # No title found, so using term
                        text = headers_re.sub(r'#\1 \2', text)
                        index_json['chapters'][key][term] = title
                        term_text[term] = text
                        self.check_punctuation_pairs(text, f'{section}/{term}', allow_close_parenthesis_points=True)
                    # Sort terms by title and add to markdown
                    markdown = ''
                    titles = index_json['chapters'][key]
                    terms_sorted_by_title = sorted(titles, key=lambda i: titles[i].lower())
                    for term in terms_sorted_by_title:
                        # Less efficient to call fix_tW_links for each term here, but it helps us to know which file any errors are in
                        fixed_markdown = self.fix_tW_links(term_text[term], section, term, self.repo_owner)
                        if markdown:
                            markdown += '<hr>\n\n'
                        markdown += f'{fixed_markdown}\n\n'
                    markdown = f'# <a id="tw-section-{section}"/>{self.section_titles[section]}\n\n' + markdown
                    # markdown = self.fix_tW_links(markdown, section, self.repo_owner)
                    output_file = os.path.join(self.output_dir, f'{section}.md')
                    write_file(output_file, markdown)
                    self.num_files_written += 1
                    config_file = os.path.join(self.source_dir, project.path, 'config.yaml')
                    if os.path.isfile(config_file):
                        copy(config_file, os.path.join(self.output_dir, 'config.yaml'))
                    elif project.path!='./':
                        self.warnings.append(f"Possible missing config.yaml file in {project.path} folder")

                # Write out TW index.json
                output_file = os.path.join(self.output_dir, 'index.json')
                write_file(output_file, index_json)

        if self.num_files_written == 0:
            AppSettings.logger.error(f"tW preprocessor didn't write any markdown files")
            self.errors.append("No tW source files discovered")
        else:
            AppSettings.logger.debug(f"tW preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")
        AppSettings.logger.debug(f"tW preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of TwPreprocessor run()


    compiled_tA_re = re.compile(r'rc://([^/]+)/ta/([^/]+)/([^\s)\]\n$]+)', flags=re.IGNORECASE)
    compiled_tW_re1 = re.compile(r'rc://([^/]+)/ta/([^/]+)/([^\s)\]\n$]+)')
    # compiled_tN_help_re = re.compile(r'rc://([^/]+)/([^/]+)/([^/]+)/([^\s)\]\n$]+)', flags=re.IGNORECASE)
    def fix_tW_links(self, content:str, sectionName:str, term_name:str, repo_owner:str) -> str:
        """
        Also does some checking now (as from 27 March 2020)
        """
        # AppSettings.logger.debug(f"fix_tW_links('{content[:10]}…', '{sectionName}', '{term_name}', '{repo_owner}')…" )
        assert sectionName in ('kt','names','other')
        sectionName = f'{sectionName}/{term_name}'

        # Convert tA RC links, e.g. rc://en/ta/man/translate/figs-euphemism
        #                           => https://git.door43.org/{repo_owner}/en_ta/translate/figs-euphemism/01.md
        # WAS content = re.sub(r'rc://([^/]+)/ta/([^/]+)/([^\s)\]\n$]+)',
        #                  rf'https://git.door43.org/{repo_owner}/\1_ta/src/branch/master/\3/01.md', content,
        #                  flags=re.IGNORECASE)
        content_start_index = 0
        bad_file_count = 0
        while (match := TwPreprocessor.compiled_tA_re.search(content, content_start_index)):
            # print(f"Match1a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match1b: {match.groups()}")
            assert match.group(2) == 'man'
            link_text = file_url = f'https://git.door43.org/{repo_owner}/{match.group(1)}_ta/src/branch/master/{match.group(3)}/01.md'
            # print(f"Match1c: file URL='{file_url}'")
            try:
                file_contents = self.content_cache[file_url]
            except KeyError:
                try:
                    # AppSettings.logger.debug(f"tW {sectionName} fix_linkA fetching {file_url}…")
                    file_contents = get_url(file_url)
                    if '<h3 id="' in file_contents and len(file_contents)>200:
                        file_contents = 'good' # No need to store entire file contents
                    elif bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tW fix_linkA in '{sectionName}' fetching {file_url} only got '{file_contents}'")
                        self.warnings.append(f"Link in '{sectionName}' to {file_url} only found '{file_contents}'")
                    self.content_cache[file_url] = file_contents # Remember that we were successful
                except Exception as e:
                    bad_file_count += 1
                    if bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tW '{sectionName}' fix_linkA fetching {file_url} got: {e}")
                        self.warnings.append(f"Error in '{sectionName}' with tA '{match.group(3)}' link {file_url}: {e}")
                    link_text = self.content_cache[file_url] = f'INVALID {match.group(3)}'
            content = f'{content[:match.start()]}{link_text}{content[match.end():]}'
            content_start_index = match.start() + 1

        # Convert other RC links, e.g. rc://en/tn/help/1sa/16/02
        #                           => https://git.door43.org/{repo_owner}/en_tn/1sa/16/02.md
        content = re.sub(r'rc://([^/]+)/([^/]+)/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/\1_\2/src/branch/master/\4.md', content,
                         flags=re.IGNORECASE)


        # Fix links to other tW sections within the same manual (only one ../ and a section name that matches section_link)
        # e.g. [covenant](../kt/covenant.md) => [covenant](#covenant)
        compiled_pattern = re.compile( rf'\]\(\.\.\/{sectionName}\/([^/]+).md\)' )
        # WAS content = re.sub(pattern, r'](#\1)', content)
        content_start_index = 0
        bad_file_count = 0
        while (match := compiled_pattern.search(content, content_start_index)):
            # print(f"Match3a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match3b: {match.groups()}")
            link_text = f'](#{match.group(1)})'
            # print(f"Match3c: link_text='{link_text}'")
            filepath = f'{self.source_dir}/bible/{sectionName}/{match.group(1)}.md'
            # print(f'Match3d: filepath={filepath}')
            try:
                file_contents = self.content_cache[filepath]
            except KeyError:
                try:
                    # AppSettings.logger.debug(f"tW {sectionName} fix_linkC fetching {filepath}…")
                    file_contents = read_file(filepath)
                    if '## ' in file_contents and len(file_contents)>200:
                        file_contents = 'good' # No need to store entire file contents
                    elif bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tW fix_linkC in '{sectionName}' reading {filepath} only got '{file_contents}'")
                        self.warnings.append(f"Link in '{sectionName}' to {filepath} only found '{file_contents}'")
                    self.content_cache[filepath] = file_contents # Remember that we were successful
                except Exception as e:
                    bad_file_count += 1
                    if bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tW '{sectionName}' fix_linkC reading {filepath} got: {e}")
                        self.warnings.append(f"Error with '{sectionName}' internal '{match.group(1)}' link {filepath}: {e}" \
                                                .replace(f'{self.source_dir}/', ''))
                    self.content_cache[filepath] = f'INVALID {match.group(1)}'
                    link_text = f']({self.content_cache[filepath]})'
            content = f'{content[:match.start()]}{link_text}{content[match.end():]}'
            content_start_index = match.start() + 1

        # Fix links to other sections within the same manual (only one ../ and a section name)
        # e.g. [commit](../other/commit.md) => [commit](other.html#commit)
        for s in TwPreprocessor.section_titles:
            # Was pattern = re.compile(r'\]\(\.\./{0}/([^/]+).md\)'.format(s))
            # replace = r']({0}.html#\1)'.format(s)
            # content = re.sub(pattern, replace, content)
            compiled_pattern = re.compile(rf'\]\(\.\./{s}/([^/]+).md\)')
            content_start_index = 0
            bad_file_count = 0
            while (match := compiled_pattern.search(content, content_start_index)):
                # print(f"Match4a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
                # print(f"Match4b: {match.groups()}")
                link_text = rf']({s}.html#{match.group(1)})'
                # print(f"Match4c: link_text='{link_text}'")
                filepath = f'{self.source_dir}/bible/{s}/{match.group(1)}.md'
                # print(f'Match4d: filepath={filepath}')
                try:
                    file_contents = self.content_cache[filepath]
                except KeyError:
                    try:
                        # AppSettings.logger.debug(f"tW {sectionName} fix_linkD fetching {filepath}…")
                        file_contents = read_file(filepath)
                        if '## ' in file_contents and len(file_contents)>200:
                            file_contents = 'good' # No need to store entire file contents
                        elif bad_file_count < 15 and len(self.warnings) < 200:
                            AppSettings.logger.debug(f"tW fix_linkD in '{sectionName}' reading {filepath} only got '{file_contents}'")
                            self.warnings.append(f"Link in '{sectionName}' to {filepath} only found '{file_contents}'")
                        self.content_cache[filepath] = file_contents # Remember that we were successful
                    except Exception as e:
                        bad_file_count += 1
                        if bad_file_count < 15 and len(self.warnings) < 200:
                            AppSettings.logger.debug(f"tW '{sectionName}' fix_linkD reading {filepath} got: {e}")
                            self.warnings.append(f"Error with '{sectionName}' internal '{s}/{match.group(1)}' link {filepath}: {e}" \
                                                .replace(f'{self.source_dir}/', ''))
                        self.content_cache[filepath] = f'INVALID {s}/{match.group(1)}'
                        link_text = f']({self.content_cache[filepath]})'
                content = f'{content[:match.start()]}{link_text}{content[match.end():]}'
                content_start_index = match.start() + 1

        # fix links to other sections that just have the section name but no 01.md page (preserve http:// links)
        # e.g. See [Verbs](figs-verb) => See [Verbs](#figs-verb)
        contentSave1 = content
        content = re.sub(r'\]\(([^# :/)]+)\)',
                         r'](#\1)', content)
        if content != contentSave1:
            AppSettings.logger.debug("fix_tW_links still changed links here!")

        # Convert URLs to links if not already
        contentSave2 = content
        content = re.sub(r'([^"(\[])((http|https|ftp)://[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])',
                         r'\1[\2](\2)',
                         content, flags=re.IGNORECASE)
        if content != contentSave2:
            AppSettings.logger.debug("fix_tW_links still changed URLs here!")

        # URLs wth just www at the start, no http
        contentSave3 = content
        content = re.sub(r'([^A-Z0-9"(/])(www\.[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])',
                         r'\1[\2](http://\2)',
                         content, flags=re.IGNORECASE)
        if content != contentSave3:
            AppSettings.logger.debug("fix_tW_links still changed www's here!")

        return content
    # end of TwPreprocessor fix_tW_links function
# end of class TwPreprocessor



class TnPreprocessor(Preprocessor):
    index_json:Dict[str,Any] = {
        'titles': {},
        'chapters': {},
        'book_codes': {}
    }


    def __init__(self, *args, **kwargs) -> None:
        super(TnPreprocessor, self).__init__(*args, **kwargs)
        self.book_filenames:List[str] = []
        self.title_cache = {}
        self.need_to_check_quotes = False
        self.loaded_file_path = None
        self.loaded_file_contents = None
        self.preload_dir = tempfile.mkdtemp(prefix='tX_tN_linter_preload_')
    # end of TnPreprocessor.__init__ function


    def get_book_list(self):
        return self.book_filenames


    def preload_original_text_archive(self, name:str, zip_url:str) -> bool:
        """
        Fetch and unpack the Hebrew/Greek zip file.

        Returns a True/False success flag
        """
        AppSettings.logger.info(f"preload_original_text_archive({name}, {zip_url})…")
        zip_path = os.path.join(self.preload_dir, f'{name}.zip')
        try:
            download_file(zip_url, zip_path)
            unzip(zip_path, self.preload_dir)
            remove_file(zip_path)
        except Exception as e:
            AppSettings.logger.error(f"Unable to download {zip_url}: {e}")
            self.warnings.append(f"Unable to download '{name}' from {zip_url}")
            return False
        # AppSettings.logger.debug(f"Got {name} files:", os.listdir(self.preload_dir))
        return True
    # end of TnPreprocessor.preload_original_text_archive function


    def get_quoted_versions(self) -> None:
        """
        See if manifest has relationships back to original language versions
        Compares with the unfoldingWord version if possible
          otherwise falls back to the Door43Catalog version

        NOTE: Moved here Feb2020 from the tX TN linter because it seemed out of place there

        Sets self.need_to_check_quotes to True if successful.
        """
        AppSettings.logger.debug("tN preprocessor get_quoted_versions()…")
        rels = self.rc.resource.relation
        if isinstance(rels, list):
            for rel in rels:
                if 'hbo/uhb' in rel:
                    if '?v=' not in rel:
                        self.warnings.append(f"No Hebrew version number specified in manifest: '{rel}'")
                    version = rel[rel.find('?v=')+3:]
                    url = f"https://git.door43.org/unfoldingWord/UHB/archive/v{version}.zip"
                    successFlag = self.preload_original_text_archive('uhb', url)
                    if not successFlag: # Try the Door43 Catalog version
                        url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/uhb.zip"
                        successFlag = self.preload_original_text_archive('uhb', url)
                    if successFlag:
                        self.messages.append(f"Note: Using {url} for checking Hebrew quotes against.")
                        self.need_to_check_quotes = True
                if 'el-x-koine/ugnt' in rel:
                    if '?v=' not in rel:
                        self.warnings.append(f"No Greek version number specified in manifest: '{rel}'")
                    version = rel[rel.find('?v=')+3:]
                    url = f"https://git.door43.org/unfoldingWord/UGNT/archive/v{version}.zip"
                    successFlag = self.preload_original_text_archive('ugnt', url)
                    if not successFlag: # Try the Door43 Catalog version
                        url = f"https://cdn.door43.org/{rel.replace('?v=', '/v')}/ugnt.zip"
                        successFlag = self.preload_original_text_archive('ugnt', url)
                    if successFlag:
                        self.messages.append(f"Note: Using {url} for checking Greek quotes against.")
                        self.need_to_check_quotes = True
        elif rels:
            AppSettings.logger.debug(f"tN preprocessor get_quoted_versions expected a list not {rels!r}")

        if not self.need_to_check_quotes:
            self.warnings.append("Unable to find/load original language (Heb/Grk) sources for comparing tN snippets against.")
    # end of TnPreprocessor.get_quoted_versions()


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"tN preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        index_json = {
            'titles': {},
            'chapters': {},
            'book_codes': {}
        }
        try:
            language_id = self.rc.manifest['dublin_core']['language']['identifier']
            AppSettings.logger.debug(f"tN preprocessor: Got {language_id=}")
        except (KeyError, TypeError):
            language_id = 'en'
            AppSettings.logger.debug(f"tN preprocessor: Defaulted to {language_id=}")

        self.get_quoted_versions() # Sets self.need_to_check_quotes

        def do_basic_text_checks(field_name:str, field_data:str) -> None:
            """
            Checks for basic things like leading/trailing/doubled spaces, etc.

            Relies on outer variables for forming warning messages.
            """
            if not field_data: return # Nothing to check
            assert field_name

            len_field_data = len(field_data)
            if field_data[0]==' ':
                self.warnings.append(f"Unexpected leading space(s) in '{field_data[:10].replace(' ','␣')}…' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            elif field_data.lstrip() != field_data:
                self.warnings.append(f"Unexpected leading whitespace in '{field_data[:10].replace(' ','␣')}…' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data.startswith('<br>'):
                self.warnings.append(f"Unexpected leading break in '{field_data[:10].replace('<','&lt;').replace('>','&gt;')}…' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data[-1]==' ':
                self.warnings.append(f"Unexpected trailing space(s) in '…{field_data[-10:].replace(' ','␣')}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            elif field_data.rstrip() != field_data:
                self.warnings.append(f"Unexpected trailing whitespace in '{field_data[:10].replace(' ','␣')}…' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data.endswith('<br>'):
                self.warnings.append(f"Unexpected trailing break in '…{field_data[-10:].replace('<','&lt;').replace('>','&gt;')}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")

            beforeCount, afterCount = 5, 6
            if (ix:=field_data.find('  ')) != -1:
                extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace(' ','␣')
                if ix-beforeCount > 0: extract = f'…{extract}'
                if ix+afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Unexpected double spaces in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if (ix:=field_data.find('\u00A0')) != -1:
                extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace('\u00A0','⍽')
                if ix-beforeCount > 0: extract = f'…{extract}'
                if ix+afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Unexpected non-break space in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if (ix:=field_data.find('\u200B')) != -1:
                extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace('\u200B','⍽')
                if ix-beforeCount > 0: extract = f'…{extract}'
                if ix+afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Unexpected zero-width space in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data[0] == '\u200D':
                extract = field_data[:afterCount].replace('\u200D','⍽')
                if afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Starts with zero-width word joiner in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data[-1] == '\u200D':
                extract = field_data[-beforeCount:].replace('\u200D','⍽')
                if beforeCount < len_field_data: extract = f'…{extract}'
                self.warnings.append(f"Ends with zero-width word joiner in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            # if (ix:=field_data.find('\u200D')) != -1:
            #     extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace('\u200D','⍽')
            #     if ix-beforeCount > 0: extract = f'…{extract}'
            #     if ix+afterCount < len_field_data: extract = f'{extract}…'
            #     self.warnings.append(f"Unexpected zero-width joiner in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data[0] == '\u2060':
                extract = field_data[:afterCount].replace('\u2060','⍽')
                if afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Starts with word joiner in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_data[-1] == '\u2060':
                extract = field_data[-beforeCount:].replace('\u2060','⍽')
                if beforeCount < len_field_data: extract = f'…{extract}'
                self.warnings.append(f"Ends with word joiner in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if (ix:=field_data.find('\n')) != -1:
                extract = field_data[max(ix-beforeCount,0):ix+afterCount]
                if ix-beforeCount > 0: extract = f'…{extract}'
                if ix+afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Unexpected newLine character in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if (ix:=field_data.find('\r')) != -1:
                extract = field_data[max(ix-beforeCount,0):ix+afterCount]
                if ix-beforeCount > 0: extract = f'…{extract}'
                if ix+afterCount < len_field_data: extract = f'{extract}…'
                self.warnings.append(f"Unexpected carriageReturn character in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
            if field_name == 'OrigQuote':
                if (ix:=field_data.find(' …')) != -1:
                    extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace(' ','␣')
                    if ix-beforeCount > 0: extract = f'…{extract}'
                    if ix+afterCount < len_field_data: extract = f'{extract}…'
                    self.warnings.append(f"Unexpected space before ellipse character in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
                if (ix:=field_data.find('… ')) != -1:
                    extract = field_data[max(ix-beforeCount,0):ix+afterCount].replace(' ','␣')
                    if ix-beforeCount > 0: extract = f'…{extract}'
                    if ix+afterCount < len_field_data: extract = f'{extract}…'
                    self.warnings.append(f"Unexpected space after ellipse character in '{extract}' in {field_name} at {B} {C}:{V} ({field_id}) in line {n}")
        # end of do_basic_text_checks

        headers_re = re.compile('^(#+) +(.+?) *#*$', flags=re.MULTILINE)
        EXPECTED_TSV_SOURCE_TAB_COUNT = 8 # So there's one more column than this
        for project in self.rc.projects:
            AppSettings.logger.debug(f"tN preprocessor: Adjusting/Copying file(s) for '{project.identifier}' …")
            if project.identifier in BOOK_NAMES:
                book = project.identifier.lower()
                html_file = f'{BOOK_NUMBERS[book]}-{book.upper()}.html'
                index_json['book_codes'][html_file] = book
                name = BOOK_NAMES[book]
                index_json['titles'][html_file] = name
                # If there's a TSV file, copy it across
                found_tsv = False
                tsv_filename_end = f'{BOOK_NUMBERS[book]}-{book.upper()}.tsv'
                for this_filepath in glob(os.path.join(self.source_dir, '*.tsv')):
                    if this_filepath.endswith(tsv_filename_end): # We have the tsv file
                        found_tsv = True
                        AppSettings.logger.debug(f"tN preprocessor got {this_filepath}")
                        line_number = 1
                        lastB = lastC = lastV = None
                        field_id_list:List[str] = []
                        with open(this_filepath, 'rt') as tsv_source_file:
                            with open(os.path.join(self.output_dir, os.path.basename(this_filepath)), 'wt') as tsv_output_file:
                                for n, tsv_line in enumerate(tsv_source_file, start=1):
                                    tsv_line = tsv_line.rstrip('\n')
                                    tab_count = tsv_line.count('\t')
                                    if line_number == 1:
                                        # AppSettings.logger.debug(f"TSV header line is '{tsv_line}")
                                        if tsv_line != 'Book	Chapter	Verse	ID	SupportReference	OrigQuote	Occurrence	GLQuote	OccurrenceNote':
                                            self.errors.append(f"Unexpected TSV header line: '{tsv_line}' in {os.path.basename(this_filepath)}")
                                    elif tab_count != EXPECTED_TSV_SOURCE_TAB_COUNT:
                                        AppSettings.logger.debug(f"Unexpected line #{line_number} with {tab_count} tabs (expected {EXPECTED_TSV_SOURCE_TAB_COUNT}): '{tsv_line}'")
                                        self.warnings.append(f"Unexpected line #{line_number} with {tab_count} tabs (expected {EXPECTED_TSV_SOURCE_TAB_COUNT}): '{tsv_line}'")
                                        continue # otherwise we crash on the next line
                                    B, C, V, field_id, SupportReference, OrigQuote, Occurrence, GLQuote, OccurrenceNote = tsv_line.split('\t')
                                    if B!=lastB or C!=lastC or V!=lastV:
                                        field_id_list:List[str] = [] # IDs only need to be unique within each verse
                                        lastB, lastC, lastV = B, C, V

                                    if line_number > 1: # not the headings line
                                        # Check book identifier and C V fields
                                        if not B:
                                            self.warnings.append(f"Missing book code at {C}:{V} with '{field_id}'")
                                        if B not in USFM_BOOK_IDENTIFIERS:
                                            self.warnings.append(f"Bad book code '{B}' at {C}:{V} with '{field_id}' (Should be exactly three uppercase letters or digits)")
                                        if not C:
                                            self.warnings.append(f"Missing chapter number at {B} {V} with '{field_id}'")
                                        if C not in ('front',) and not C.isdigit():
                                            self.warnings.append(f"Bad chapter number '{C}' at {B} {V} with '{field_id}'")
                                        if not V:
                                            self.warnings.append(f"Missing verse number at {C}:{V} with '{field_id}'")
                                        if V not in ('intro',) and not V.isdigit():
                                            self.warnings.append(f"Bad verse number '{V}' at {C}:{V} with '{field_id}'")
                                        if C == 'front' and V != 'intro':
                                            self.warnings.append(f"Unexpected C:V combination at {B} {C}:{V} with '{field_id}'")
                                        if line_number > 2: # compare BCV progressions
                                                if B != lastB:
                                                    self.warnings.append(f"Only expected one book code: have '{B}' at {C}:{V} with '{field_id}' after having '{lastB}'")
                                                if C.isdigit() and lastC.isdigit() and int(C) < int(lastC):
                                                    self.warnings.append(f"Chapter numbers out of order at {B} {C}:{V} with '{field_id}' after {lastB} {lastC}:{lastV}")
                                                if C==lastC and V.isdigit() and lastV.isdigit() and int(V) < int(lastV):
                                                    self.warnings.append(f"Verse numbers out of order at {B} {C}:{V} with '{field_id}' after {lastB} {lastC}:{lastV}")

                                        # Check ID field
                                        if not field_id:
                                            self.warnings.append(f"Missing ID at {B} {C}:{V}")
                                        elif len(field_id) != 4:
                                            self.warnings.append(f"Bad ID at {B} {C}:{V} with '{field_id}' (Should be exactly four characters long)")
                                        elif not field_id[0].isalpha():
                                            self.warnings.append(f"Bad ID at {B} {C}:{V} with '{field_id}' (Should start with a letter)")
                                        elif not field_id.replace('-','x').isalnum():
                                            self.warnings.append(f"Bad ID at {B} {C}:{V} with '{field_id}' (Should only contain letters, digits, and hyphens)")
                                        elif field_id.lower() != field_id:
                                            self.warnings.append(f"Bad ID at {B} {C}:{V} with '{field_id}' (Letters should be lowercase)")
                                        if field_id in field_id_list:
                                            self.warnings.append(f"Duplicate ID at {B} {C}:{V} with '{field_id}'")
                                        field_id_list.append(field_id)

                                        if SupportReference:
                                            do_basic_text_checks('SupportReference', SupportReference)
                                            self.check_support_reference(f'{B} {C}:{V}', field_id, SupportReference, OccurrenceNote, self.repo_owner, language_id)

                                        if OrigQuote:
                                            do_basic_text_checks('OrigQuote', OrigQuote)

                                        if (not Occurrence.replace('-','0').isdigit() # allows for -1 as well
                                        or -1>int(Occurrence)>30): # How many words in the longest verse???
                                            self.warnings.append(f"Bad Occurrence number '{Occurrence}' at {B} {C}:{V} with '{field_id}' (Should be number -1,0,1,2,…)")
                                        elif OccurrenceNote == '-1' and '…' in OrigQuote:
                                            self.warnings.append(f"Bad Occurrence number '{Occurrence}' at {B} {C}:{V} with '{field_id}' (-1 can't combine with ellipsis in OrigQuote)")

                                        if GLQuote:
                                            do_basic_text_checks('GLQuote', GLQuote)

                                        if OccurrenceNote:
                                            do_basic_text_checks('OccurrenceNote', OccurrenceNote)
                                        if '://' in OccurrenceNote or '[[' in OccurrenceNote:
                                            OccurrenceNote = self.fix_tN_links(f'{B} {C}:{V}', OccurrenceNote, self.repo_owner, language_id)
                                        if 'rc://' in OccurrenceNote:
                                            self.warnings.append(f"Unable to process link at {B} {C}:{V} in '{OccurrenceNote}'")
                                        if B != 'Book' \
                                        and self.need_to_check_quotes \
                                        and OrigQuote:
                                            try: self.check_original_language_TN_quotes(B,C,V, field_id, OrigQuote)
                                            except Exception as e:
                                                self.warnings.append(f"{B} {C}:{V} Unable to check original language quotes: {e}")

                                    tsv_output_file.write(f'{B}\t{C}\t{V}\t{OrigQuote}\t{OccurrenceNote}\n')
                                    line_number += 1
                        AppSettings.logger.info(f"Loaded {line_number:,} TSV lines from {os.path.basename(this_filepath)}.")
                        self.num_files_written += 1
                        break # Don't bother looking for other files since we found our TSV one for this book
                # NOTE: This code will create an .md file if there is a missing TSV file
                if not found_tsv: # Look for markdown or json .txt
                    markdown = ''
                    chapter_dirs = sorted(glob(os.path.join(self.source_dir, project.path, '*')))
                    markdown += f'# <a id="tn-{book}"/> {name}\n\n'
                    index_json['chapters'][html_file]:List[str] = []
                    for move_str in ['front', 'intro']:
                        self.move_to_front(chapter_dirs, move_str)
                    found_something = False
                    for chapter_dir in chapter_dirs:
                        chapter = os.path.basename(chapter_dir)
                        if chapter in self.ignoreFiles or chapter == 'manifest.json':
                            # NOTE: Would it have been better to check for file vs folder here (and ignore files) ???
                            continue
                        link = f'tn-chapter-{book}-{chapter.zfill(3)}'
                        index_json['chapters'][html_file].append(link)
                        markdown += f"""## <a id="{link}"/> {name} {chapter.lstrip('0')}\n\n"""
                        chunk_filepaths = sorted(glob(os.path.join(chapter_dir, '*.md')))
                        if chunk_filepaths: # We have .md files
                            # AppSettings.logger.debug(f"tN preprocessor: got {len(chunk_filepaths)} md chunk files: {chunk_filepaths}")
                            found_something = True
                            for move_str in ['front', 'intro']:
                                self.move_to_front(chunk_filepaths, move_str)
                            for chunk_idx, chunk_filepath in enumerate(chunk_filepaths):
                                if os.path.basename(chunk_filepath) in self.ignoreFiles:
                                    continue
                                start_verse = os.path.splitext(os.path.basename(chunk_filepath))[0].lstrip('0')
                                if chunk_idx < len(chunk_filepaths)-1:
                                    base_file_name = os.path.splitext(os.path.basename(chunk_filepaths[chunk_idx + 1]))[0]
                                    if base_file_name.isdigit():
                                        end_verse = str(int(base_file_name) - 1)
                                    else:
                                        end_verse = start_verse
                                else:
                                    chapter_str = chapter.lstrip('0')
                                    chapter_verses = BOOK_CHAPTER_VERSES[book]
                                    end_verse = chapter_verses[chapter_str] if chapter_str in chapter_verses else start_verse

                                start_verse_str = str(start_verse).zfill(3) if start_verse.isdigit() else start_verse
                                link = f'tn-chunk-{book}-{str(chapter).zfill(3)}-{start_verse_str}'
                                markdown += '### <a id="{0}"/>{1} {2}:{3}{4}\n\n'. \
                                    format(link, name, chapter.lstrip('0'), start_verse,
                                        '-'+end_verse if start_verse != end_verse else '')
                                try: text = read_file(chunk_filepath) + '\n\n'
                                except Exception as e:
                                    self.errors.append(f"Error reading {os.path.basename(chunk_filepath)}: {e}")
                                    continue
                                text = headers_re.sub(r'\1## \2', text)  # This will bump any header down 2 levels
                                markdown += text
                        else: # See if there's .txt files (as no .md files found)
                            # NOTE: These seem to actually be json files (created by tS)
                            chunk_filepaths = sorted(glob(os.path.join(chapter_dir, '*.txt')))
                            # AppSettings.logger.debug(f"tN preprocessor: got {len(chunk_filepaths)} txt chunk files: {chunk_filepaths}")
                            if chunk_filepaths: found_something = True
                            for move_str in ['front', 'intro']:
                                self.move_to_front(chunk_filepaths, move_str)
                            for chunk_idx, chunk_filepath in enumerate(chunk_filepaths):
                                if os.path.basename(chunk_filepath) in self.ignoreFiles:
                                    # AppSettings.logger.debug(f"tN preprocessor: ignored {chunk_filepath}")
                                    continue
                                start_verse = os.path.splitext(os.path.basename(chunk_filepath))[0].lstrip('0')
                                if chunk_idx < len(chunk_filepaths)-1:
                                    base_file_name = os.path.splitext(os.path.basename(chunk_filepaths[chunk_idx + 1]))[0]
                                    if base_file_name.isdigit():
                                        end_verse = str(int(base_file_name) - 1)
                                    else:
                                        end_verse = start_verse
                                else:
                                    chapter_str = chapter.lstrip('0')
                                    chapter_verses = BOOK_CHAPTER_VERSES[book]
                                    end_verse = chapter_verses[chapter_str] if chapter_str in chapter_verses else start_verse

                                start_verse_str = str(start_verse).zfill(3) if start_verse.isdigit() else start_verse
                                link = f'tn-chunk-{book}-{str(chapter).zfill(3)}-{start_verse_str}'
                                markdown += '### <a id="{0}"/>{1} {2}:{3}{4}\n\n'. \
                                    format(link, name, chapter.lstrip('0'), start_verse,
                                        '-'+end_verse if start_verse != end_verse else '')
                                text = read_file(chunk_filepath)
                                try:
                                    json_data = json.loads(text)
                                except json.decoder.JSONDecodeError as e:
                                    # Clean-up the filepath for display (mostly removing /tmp folder names)
                                    adjusted_filepath = '/'.join(chunk_filepath.split('/')[6:]) #.replace('/./','/')
                                    error_message = f"Badly formed tN json file '{adjusted_filepath}': {e}"
                                    AppSettings.logger.error(error_message)
                                    self.errors.append(error_message)
                                    json_data = {}
                                for tn_unit in json_data:
                                    if 'title' in tn_unit and 'body' in tn_unit:
                                        markdown += f"### {tn_unit['title']}\n\n"
                                        markdown += f"{tn_unit['body']}\n\n"
                                    else:
                                        self.warnings.append(f"Unexpected tN unit in {chunk_filepath}: {tn_unit}")
                    if not found_something:
                        self.errors.append(f"tN Preprocessor didn't find any valid source files for {book}")
                    markdown = self.fix_tN_links(book, markdown, self.repo_owner, language_id)
                    if 'rc://' in markdown:
                        self.warnings.append(f"Unable to all process 'rc://' links in {book}")
                    book_file_name = f'{BOOK_NUMBERS[book]}-{book.upper()}.md'
                    self.book_filenames.append(book_file_name)
                    file_path = os.path.join(self.output_dir, book_file_name)
                    # AppSettings.logger.debug(f"tN preprocessor: writing {file_path} with: {markdown}")
                    write_file(file_path, markdown)
                    self.num_files_written += 1
            else:
                AppSettings.logger.debug(f"TnPreprocessor: extra project found: {project.identifier}")

        if self.num_files_written == 0:
            AppSettings.logger.error(f"tN preprocessor didn't write any markdown files")
            self.errors.append("No tN source files discovered")
        else:
            AppSettings.logger.debug(f"tN preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        # Write out TN index.json
        output_file = os.path.join(self.output_dir, 'index.json')
        write_file(output_file, index_json)

        # Delete temp folder
        if prefix and debug_mode_flag:
            AppSettings.logger.debug(f"Temp folder '{self.preload_dir}' has been left on disk for debugging!")
        else:
            remove_tree(self.preload_dir)

        # AppSettings.logger.debug(f"tN Preprocessor returning with {self.output_dir} = {os.listdir(self.output_dir)}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of TnPreprocessor run()


    def move_to_front(self, files:List[str], move_str:str) -> None:
        if files:
            last_file = files[-1]
            if move_str in last_file:  # move intro to front
                files.pop()
                files.insert(0, last_file)


    def check_original_language_TN_quotes(self, B:str,C:str,V:str, field_id:str, quoteField:str) -> None:
        """
        Check that the quoted portions can indeed be found in the original language versions.

        Moved here Feb2020 from tX TN linter
        """
        # AppSettings.logger.debug(f"check_original_language_TN_quotes({B},{C},{V}, {field_id}, {quoteField})…")
        TNid = f'{B} {C}:{V} ({field_id})'

        # if quoteField.lstrip() != quoteField:
        #     self.warnings.append(f"Unexpected whitespace at start of {TNid} '{quoteField}'")
        # if quoteField.rstrip() != quoteField:
        #     self.warnings.append(f"Unexpected whitespace at end of {TNid} '{quoteField}'")
        # quoteField = quoteField.strip() # so we don't get consequential errors

        if '...' in quoteField:
            # AppSettings.logger.debug(f"Bad ellipse characters in {TNid} '{quoteField}'")
            self.warnings.append(f"Should use proper ellipse character in {TNid} '{quoteField}'")

        if '…' in quoteField:
            quoteBits = quoteField.split('…')
            if ' …' in quoteField or '… ' in quoteField:
                AppSettings.logger.debug(f"Unexpected space(s) beside ellipse in {TNid} '{quoteField}'")
                self.warnings.append(f"Unexpected space(s) beside ellipse character in {TNid} '{quoteField}'")
        elif '...' in quoteField: # Yes, we still actually allow this
            quoteBits = quoteField.split('...')
            if ' ...' in quoteField or '... ' in quoteField:
                AppSettings.logger.debug(f"Unexpected space(s) beside ellipse characters in {TNid} '{quoteField}'")
                self.warnings.append(f"Unexpected space(s) beside ellipse characters in {TNid} '{quoteField}'")
        else:
            quoteBits = None

        verse_text = self.get_passage(B,C,V)
        if not verse_text:
            return # nothing else we can do here

        if quoteBits:
            numQuoteBits = len(quoteBits)
            if numQuoteBits >= 2:
                for index in range(numQuoteBits):
                    if quoteBits[index] not in verse_text: # this is what we really want to catch
                        # If the quote has multiple parts, create a description of the current part
                        if index == 0: description = 'beginning'
                        elif index == numQuoteBits-1: description = 'end'
                        else: description = f"middle{index if numQuoteBits>3 else ''}"
                        # AppSettings.logger.debug(f"Unable to find {TNid} '{quoteBits[index]}' ({description}) in '{verse_text}'")
                        self.warnings.append(f"Unable to find {TNid} {description} of '{quoteField}' in '{verse_text}'")
            else: # < 2
                self.warnings.append(f"Ellipsis without surrounding snippet in {TNid} '{quoteField}'")
        else: # Only a single quote (no ellipsis)
            if quoteField in verse_text:
                # Double check that it doesn't start/stop in the middle of a word
                remainingBits = verse_text.split(quoteField, 1)
                assert len(remainingBits) == 2
                if remainingBits[0] and remainingBits[0][-1].isalpha():
                    badChar = remainingBits[0][-1]
                    badCharString = f" by '{badChar}' {unicodedata.name(badChar)}={hex(ord(badChar))}"
                    AppSettings.logger.debug(f"Seems {TNid} '{quoteField}' might not start at the beginning of a word—it's preceded {badCharString} in '{verse_text}'")
                    self.warnings.append(f"Seems {TNid} '{quoteField}' might not start at the beginning of a word—it's (preceded {badCharString} in '{verse_text}'")
                if remainingBits[1] and remainingBits[1][0].isalpha():
                    badChar = remainingBits[1][0]
                    badCharString = f" by '{badChar}' {unicodedata.name(badChar)}={hex(ord(badChar))}"
                    AppSettings.logger.debug(f"Seems {TNid} '{quoteField}' might not finish at the end of a word—it's followed {badCharString} in '{verse_text}'")
                    self.warnings.append(f"Seems {TNid} '{quoteField}' might not finish at the end of a word—it's followed {badCharString} in '{verse_text}'")
            else: # can't find the given text
                # AppSettings.logger.debug(f"Unable to find {TNid} '{quoteField}' in '{verse_text}'")
                extra_text = " (contains No-Break Space shown as '⍽')" if '\u00A0' in quoteField else ""
                if extra_text: quoteField = quoteField.replace('\u00A0', '⍽')
                self.warnings.append(f"Unable to find {TNid} '{quoteField}'{extra_text} in '{verse_text}'")
    # end of TnPreprocessor.check_original_language_TN_quotes function


    def get_passage(self, B:str, C:str,V:str) -> str:
        """
        Get the information for the given verse out of the appropriate book file.

        Also removes milestones and extra word (\\w) information
        """
        # AppSettings.logger.debug(f"get_passage({B}, {C},{V})…")

        try: book_number = BOOK_NUMBERS[B.lower()]
        except KeyError: # how can this happen?
            AppSettings.logger.error(f"Unable to find book number for '{B} {C}:{V}' in get_passage()")
            book_number = 0

        # Look for OT book first—if not found, look for NT book
        #   NOTE: Lazy way to determine which testament/folder the book is in
        book_path = os.path.join(self.preload_dir, f'{book_number}-{B}.usfm')
        if not os.path.isfile(book_path):
            # NOTE: uW UHB and UGNT repos didn't use to have language code in repo name
            book_path = os.path.join(self.preload_dir, 'hbo_uhb/', f'{book_number}-{B}.usfm')
            if not os.path.isfile(book_path):
                book_path = os.path.join(self.preload_dir, 'uhb/', f'{book_number}-{B}.usfm')
            if not os.path.isfile(book_path):
                book_path = os.path.join(self.preload_dir, 'el-x-koine_ugnt/', f'{book_number}-{B}.usfm')
            if not os.path.isfile(book_path):
                book_path = os.path.join(self.preload_dir, 'ugnt/', f'{book_number}-{B}.usfm')
        if not os.path.isfile(book_path):
            return None
        if self.loaded_file_path != book_path:
            # It's not cached already
            AppSettings.logger.info(f"Reading {book_path}…")
            with open(book_path, 'rt') as book_file:
                self.loaded_file_contents = book_file.read()
            self.loaded_file_path = book_path
            # Do some initial cleaning and convert to lines
            # NOTE: We still have to handle older versions of these files (which might be specified in the manifest)
            self.loaded_file_contents = self.loaded_file_contents \
                                            .replace('\\zaln-e\\*','') \
                                            .replace('\\k-e\\*', '')
            self.loaded_file_contents = re.sub(r'\\zaln-s (.+?)\\\*', '', self.loaded_file_contents) # Remove self-closed \zaln start milestones
            self.loaded_file_contents = re.sub(r'\\k-s (.+?)\\\*', '', self.loaded_file_contents) # Remove self-closed \k start milestones
            self.loaded_file_contents = re.sub(r'\\k-s (.+?)[\n\\]', '', self.loaded_file_contents) # Remove older unclosed \k start milestones
            self.loaded_file_contents = self.loaded_file_contents.split('\n')

        found_chapter = found_verse = False
        verseText = ''
        for book_line in self.loaded_file_contents:
            if not found_chapter and book_line == f'\\c {C}':
                found_chapter = True
                continue
            if found_chapter and not found_verse and book_line.startswith(f'\\v {V}'):
                found_verse = True
                book_line = book_line[3+len(V):] # Delete verse number so below bit doesn't fail

            if found_verse:
                if book_line.startswith('\\v ') or book_line.startswith('\\c '):
                    break # Don't go into the next verse or chapter
                verseText += ('' if book_line.startswith('\\f ') else ' ') + book_line
        verseText = verseText.replace('\\p ', '').strip().replace('  ', ' ')
        # print(f"Got verse text1: '{verseText}'")

        # Remove \w fields (just leaving the actual Bible text words)
        ixW = verseText.find('\\w ')
        while ixW != -1:
            ixEnd = verseText.find('\\w*', ixW)
            if ixEnd != -1:
                field = verseText[ixW+3:ixEnd]
                bits = field.split('|')
                adjusted_field = bits[0]
                verseText = verseText[:ixW] + adjusted_field + verseText[ixEnd+3:]
            else:
                AppSettings.logger.error(f"Missing \\w* in {B} {C}:{V} verseText: '{verseText}'")
                verseText = verseText.replace('\\w ', '', 1) # Attempt to limp on
            ixW = verseText.find('\\w ', ixW+1) # Might be another one
        # print(f"Got verse text2: '{verseText}'")

        # Remove markers belonging to the next verse
        if verseText.endswith('\\p'):
            verseText = verseText[:-2]

        # Remove footnotes
        verseText = re.sub(r'\\f (.+?)\\f\*', '', verseText)
        # Remove alternative versifications
        verseText = re.sub(r'\\va (.+?)\\va\*', '', verseText)
        verseText = re.sub(r'\\ca (.+?)\\ca\*', '', verseText)
        # print(f"Got verse text3: '{verseText}'")

        if '\\' in verseText:
            AppSettings.logger.error(f"get_passage still has backslash in {B} {C}:{V} '{verseText}'")

        # Final clean-up
        return verseText.replace('  ', ' ')
    # end of TnPreprocessor.get_passage function


    def check_support_reference(self, BCV:str, field_id:str, shortReference:str, fullNote:str, repo_owner:str, language_code:str) -> None:
        """
        We expect a full Resource Container link to TA like 'rc://en/ta/man/translate/figs-explicit'
            or else just a TA 'translate' topic like 'figs-explicit'.

        TODO: If we're really not going to use the fullNote parameter, it could be removed.
        """
        # AppSettings.logger.debug(f"check_support_reference({BCV}, {field_id}, {shortReference}, {fullNote}, {repo_owner}, {language_code})…" )

        if shortReference.startswith('rc://'):
            revisedContents = self.fix_tN_links(f'{BCV}-{field_id}', shortReference, repo_owner, language_code)
            AppSettings.logger.info( f"Got back revisedContents='{revisedContents}'")
            AppSettings.logger.info( f"What if (anything) should we be doing next????") # Not really checked or finished (coz no data to test on yet)
        else: # it's just the short form
            file_url = f'https://git.door43.org/{repo_owner}/{language_code}_ta/src/branch/master/translate/{shortReference}/01.md'
            # print(f"check_support_reference: Got file URL='{file_url}'")
            try:
                link_title = self.title_cache[file_url]
                # print(f"check_support_reference: Got from cache link_text='{link_text}' for {file_url}")
            except KeyError:
                link_title = None
                title_file_url = file_url.replace('src','raw').replace('01','title')
                # print(f"check_support_reference title file URL='{title_file_url}'")
                try:
                    file_contents = get_url(title_file_url)
                except Exception as e:
                    AppSettings.logger.debug(f"tN {BCV} fix_linkA fetching {title_file_url} got: {e}")
                    self.warnings.append(f"{BCV} error with tA '{shortReference}' link {title_file_url}: {e}")
                    file_contents = None
                if file_contents:
                    link_title = file_contents
                    if '\n' in link_title or '\r' in link_title or '\t' in link_title:
                        AppSettings.logger.debug(f"tN {BCV} {title_file_url} contains illegal chars: {link_title!r}")
                        self.warnings.append(f"{BCV} unwanted characters in {title_file_url} title: {link_title!r}")
                        link_title = link_title.strip().replace('\n','NL').replace('\r','CR').replace('\t','TAB')
                    if not link_title:
                        AppSettings.logger.debug(f"tN {BCV} got effectively blank title from {title_file_url}")
                        self.warnings.append(f"{BCV} title in {title_file_url} seems blank")
                        link_title = f"BLANK from '{shortReference}'"
                    self.title_cache[file_url] = link_title
                    # print(f"check_support_reference: Added to cache link_text='{link_text}' for {file_url}")
            if not link_title:
                AppSettings.logger.error(f"Bad SupportReference='{shortReference}' at {BCV} ({field_id})")
                self.errors.append(f"Bad SupportReference='{shortReference}' at {BCV} ({field_id})")
            elif f'/{shortReference}' not in fullNote:
                AppSettings.logger.warning(f"Possible tN mismatch at {BCV} ({field_id}) between SupportReference='{shortReference}' and expected link in '{fullNote}'")
                self.warnings.append(f"Possible mismatch at {BCV} ({field_id}) between SupportReference='{shortReference}' and expected link in '{fullNote}'")
            elif not shortReference.startswith('figs-') \
              and not shortReference.startswith('grammar-') \
              and not shortReference.startswith('translate-') \
              and not shortReference.startswith('writing-') \
              and shortReference != 'guidelines-sonofgodprinciples':
                AppSettings.logger.warning(f"Unexpected tN {BCV} ({field_id}) SupportReference='{shortReference}' is not Just-In-Time article")
                self.warnings.append(f"Unexpected {BCV} ({field_id}) SupportReference='{shortReference}' is not Just-In-Time article")
    # end of TnPreprocessor.check_support_reference function


    compiled_re1 = re.compile(r'\[\[https://([^ ]+?)/src/branch/master/([^ .]+?)/01\.md\]\]',
                                            flags=re.IGNORECASE)
    compiled_re2 = re.compile(r'\[\[https://([^ ]+?)/src/branch/master/([^ .]+?)\.md\]\]',
                                            flags=re.IGNORECASE)
    def fix_tN_links(self, BCV:str, content:str, repo_owner:str, language_code:str) -> str:
        """
        For both MD and TSV varieties
        """
        # AppSettings.logger.debug(f"fix_tN_links({BCV}, {content}, {repo_owner}, {language_code})…" )
        # assert content.count('(') == content.count(')')
        # assert content.count('[') == content.count(']')

        # Convert wildcard tA RC links, e.g. rc://*/ta/man/translate/figs-euphemism
        #               => https://git.door43.org/{repo_owner}/LL_ta/src/branch/master/translate/figs-euphemism/01.md
        # content1 = content
        content = re.sub(r'rc://\*/ta/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/{language_code}_ta/src/branch/master/\2/01.md',
                         content, flags=re.IGNORECASE)
        # if content != content1: print(f"1: was {content1}\nnow {content}")
        # Convert non-wildcard tA RC links, e.g. rc://en/ta/man/translate/figs-euphemism
        #               => https://git.door43.org/{repo_owner}/en_ta/src/branch/master/translate/figs-euphemism/01.md
        # content2 = content
        content = re.sub(r'rc://([^/]+)/ta/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/\1_ta/src/branch/master/\3/01.md',
                         content, flags=re.IGNORECASE)
        # if content != content2: print(f"2: was {content2}\nnow {content}")
        # Convert other wildcard RC links, e.g. rc://*/tn/help/1sa/16/02
        #               => https://git.door43.org/{repo_owner}/LL_tn/src/branch/master/1sa/16/02.md
        # content3 = content
        content = re.sub(r'rc://\*/([^/]+)/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/{language_code}_\1/src/branch/master/\3.md',
                         content, flags=re.IGNORECASE)
        # if content != content3: print(f"3: was {content3}\nnow {content}")
        # Convert other non-wildcard RC links, e.g. rc://en/tn/help/1sa/16/02
        #               => https://git.door43.org/{repo_owner}/en_tn/src/branch/master/1sa/16/02.md
        # content4 = content
        content = re.sub(r'rc://([^/]+)/([^/]+)/([^/]+)/([^\s)\]\n$]+)',
                         rf'https://git.door43.org/{repo_owner}/\1_\2/src/branch/master/\4.md',
                         content, flags=re.IGNORECASE)
        # if content != content4: print(f"4: was {content4}\nnow {content}")

        # Fix links to other sections that just have the section name but no 01.md page (preserve http:// links)
        # e.g. See [Verbs](figs-verb) => See [Verbs](#figs-verb)
        # content5 = content
        content = re.sub(r'\]\(([^# :/)]+)\)',
                         r'](#\1)', content)
        # if content != content5: print(f"5: was {content5}\nnow {content}")
        # Convert URLs to links if not already
        # content6 = content
        content = re.sub(r'([^"(\[])((http|https|ftp)://[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])',
                         r'\1[\2](\2)',
                         content, flags=re.IGNORECASE)
        # if content != content6: print(f"6: was {content6}\nnow {content}")
        # URLS wth just www at the start, no http
        # content7 = content
        content = re.sub(r'([^A-Z0-9"(/])(www\.[A-Z0-9/?&_.:=#-]+[A-Z0-9/?&_:=#-])',
                         r'\1[\2](http://\2)',
                         content, flags=re.IGNORECASE)
        # if content != content7: print(f"7: was {content7}\nnow {content}")

        # [[Links inside double-brackets]] => [short-text](url)
        # content8 = content
        # content = re.sub(r'\[\[https://(.+?)/src/branch/master/(.+?)/01\.md\]\]',
        #                  r'[\2](https://\1/src/branch/master/\2/01\.md)',
        #                  content, flags=re.IGNORECASE)
        content_start_index = 0
        bad_file_count = 0
        while (match := TnPreprocessor.compiled_re1.search(content, content_start_index)):
            # print(f"Match8a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match8b: {match.groups()}")
            file_url = f'https://{match.group(1)}/src/branch/master/{match.group(2)}/01.md'
            # print(f"Match8c: file URL='{file_url}'")
            try:
                link_text = self.title_cache[file_url]
            except KeyError:
                repo_type = match.group(1).split('_')[-1].upper() # e.g., TA or TW
                link_text = f'{repo_type}:{match.group(2)}' # default
                title_file_url = file_url.replace('src','raw').replace('01','title')
                # print(f"Match8d: title file URL='{title_file_url}'")
                try:
                    file_contents = get_url(title_file_url)
                except Exception as e:
                    bad_file_count += 1
                    if bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tN {BCV} fix_linkA fetching {title_file_url} got: {e}")
                        self.warnings.append(f"{BCV} error with tA '{match.group(2)}' link {title_file_url}: {e}")
                    link_text = self.title_cache[file_url] = f'INVALID {match.group(2)}'
                    file_contents = None
                if file_contents:
                    link_text = file_contents
                    if '\n' in link_text or '\r' in link_text or '\t' in link_text:
                        AppSettings.logger.debug(f"tN {BCV} {title_file_url} contains illegal chars: {link_text!r}")
                        self.warnings.append(f"{BCV} unwanted characters in {title_file_url} title: {link_text!r}")
                        link_text = link_text.strip().replace('\n','NL').replace('\r','CR').replace('\t','TAB')
                    if not link_text:
                        AppSettings.logger.debug(f"tN {BCV} got effectively blank title from {title_file_url}")
                        self.warnings.append(f"{BCV} title in {title_file_url} seems blank")
                        link_text = f'BLANK from {match.group(2)}'
                    self.title_cache[file_url] = link_text
                    # print(f"cache length = {len(self.title_cache)}")
            # new_link_markdown = f'[{link_text}]({file_url})'
            # print(f"Match8e: New tA link = {new_link_markdown}")
            content = f'{content[:match.start()]}[{link_text}]({file_url}){content[match.end():]}'
            content_start_index = match.start() + 1
        # if content != content8: print(f"8: was {content8}\nnow {content}")
        # assert content.count('(') == content.count(')')
        # assert content.count('[') == content.count(']')

        # content9 = content
        # content = re.sub(r'\[\[https://([^ ]+?)/src/branch/master/([^ .]+?)\.md\]\]',
        #                  r'[\2](https://\1/src/branch/master/\2\.md)',
        #                  content, flags=re.IGNORECASE)
        content_start_index = 0
        bad_file_count = 0
        while (match := TnPreprocessor.compiled_re2.search(content, content_start_index)):
            # print(f"Match9a: {match.start()}:{match.end()} '{content[match.start():match.end()]}'")
            # print(f"Match9b: {match.groups()}")
            file_url = f'https://{match.group(1)}/src/branch/master/{match.group(2)}.md'
            # print(f"Match9c: file URL='{file_url}'")
            try:
                link_text = self.title_cache[file_url]
            except KeyError:
                repo_type = match.group(1).split('_')[-1].upper() # e.g., TA or TW
                link_text = f'{repo_type}:{match.group(2)}' # default
                title_file_url = file_url.replace('src','raw')
                # print(f"Match9d: title file URL='{title_file_url}'")
                try:
                    file_contents = get_url(title_file_url)
                except Exception as e:
                    bad_file_count += 1
                    if bad_file_count < 15 and len(self.warnings) < 200:
                        AppSettings.logger.debug(f"tN {BCV} fix_linkB fetching {title_file_url} got: {e}")
                        self.warnings.append(f"{BCV} error with tW '{match.group(2)}' link {title_file_url}: {e}")
                    link_text = self.title_cache[file_url] = f'INVALID {match.group(2)}'
                    file_contents = None
                if file_contents:
                    try:
                        link_text = file_contents.split('\n',1)[0].lstrip() # Get the first line
                        if link_text.startswith('# '):
                            link_text = link_text[2:]
                        if '\n' in link_text or '\r' in link_text or '\t' in link_text:
                            AppSettings.logger.debug(f"tN {BCV} {title_file_url} contains illegal chars: {link_text!r}")
                            self.warnings.append(f"{BCV} unwanted characters in {title_file_url} title: {link_text!r}")
                            link_text = link_text.strip().replace('\n','NL').replace('\r','CR').replace('\t','TAB')
                        if not link_text:
                            AppSettings.logger.debug(f"tN {BCV} got effectively blank title from {title_file_url}")
                            self.warnings.append(f"{BCV} title in {title_file_url} seems blank")
                            link_text = f'BLANK from {match.group(2)}'
                        self.title_cache[file_url] = link_text
                        # print(f"cache length = {len(self.title_cache)}")
                    except Exception as e:
                        AppSettings.logger.debug(f"tN fix_linkB getting title from {file_contents} got: {e}")
            # new_link_markdown = f'[{link_text}]({file_url})'
            # print(f"Match9e: New tW link = {new_link_markdown}")
            content = f'{content[:match.start()]}[{link_text}]({file_url}){content[match.end():]}'
            content_start_index = match.start() + 1
        # if content != content9: print(f"9: was {content9}\nnow {content}")

        # assert content.count('(') == content.count(')')
        # assert content.count('[') == content.count(']')
        return content
    # end of fix_tN_links function
# end of class TnPreprocessor



class LexiconPreprocessor(Preprocessor):

    # def __init__(self, *args, **kwargs) -> None:
    #     super(LexiconPreprocessor, self).__init__(*args, **kwargs)


    def compile_lexicon_entry(self, project, folder):
        """
        Recursive section markdown creator
        Expects a folder containing only one file: 01.md

        :param project:
        :param str folder:
        :return: markdown str
        """
        # AppSettings.logger.debug(f"compile_lexicon_entry for {project} {folder} …")
        content_folderpath = os.path.join(self.source_dir, project.path, folder)
        file_list = os.listdir(content_folderpath)
        if len(file_list) != 1: # expecting '01.md'
            AppSettings.logger.error(f"Unexpected files in {folder}: {file_list}")
        markdown = "" # f"# {folder}\n" # Not needed coz Strongs number is included inside the file
        content_filepath = os.path.join(content_folderpath, '01.md')
        if os.path.isfile(content_filepath):
            try: content = read_file(content_filepath)
            except Exception as e:
                msg = f"Error reading {os.path.basename(content_folderpath)}/01.md: {e}"
                AppSettings.logger.error(msg)
                self.errors.append(msg)
                content = None
        else:
            msg = f"compile_lexicon_entry couldn't find any files for {folder}"
            AppSettings.logger.error(msg)
            self.warnings.append(msg)
            content = None
        if content:
            # markdown += f'{content}\n\n'
            markdown = f'{content}\n'
        return markdown
    # end of compile_lexicon_entry(self, project, section, level)


    def run(self) -> Tuple[int, List[str]]:
        AppSettings.logger.debug(f"Lexicon preprocessor starting with {self.source_dir} = {os.listdir(self.source_dir)} …")
        for project in self.rc.projects:
            project_path = os.path.join(self.source_dir, project.path)
            print("project_path", project_path)

            AppSettings.logger.debug(f"Lexicon preprocessor: Copying files for '{project.identifier}' …")

            # Even though the .md files won't be converted, they still need to be copied
            #   so they can be linted
            for something in sorted(os.listdir(project_path)):
                # something can be a file or a folder containing the markdown file
                if os.path.isdir(os.path.join(project_path, something)) \
                and something not in LexiconPreprocessor.ignoreDirectories:
                    # Entries are in separate folders (like en_ugl)
                    entry_markdown = self.compile_lexicon_entry(project, something)
                    # entry_markdown = self.fix_entry_links(entry_markdown)
                    write_file(os.path.join(self.output_dir, f'{something}.md'), entry_markdown)
                    self.num_files_written += 1
                elif os.path.isfile(os.path.join(project_path, something)) \
                and something not in LexiconPreprocessor.ignoreFiles \
                and something != 'index.md':
                    # Entries are in the main folder in named .md files
                    # copy(os.path.join(project_path, something), self.output_dir)
                    # entry_markdown = read_file(something)
                    with open(os.path.join(project_path, something), 'rt') as ef:
                        entry_markdown = ef.read()
                    # entry_markdown = self.fix_entry_links(entry_markdown)
                    write_file(os.path.join(self.output_dir, f'{something}.md'), entry_markdown)
                    self.num_files_written += 1

            # Now do the special stuff
            # TODO: Ideally most of the English strings below should be translated for other lexicons
            # Create two index files—one by Strongs number (the original) and one by lemma
            index_filepath = os.path.join(project_path, 'index.md')
            if os.path.isfile(index_filepath):
                with open(index_filepath, 'rt') as ixf:
                    index_markdown = ixf.read()
                index_markdown = self.fix_index_links(index_markdown)
                number_index_markdown = f"# Number index\n\n{index_markdown}"
                write_file(os.path.join(self.output_dir, 'number_index.md'), number_index_markdown)
                self.num_files_written += 1
                word_index_markdown = f"# Word/lemma index\n\n{self.change_index_entries(project_path, index_markdown)}"
                write_file(os.path.join(self.output_dir, 'word_index.md'), word_index_markdown)
                self.num_files_written += 1

            # Copy the README as index.md, adding some extra links at top and bottom
            readme_filepath = os.path.join(self.source_dir, 'README.md')
            if os.path.isfile(readme_filepath):
                with open(readme_filepath, 'rt') as rmf:
                    readme_markdown = rmf.read()
            else:
                AppSettings.logger.error("Lexicon preprocessor cannot find README.md")
                readme_markdown = "No README.md found\n"
            link1 = "* [Lexicon entries (by number)](number_index.html)\n"
            link2 = "* [Lexicon entries (by word)](word_index.html)\n"
            readme_markdown = f"{link1}{link2}\n\n{readme_markdown}\n{link1}{link2}"
            write_file(os.path.join(self.output_dir, 'index.md'), readme_markdown)
            self.num_files_written += 1

        if self.num_files_written == 0:
            AppSettings.logger.error("Lexicon preprocessor didn't write any markdown files")
            self.errors.append("No lexicon source files discovered")
        else:
            AppSettings.logger.debug(f"Lexicon preprocessor wrote {self.num_files_written} markdown files with {len(self.errors)} errors and {len(self.warnings)} warnings")

        str_list = str(os.listdir(self.output_dir))
        str_list_adjusted = str_list if len(str_list)<1500 \
                                else f'{str_list[:1000]} …… {str_list[-500:]}'
        AppSettings.logger.debug(f"Lexicon preprocessor returning with {self.output_dir} = {str_list_adjusted}")
        return self.num_files_written, self.errors + self.warnings + (self.messages if self.errors or self.warnings else [])
    # end of LexiconPreprocessor run()


    def fix_index_links(self, content):
        """
        Changes the actual links to point to the original .md files.
        """
        # AppSettings.logger.debug("LexiconPreprocessor.fix_index_links(…)…")

        newLines:List[str] = []
        for line in content.split('\n'):
            # print("line:", line)
            if '](' in line:
                bits = line.split('](', 1)
                # print("bits", bits)
                assert bits[0][0]=='[' and bits[1][-1]==')'
                strongs, filenameOrFolder = bits[0][1:], bits[1][:-1]
                # print("strongs", strongs, "filenameOrFolder", filenameOrFolder)
                assert filenameOrFolder.startswith('./')
                filenameOrFolder = f"{self.commit_url}/content/{filenameOrFolder[2:]}" \
                                    .replace('/commit/', '/raw/commit/') \
                                    .replace('/commit/master/', '/branch/master/')
                # print("filenameOrFolder", filenameOrFolder)
                line = f"[{strongs[:-3] if strongs.endswith('.md') else strongs}]" \
                       f"({filenameOrFolder}{'/01.md' if not filenameOrFolder.endswith('.md') else ''})"
            newLines.append(line)
        content = '\n'.join(newLines)
        return content
    # end of LexiconPreprocessor fix_index_links(content)


    def change_index_entries(self, project_path:str, content:str) -> str:
        """
        """
        # AppSettings.logger.debug(f"LexiconPreprocessor.change_index_entries({project_path}, …)…")

        # Change Strongs numbers to lemma entries
        newLines:List[str] = []
        for line in content.split('\n'):
            # print("line:", line)
            if '](' in line:
                bits = line.split('](', 1)
                # print("bits", bits)
                assert bits[0][0]=='[' and bits[1][-1]==')'
                strongs, fileURL = bits[0][1:], bits[1][:-1]
                # print("strongs", strongs, ' ', "fileURL", fileURL)
                fileURLbits = fileURL.split('/')
                filepath = os.path.join(project_path, fileURLbits[-1])
                # print("filepath1", filepath)
                if not os.path.isfile(filepath):
                    filepath = os.path.join(project_path, fileURLbits[-2], fileURLbits[-1])
                    # print("filepath2", filepath)
                title = None
                try:
                    with open(filepath, 'rt') as lex_file:
                        lex_content = lex_file.read()
                except FileNotFoundError:
                    AppSettings.logger.error(f"LexiconPreprocessor.change_index_entries could not find {filepath}")
                    self.warnings.append(f"No lexicon entry file found for {strongs}")
                    lex_content = None
                    title = "-BAD-"
                if lex_content and lex_content[0]=='#' and lex_content[1]==' ':
                    title = lex_content[2:8].replace('\n', ' ').replace(' ', ' ') # non-break space
                # print("title", repr(title))
                if lex_content and title is None: # Why?
                    AppSettings.logger.error("LexiconPreprocessor.change_index_entries could not find lemma string")
                    title = strongs
                line = f"[{title:6}]({fileURL})"
            newLines.append(line)
        content = '\n'.join(newLines)
        return content
    # end of LexiconPreprocessor change_index_entries(project_path, content)
# end of class LexiconPreprocessor
