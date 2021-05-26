# DOOR43 RQ Settings

from os import getenv, environ

# NOTE: Most of these variable names are defined by the rq package

# Read the redis URL from an environment variable
REDIS_URL = getenv('REDIS_URL', 'redis://127.0.0.1:6379')
# You can also specify the Redis DB to use
# REDIS_HOST = 'redis.example.com'
# REDIS_PORT = 6380
# REDIS_DB = 3
# REDIS_PASSWORD = 'very secret'

# Queues to listen on
#QUEUES = ['high', 'normal', 'low'] # NOTE: The first queue in the list is processed first
ENQUEUE_NAME = 'Door43_webhook' # Becomes the queue name -- MUST match enqueueMain.py in door43-enqueue-job
CALLBACK_SUFFIX = '_callback'
prefix = getenv('QUEUE_PREFIX', '') # Gets (optional) QUEUE_PREFIX environment variable -- set to 'dev-' for development
QUEUE_NAME_SUFFIX = '' # Used to switch to a different queue, e.g., '_1'
webhook_queue_name = prefix + ENQUEUE_NAME + QUEUE_NAME_SUFFIX
callback_queue_name = prefix + ENQUEUE_NAME + CALLBACK_SUFFIX + QUEUE_NAME_SUFFIX
QUEUES = [callback_queue_name, webhook_queue_name] # Callback (i.e., finishing off jobs) is higher priority

# If you're using Sentry to collect your runtime exceptions, you can use this
# to configure RQ for it in a single step
# The 'sync+' prefix is required for raven: https://github.com/nvie/rq/issues/350#issuecomment-43592410
#SENTRY_DSN = 'sync+http://public:secret@example.com/1'

# Our stuff
# This is placed here so it fails at start-up if the environment variable is missing
gogs_user_token = environ['GOGS_USER_TOKEN']

debug_mode_flag = getenv('DEBUG_MODE', '')

# long_prefix = 'develop.' if prefix else ''
# tx_post_url = 'http://127.0.0.1:8090/' if prefix and debug_mode_flag \
#                 else f'https://git.door43.org/{prefix}tx/'
if prefix:
    if debug_mode_flag:
        tx_post_url = 'http://127.0.0.1:8090/'
    else: # development on AWS
        tx_post_url = 'https://develop.door43.org/tx/'
else: # production
    tx_post_url = 'https://git.door43.org/tx/'

REDIS_JOB_LIST = f'{prefix}Door43_outstanding_jobs'
