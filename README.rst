master:

.. image:: https://travis-ci.org/unfoldingWord-dev/door43-job-handler.svg?branch=master
    :alt: Build Status
    :target: https://travis-ci.org/unfoldingWord-dev/door43-job-handler?branch=master

.. image:: https://coveralls.io/repos/github/unfoldingWord-dev/door43-job-handler/badge.svg?branch=master
    :alt: Coveralls
    :target: https://coveralls.io/github/unfoldingWord-dev/door43-job-handler?branch=master

develop:

.. image:: https://travis-ci.org/unfoldingWord-dev/door43-job-handler.svg?branch=develop
    :alt: Build Status
    :target: https://travis-ci.org/unfoldingWord-dev/door43-job-handler?branch=develop

.. image:: https://coveralls.io/repos/github/unfoldingWord-dev/door43-job-handler/badge.svg?branch=develop
    :alt: Coveralls
    :target: https://coveralls.io/github/unfoldingWord-dev/door43-job-handler?branch=develop


door43-job-handler (part of tx platform)
========================================

This program accepts jobs from a rq/redis queue (placed there by the
[door43-enqueue-job](https://github.com/unfoldingWord-dev/door43-enqueue-job)) program.

Setup
-----

Requires Python 3.6 or later (Python2 compatibility has been removed.)

Satisfy basic dependencies:

.. code-block:: bash

    git clone https://github.com/unfoldingWord-dev/door43-job-handler.git
    OR/ with ssh: git clone git@github.com:unfoldingWord-dev/door43-job-handler.git
    sudo apt-get install python3-pip

We recommend you create a Python virtual environment to help manage Python package dependencies:

.. code-block:: bash

    cd door43-job-handler
    python3 -m venv myVenv

Now load that virtual environment and install dependencies:

.. code-block:: bash

    source myVenv/bin/activate
    make dependencies

Deployment
----------

Travis-CI is hooked to from GitHub to automatically test commits to both the `develop`
and `master` branches, and on success, to build containers (tagged with those branch names)
that are pushed to [DockerHub](https://hub.docker.com/u/unfoldingword/).

To fetch the container use something like:

.. code-block:: bash

    docker pull --all-tags unfoldingword/door43_job_handler
or

.. code-block:: bash

    docker pull unfoldingword/door43_job_handler:develop

To view downloaded images and their tags:

.. code-block:: bash

    docker images

To test the container (assuming that the confidential environment variables are already set in the current environment) use:

.. code-block:: bash

    docker run --env DB_ENDPOINT --env TX_DATABASE_PW --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY --env QUEUE_PREFIX=dev- --env DEBUG_MODE=True --env REDIS_URL="redis://<redis_hostname>:6379" --net="host" --name dev-door43_job_handler --rm unfoldingword/door43_job_handler:develop

or if not (and adding optional GRAPHITE_HOSTNAME):

.. code-block:: bash

    docker run --env DB_ENDPOINT=<db_endpoint> --env TX_DATABASE_PW=<tx_db_pw> --env AWS_ACCESS_KEY_ID=<access_key> --env AWS_SECRET_ACCESS_KEY=<sa_key> --env QUEUE_PREFIX=dev- --env DEBUG_MODE=True GRAPHITE_HOSTNAME=<graphite_hostname> --env REDIS_URL="redis://<redis_hostname>:6379" --env --net="host" --name dev-door43_job_handler --rm unfoldingword/door43_job_handler:develop

NOTE: --rm automatically removes the container from the docker daemon when it exits
            (it doesn't delete the pulled image from disk)

To run the container in production use with the desired values:

.. code-block:: bash

    docker run --env DB_ENDPOINT=<db_endpoint> --env TX_DATABASE_PW=<tx_db_pw> --env AWS_ACCESS_KEY_ID=<access_key> --env AWS_SECRET_ACCESS_KEY=<sa_key> --env GRAPHITE_HOSTNAME=<graphite_hostname> --env REDIS_URL="redis://<redis_hostname>:6379" --net="host" --name door43_job_handler --detach --rm unfoldingword/door43_job_handler:master

Running containers can be viewed with (or append --all to see all containers):

.. code-block:: bash

    docker ps

The output log can be viewed on the (AWS EC2) host machine at:
    /var/lib/docker/containers/<containerID>/<containerID>-json.log

You can connect to a shell inside the container with commands like:

.. code-block:: bash

	# Gives a shell on the running container -- Note: no bash shell available
	docker exec -it `docker inspect --format="{{.Id}}" door43_job_handler` sh
	docker exec -it `docker inspect --format="{{.Id}}" dev-door43_job_handler` sh

The container can be stopped with a command like:

.. code-block:: bash

    docker stop dev-door43_job_handler
or using the full container name:

.. code-block:: bash

    docker stop unfoldingword/door43_job_handler:develop

The production container will be deployed to the unfoldingWord AWS EC2 instance, where
[Watchtower](https://github.com/v2tec/watchtower) will automatically check for, pull, and run updated containers.
