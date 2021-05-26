XXXdoc: clean_doc
	echo 'building docs…'
	cd docs && sphinx-apidoc --force -M -P -e -o source/ ../enqueue
	cd docs && make html

XXXclean_doc:
	echo 'cleaning docs…'
	cd docs && rm -f source/enqueue
	cd docs && rm -f source/enqueue*.rst

dependencies:
	pip3 install --upgrade pip
	pip3 install --requirement requirements.txt

testDependencies:
	pip3 install --upgrade pip
	pip3 install --requirement test_requirements.txt
dependenciesTest:
	pip3 install --upgrade pip
	pip3 install --requirement test_requirements.txt

# NOTE: The following environment variables are expected to be set for testing:
#	DB_ENDPOINT
#	TX_DATABASE_PW
#	AWS_ACCESS_KEY_ID
#	AWS_SECRET_ACCESS_KEY
#	GOGS_USER_TOKEN
checkEnvVariables:
	@ if [ -z "${DB_ENDPOINT}" ]; then \
		echo "Need to set DB_ENDPOINT"; \
		exit 1; \
	fi
	@ if [ -z "${TX_DATABASE_PW}" ]; then \
		echo "Need to set TX_DATABASE_PW"; \
		exit 1; \
	fi
	@ if [ -z "${AWS_ACCESS_KEY_ID}" ]; then \
		echo "Need to set AWS_ACCESS_KEY_ID"; \
		exit 1; \
	fi
	@ if [ -z "${AWS_SECRET_ACCESS_KEY}" ]; then \
		echo "Need to set AWS_SECRET_ACCESS_KEY"; \
		exit 1; \
	fi
	@ if [ -z "${GOGS_USER_TOKEN}" ]; then \
		echo "Need to set GOGS_USER_TOKEN"; \
		exit 1; \
	fi

# NOTE: The following environment variables are optional:
#	REDIS_URL (can be omitted for testing if a local instance is running)
#	DEBUG_MODE (can be set to any non-blank string to run in debug mode for testing)
#	GRAPHITE_HOSTNAME (defaults to localhost if missing)
#	QUEUE_PREFIX (defaults to '', set to dev- for testing)

test:
	# You should have already installed the testDependencies before this
	# To do individual tests:
	#		discover -s = start directory
	#		discover -p = filename pattern
	#	TEST_MODE="TEST" python3 -m unittest discover -s tests/client_tests
	#	TEST_MODE="TEST" python3 -m unittest discover -p testXXX.py
	TEST_MODE="TEST" python3 -m unittest discover -s tests/

info:
	# Runs the rq info display with a one-second refresh
	rq info --interval 1

runDev: checkEnvVariables
	# This runs the rq job handler
	#   which removes and then processes jobs from the local redis dev- queue
	QUEUE_PREFIX="dev-" rq worker --config rq_settings --name D43_Dev_JobHandler

runDevDebug: checkEnvVariables
	# This runs the rq job handler
	#   which removes and then processes jobs from the local redis dev- queue
	QUEUE_PREFIX="dev-" DEBUG_MODE="true" rq worker --config rq_settings --name D43_Dev_JobHandler

run:
	# This runs the rq job handler
	#   which removes and then processes jobs from the production redis queue
	# TODO: Can the AWS redis url go in here (i.e., is it public)?
	REDIS_URL="dadada" rq worker --config rq_settings --name D43_JobHandler

imageDev:
	docker build --file Dockerfile-developBranch --tag unfoldingword/door43_job_handler:develop .

imageMaster:
	docker build --file Dockerfile-masterBranch --tag unfoldingword/door43_job_handler:master .

pushDevImage:
	# Expects to be already logged into Docker, e.g., docker login -u $(DOCKER_USERNAME)
	docker push unfoldingword/door43_job_handler:develop

pushMasterImage:
	# Expects to be already logged into Docker, e.g., docker login -u $(DOCKER_USERNAME)
	docker push unfoldingword/door43_job_handler:master

# NOTE: To test the container (assuming that the confidential environment variables are already set in the current environment) use:
# 	docker run --env DB_ENDPOINT --env TX_DATABASE_PW --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY --env QUEUE_PREFIX=dev- --env DEBUG_MODE=True --env REDIS_URL=<redis_url> --net="host" --name door43_job_handler --rm door43_job_handler


# NOTE: To run the container in production use with the desired values:
#     	docker run --env DB_ENDPOINT --env TX_DATABASE_PW=<tx_db_pw> --env AWS_ACCESS_KEY_ID=<access_key> --env AWS_SECRET_ACCESS_KEY=<sa_key> --env GRAPHITE_HOSTNAME=<graphite_hostname> --env REDIS_URL=<redis_url> --net="host" --name door43_job_handler --rm door43_job_handler

connect:
	# Gives a shell on the running container -- Note: no bash shell available
	docker exec -it `docker inspect --format="{{.Id}}" door43_job_handler` sh

connectDev:
	# Gives a shell on the running container -- Note: no bash shell available
	docker exec -it `docker inspect --format="{{.Id}}" dev-door43_job_handler` sh
