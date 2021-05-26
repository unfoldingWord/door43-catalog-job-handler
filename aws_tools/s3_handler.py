import os
import json
import boto3
import botocore
from boto3.session import Session
from typing import Any, Optional



class S3Handler:
    def __init__(self, bucket_name:Optional[str]=None,
                    aws_access_key_id:Optional[str]=None, aws_secret_access_key:Optional[str]=None,
                    aws_region_name:str='us-west-2') -> None:
        self.bucket_name = bucket_name
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_region_name = aws_region_name
        self.bucket:Optional[Any] = None
        self.client:Optional[Any] = None
        self.resource:Optional[Any] = None
        self.setup_resources()


    def setup_resources(self) -> None:
        if self.aws_access_key_id and self.aws_secret_access_key:
            session = Session(aws_access_key_id=self.aws_access_key_id,
                              aws_secret_access_key=self.aws_secret_access_key,
                              region_name=self.aws_region_name)
            self.resource = session.resource('s3')
            self.client = session.client('s3')
        else:
            self.resource = boto3.resource('s3',
                                           aws_access_key_id=self.aws_access_key_id,
                                           aws_secret_access_key=self.aws_secret_access_key,
                                           region_name=self.aws_region_name)
            self.client = boto3.client('s3',
                                       aws_access_key_id=self.aws_access_key_id,
                                       aws_secret_access_key=self.aws_secret_access_key,
                                       region_name=self.aws_region_name)

        self.bucket = None
        if self.bucket_name:
            self.bucket = self.resource.Bucket(self.bucket_name)


    def download_file(self, key:str, local_filepath:str) -> None:
        """
        Download file from S3 bucket. Similar to s3.download_file except that does
        not play nicely with moto, this however, does.
        :param string key: object to download
        :param string local_file: file to download to
        """
        body = self.resource.Object(bucket_name=self.bucket_name, key=key).get()['Body']
        with open(local_filepath, 'wb') as f:
            for chunk in iter(lambda: body.read(1024), b''):
                f.write(chunk)


    def copy(self, from_key:str, from_bucket:Optional[str]=None,
                    to_key:Optional[str]=None, catch_exception:bool=True) -> Optional[object]:
        if not to_key:
            to_key = from_key
        if not from_bucket:
            from_bucket = self.bucket_name

        if catch_exception:
            try:
                return self.resource.Object(bucket_name=self.bucket_name, key=to_key).copy_from(
                    CopySource='{0}/{1}'.format(from_bucket, from_key))
            except:
                return False
        else:
            return self.resource.Object(bucket_name=self.bucket_name, key=to_key).copy_from(
                CopySource='{0}/{1}'.format(from_bucket, from_key))


    def upload_file(self, path:str, key:str, cache_time:int=600, content_type:Optional[str]=None) -> None:
        """
        Upload file to S3 storage. Similar to the s3.upload_file, however, that
        does not work nicely with moto, whereas this function does.
        :param string path: file to upload
        :param string key: name of the object in the bucket
        """
        from general_tools.file_utils import get_mime_type
        #from app_settings.app_settings import AppSettings
        #AppSettings.logger.debug(f"s3_handler.upload_file({path}, {key}, {cache_time}, {content_type})")
        assert 'http' not in key.lower()

        with open(path, 'rb') as f:
            binary = f.read()
        if content_type is None:
            mime_type = get_mime_type(path)
            content_type = mime_type # Let browser figure out the encoding
            # content_type = f'{mime_type}; charset=utf-8' if 'usfm' in mime_type \
            #                 else mime_type # RJH added charset Oct2019
        # from app_settings.app_settings import AppSettings
        # AppSettings.logger.debug(f"Uploading {path} to S3 {key} with cache_time={cache_time} content_type='{content_type}'…")
        # AppSettings.logger.debug(f"Bucket is {self.bucket}")
        self.bucket.put_object(
            Key=key,
            Body=binary,
            ContentType=content_type,
            CacheControl=f'max-age={cache_time}'
        )


    def get_object(self, key:str):
        return self.resource.Object(bucket_name=self.bucket_name, key=key)


    def redirect(self, key:str, location:str) -> None:
        self.bucket.put_object(Key=key, WebsiteRedirectLocation=location, CacheControl='max-age=0')


    def get_file_contents(self, key:str, catch_exception:bool=True) -> Optional[str]:
        if catch_exception:
            try:
                return self.get_object(key).get()['Body'].read()
            except:
                return None
        else:
            return self.get_object(key).get()['Body'].read()


    def get_json(self, key:str, catch_exception:bool=True):
        if catch_exception:
            try:
                return json.loads(self.get_file_contents(key))
            except:
                return {}
        else:
            return json.loads(self.get_file_contents(key, catch_exception))


    def get_objects(self, prefix:Optional[str]=None, suffix:Optional[str]=None):
        filtered = []
        objects = self.bucket.objects.filter(Prefix=prefix)
        if objects:
            if suffix:
                for obj in objects:
                    if obj.key.endswith(suffix):
                        filtered.append(obj)
            else:
                filtered = objects
        return filtered


    def delete_file(self, key:str, catch_exception:bool=True):
        if catch_exception:
            try:
                return self.resource.Object(bucket_name=self.bucket_name, key=key).delete()
            except:
                return False
        else:
            return self.resource.Object(bucket_name=self.bucket_name, key=key).delete()
