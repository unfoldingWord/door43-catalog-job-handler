import logging
import os
import re
import sys
import boto3
import watchtower

from rq_settings import debug_mode_flag, use_watchtower


# TODO: Investigate if this AppSettings (was tx-Manager App) class still needs to be resetable now
def resetable(cls):
    cls._resetable_cache_ = cls.__dict__.copy()
    return cls


def reset_class(cls):
    cache = cls._resetable_cache_  # raises AttributeError on class without decorator
    # Remove any class variables that weren't in the original class as first instantiated
    for key in [key for key in cls.__dict__ if key not in cache and key != '_resetable_cache_']:
        delattr(cls, key)
    # Reset the items to original values
    for key, value in cache.items():
        try:
            if key != '_resetable_cache_':
                setattr(cls, key, value)
        except AttributeError:  # When/Why would we get this?
            pass
    cls.dirty = False


def setup_logger(logger, watchtower_log_handler, level):
    """
    Logging for the app, and turn off boto logging.
    Set here so automatically ready for any logging calls
    :param logger:
    :param level:
    :return:
    """
    for h in logger.handlers:
        logger.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
    logger.addHandler(sh)
    logger.addHandler(watchtower_log_handler)
    logger.setLevel(level)
    # Change these loggers to only report errors:
    logging.getLogger('boto3').setLevel(logging.ERROR)
    logging.getLogger('botocore').setLevel(logging.ERROR)


@resetable
class AppSettings:
    """
    For all things used for by this app, from DB connection to global handlers
    """
    _resetable_cache_ = {}
    name = 'Door43-Catalog-Job-Handler'  # Only used for logging and for testing AppSettings resets
    dirty = False

    # Stage Variables, defaults
    prefix = ''
    dcs_user = os.getenv('DCS_USER', None)
    dcs_password = os.getenv('DCS_PASSWORD', None)
    dcs_domain = os.getenv('DCS_DOMAIN', 'git.door43.org')

    # Prefixing vars
    # All variables that we change based on production, development and testing environments.
    prefixable_vars = ['name']

    # AWS credentials—get the secret ones from environment variables
    aws_region_name = 'us-west-2'
    aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID', None)
    aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY', None)

    # Logger
    logger = logging.getLogger(name)

    # Delay the rest of the logger setup until we get our prefixins

    def __init__(self, **kwargs):
        """
        Using init to set the class variables with AppSettings(var=value)
        :param kwargs:
        """
        self.init(**kwargs)

    @classmethod
    def init(cls, reset=True, **kwargs):
        """
        Class init method to set all vars
        :param bool reset:
        :param kwargs:
        """
        if cls.dirty and reset:
            reset_class(AppSettings)
        if 'prefix' in kwargs and kwargs['prefix'] != cls.prefix:
            cls.__prefix_vars(kwargs['prefix'])
        cls.set_vars(**kwargs)
        test_mode_flag = os.getenv('TEST_MODE', '')
        travis_flag = os.getenv('TRAVIS_BRANCH', '')
        log_group_name = f"{'' if test_mode_flag or travis_flag else cls.prefix}tX" \
                         f"{'_DEBUG' if debug_mode_flag else ''}" \
                         f"{'_TEST' if test_mode_flag else ''}" \
                         f"{'_TravisCI' if travis_flag else ''}"
        boto3_client = boto3.client("logs", aws_access_key_id=cls.aws_access_key_id,
                            aws_secret_access_key=cls.aws_secret_access_key,
                            region_name=cls.aws_region_name)
        
        cls.watchtower_log_handler = None
        if use_watchtower:
            cls.watchtower_log_handler = watchtower.CloudWatchLogHandler(boto3_client=boto3_client,
                                                    use_queues=False,
                                                    log_group_name=log_group_name,
                                                    stream_name=cls.name)

        setup_logger(cls.logger, cls.watchtower_log_handler,
                     logging.DEBUG if debug_mode_flag else logging.INFO)
        cls.logger.debug(
            f"Logging to AWS CloudWatch group '{log_group_name}' using key '…{cls.aws_access_key_id[-2:]}'.")

    @classmethod
    def __prefix_vars(cls, prefix):
        """
        Prefixes any variables in AppSettings.prefixable_variables. This includes URLs
        :return:
        """
        url_re = re.compile(r'^(https*://)')  # Current prefix in URLs
        for var in cls.prefixable_vars:
            value = getattr(AppSettings, var)
            if re.match(url_re, value):
                value = re.sub(url_re, r'\1{0}'.format(prefix), value)
            else:
                value = prefix + value
            setattr(AppSettings, var, value)
        cls.prefix = prefix
        cls.dirty = True

    @classmethod
    def set_vars(cls, **kwargs):
        # Sets all the given variables for the class, and then marks it as dirty
        for var, value in kwargs.items():
            if hasattr(AppSettings, var):
                setattr(AppSettings, var, value)
                cls.dirty = True

    @classmethod
    def close_logger(cls):
        # Flushes queued log entries to AWS
        cls.watchtower_log_handler.close()
