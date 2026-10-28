"""
MongoDB ReplicaSet Manager for Docker Swarm

AUTHOR: Røb (jackietreehorn01@protonmail.com)
VERSION: 1.04
REPO: https://github.com/BitWise-0x/MongoDB-ReplicaSet-Manager

DESCRIPTION:
    Automated MongoDB ReplicaSet management for Docker Swarm environments.
    Handles initialization, redeployment, scaling, and primary failover.

REQUIREMENTS:
    - MongoDB 8.0.x (compatible with 7.x/8.x)
    - PyMongo >= 4.15,<5
    - Docker SDK >= 7,<9

CORE FEATURES:
    • Fresh Deployment: Initializes replica set and creates admin/app users
    • Redeployment: Detects IP changes and updates configuration immediately
    • Dynamic Scaling: Adds/removes nodes during runtime
    • Primary Failover: Monitors and handles primary election failures
    • Smart Retry Logic: Handles MongoDB startup transitional states

USAGE:
    Deploy via docker-compose with global MongoDB service (max 1 replica per node)
    Configure via environment variables (see get_required_env_variables)
"""

from pymongo.errors import PyMongoError, OperationFailure, ServerSelectionTimeoutError
import docker
from contextlib import contextmanager
import logging
import traceback
import os
import hashlib
import re
import socket
import ssl
import pymongo as pm
import sys
import time
import json
import backoff

# Track nodes we've already warned about expected auth failures to avoid log spam
AUTH_WARNED_NODES = set()


# ANSI color codes for logging
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    RESET = '\033[0m'


class ColoredFormatter(logging.Formatter):
    """Custom formatter to add colors to log levels and key information."""

    COLORS = {
        'DEBUG': Colors.CYAN,
        'INFO': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED,
        'CRITICAL': Colors.RED + Colors.BOLD,
    }

    def format(self, record):
        # Add color to log level
        level_color = self.COLORS.get(record.levelname, Colors.WHITE)
        colored_level = f"{level_color}{record.levelname}{Colors.RESET}"

        # Format the message
        message = super().format(record)
        message = message.replace(record.levelname, colored_level, 1)

        # Highlight important information
        if "PRIMARY" in message:
            message = message.replace("PRIMARY", f"{Colors.BOLD}{Colors.MAGENTA}PRIMARY{Colors.RESET}")
        if "SECONDARY" in message:
            message = message.replace("SECONDARY", f"{Colors.CYAN}SECONDARY{Colors.RESET}")
        if "replica" in message.lower() and "set" in message.lower():
            message = message.replace("ReplicaSet", f"{Colors.BOLD}ReplicaSet{Colors.RESET}")
            message = message.replace("replicaset", f"{Colors.BOLD}replicaset{Colors.RESET}")
            message = message.replace("replicaSet", f"{Colors.BOLD}replicaSet{Colors.RESET}")

        # Highlight IP addresses (simple pattern)
        import re
        ip_pattern = r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
        message = re.sub(ip_pattern, f"{Colors.YELLOW}\\g<0>{Colors.RESET}", message)

        return message


# Added my own Docker context manager since it isn't supported natively in the Docker SDK (helps in closing client sessions)
@contextmanager
def docker_client():
    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


def get_required_env_variables():
    REQUIRED_VARS = [
        'OVERLAY_NETWORK_NAME',
        'MONGO_SERVICE_NAME',
        'REPLICASET_NAME',
        'MONGO_PORT',
        'MONGO_ROOT_USERNAME',
        'MONGO_ROOT_PASSWORD',
        'INITDB_DATABASE',
        'INITDB_USER',
        'INITDB_PASSWORD',
        'MONGO_CERTIFICATE_FILE',
        'MONGO_CA_FILE'
    ]
    envs = {}
    for rv in REQUIRED_VARS:
        env_value = next((os.environ[key] for key in os.environ if key.lower() == rv.lower()), None)
        file_value = next((os.environ[key] for key in os.environ if key.lower() == (rv + '_file').lower()), None)
        if env_value is not None and file_value is not None:
            raise RuntimeError("Set either {} or {}_FILE, not both.".format(rv, rv))
        if file_value is not None:
            try:
                with open(file_value, encoding='utf-8') as secret_file:
                    env_value = secret_file.read().rstrip('\r\n')
            except OSError as error:
                raise RuntimeError("Could not read secret file for {}: {}".format(rv, error)) from error
        envs[rv.lower()] = env_value

    if not all(value is not None for value in envs.values()):
        missing_vars = [var for var, value in envs.items() if value is None]
        raise RuntimeError(f"Missing required ENV variables: {missing_vars}")

    envs['mongo_port'] = int(envs['mongo_port']) if envs['mongo_port'] is not None else None
    raw_horizon_suffix = os.environ.get('MONGO_HORIZON_SUFFIX', '').strip()
    envs['mongo_horizon_suffix'] = raw_horizon_suffix.strip('.').lower()
    envs['mongo_horizon_name'] = os.environ.get('MONGO_HORIZON_NAME', 'external').strip()

    suffix_labels = envs['mongo_horizon_suffix'].split('.')
    if raw_horizon_suffix != envs['mongo_horizon_suffix']:
        raise RuntimeError('MONGO_HORIZON_SUFFIX must be lowercase and must not have a trailing dot.')
    if len(suffix_labels) < 3 or not envs['mongo_horizon_name'] or any(
        not label or len(label) > 63 or not label[0].isalnum() or not label[-1].isalnum()
        or any(not (char.isalnum() or char == '-') for char in label)
        for label in suffix_labels
    ):
        raise RuntimeError('MONGO_HORIZON_SUFFIX must be env.domain.tld and MONGO_HORIZON_NAME must be a DNS label.')

    return envs


def get_mongo_service(dc, mongo_service_name):
    mongo_services = [s for s in dc.services.list() if s.name == mongo_service_name]
    if len(mongo_services) > 1:
        raise RuntimeError(f"Unexpected: multiple docker services with the name '{mongo_service_name}' found: {mongo_services}")

    if not mongo_services:
        raise RuntimeError(f"Error: Could not find mongo service with name {mongo_service_name}. "
                           "Did you correctly deploy the stack with the right service name, env variables, etc?")

    return mongo_services[0]


def get_assigned_nodes(mongo_service):
    """
    Retrieve nodes assigned to the MongoDB service considering Docker assignment constraints.
    This function does not check node states or availability.

    :param mongo_service: The MongoDB service object.
    :return: set of node IDs assigned to the service.
    """
    with docker_client() as dc:
        service_tasks = mongo_service.tasks()
        assigned_nodes = set()
        for task in service_tasks:
            node_id = task['NodeID']
            assigned_nodes.add(node_id)
        return assigned_nodes


def is_service_up(mongo_service):
    """
    Check if the MongoDB service is up and running with all expected replicas.
    This function considers the number of running tasks, node availability, and state.
    It ensures that the service runs on nodes that are active and not down.

    :param mongo_service: The MongoDB service object.
    :return: Boolean indicating whether the service is fully up.
    """
    logger = logging.getLogger(__name__)

    if mongo_service is None:
        return False

    running_tasks_count = len(get_running_tasks(mongo_service))
    assigned_nodes = get_assigned_nodes(mongo_service)

    with docker_client() as dc:
        active_nodes = {node.id for node in dc.nodes.list() if node.attrs['Spec']['Availability'] == 'active' and node.attrs['Status']['State'] != 'down'}

    # Filtering assigned nodes to include only those that are active
    assigned_active_nodes = active_nodes.intersection(assigned_nodes)

    mode = mongo_service.attrs['Spec']['Mode']
    if 'Replicated' in mode:
        expected_replicas = mode['Replicated']['Replicas']
    elif 'Global' in mode:
        expected_replicas = len(assigned_active_nodes)
        logger.info("Expected number of mongodb nodes: {} | Remaining to start: {}".format(expected_replicas, expected_replicas - running_tasks_count))
    else:
        return False

    return running_tasks_count == expected_replicas


@backoff.on_exception(backoff.expo, docker.errors.APIError, max_tries=10)
def get_running_tasks(mongo_service, desired_state="running"):
    tasks = []
    for t in mongo_service.tasks(filters={'desired-state': desired_state}):
        if t['Status']['State'] == desired_state:
            tasks.append(t)
    return tasks


def get_tasks_ips(tasks, overlay_network_name):
    tasks_ips = []
    for t in tasks:
        for n in t['NetworksAttachments']:
            if n['Network']['Spec']['Name'] == overlay_network_name:
                ip = n['Addresses'][0].split('/')[0]  # clean prefix from ip
                tasks_ips.append(ip)
    return tasks_ips


def get_task_member_labels(tasks, overlay_network_name):
    """Map each running Mongo task's overlay IP to its numeric Swarm mongo label."""
    with docker_client() as dc:
        node_labels = {
            node.id: node.attrs.get('Spec', {}).get('Labels') or {}
            for node in dc.nodes.list()
        }

    member_labels = {}
    for task in tasks:
        node_id = task.get('NodeID')
        label = node_labels.get(node_id, {}).get('mongo')
        try:
            member_number = int(label)
        except (TypeError, ValueError):
            raise RuntimeError(
                "Mongo task on node {} has no numeric 'mongo' label.".format(node_id)
            )
        if member_number < 1:
            raise RuntimeError("Node label 'mongo' must be a positive integer.")

        for network in task.get('NetworksAttachments', []):
            if network.get('Network', {}).get('Spec', {}).get('Name') == overlay_network_name:
                ip = network['Addresses'][0].split('/')[0]
                if ip in member_labels:
                    raise RuntimeError("Multiple Mongo tasks have the same overlay IP {}.".format(ip))
                member_labels[ip] = member_number
                break

    if len(set(member_labels.values())) != len(member_labels):
        raise RuntimeError("Each Mongo node must have a unique numeric 'mongo' label.")
    return member_labels


def get_task_ip(task, overlay_network_name):
    for network in task.get('NetworksAttachments', []):
        if network.get('Network', {}).get('Spec', {}).get('Name') == overlay_network_name:
            return network['Addresses'][0].split('/')[0]
    return None


def external_horizon_address(member_number, mongo_port, horizon_suffix):
    return "mongo{}.{}:{}".format(member_number, horizon_suffix, mongo_port)


def get_certificate_fingerprint(certificate_file):
    with open(certificate_file, 'rb') as pem_file:
        pem = pem_file.read()
    match = re.search(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', pem, re.DOTALL)
    if not match:
        raise ValueError("No PEM certificate found in {}.".format(certificate_file))
    certificate_der = ssl.PEM_cert_to_DER_cert(match.group().decode('ascii'))
    return hashlib.sha256(certificate_der).hexdigest()


def get_certificate_bundle_fingerprint(certificate_file, ca_file):
    with open(certificate_file, 'rb') as pem_file:
        certificate_bundle = pem_file.read()
    with open(ca_file, 'rb') as ca_bundle_file:
        ca_bundle = ca_bundle_file.read()
    return hashlib.sha256(certificate_bundle + b'\0' + ca_bundle).hexdigest()


def get_presented_certificate_fingerprint(host, port, ca_file):
    context = ssl.create_default_context(cafile=ca_file)
    context.check_hostname = False
    with socket.create_connection((host, port), timeout=5) as connection:
        with context.wrap_socket(connection) as tls_connection:
            return hashlib.sha256(tls_connection.getpeercert(binary_form=True)).hexdigest()


def rotate_task_certificate(host, port, username, password, expected_fingerprint, ca_file):
    client = pm.MongoClient(
        host=host,
        port=port,
        username=username,
        password=password,
        authSource='admin',
        directConnection=True,
        tls=False,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=10000
    )
    try:
        client.admin.command('rotateCertificates')
    finally:
        client.close()

    for attempt in range(10):
        try:
            if get_presented_certificate_fingerprint(host, port, ca_file) == expected_fingerprint:
                return True
        except (OSError, ssl.SSLError):
            pass
        if attempt < 9:
            time.sleep(2)
    return False


def rotate_changed_certificates(tasks, overlay_network_name, mongo_port, mongo_root_username, mongo_root_password, certificate_file, ca_file, applied_bundles, last_attempts):
    logger = logging.getLogger(__name__)
    try:
        expected_fingerprint = get_certificate_fingerprint(certificate_file)
        bundle_fingerprint = get_certificate_bundle_fingerprint(certificate_file, ca_file)
    except (OSError, ValueError) as error:
        logger.warning("Certificate rotation deferred: {}".format(error))
        return

    active_task_ids = {task.get('ID') for task in tasks}
    for task_id in set(applied_bundles) - active_task_ids:
        applied_bundles.pop(task_id, None)
        last_attempts.pop(task_id, None)

    now = time.monotonic()
    for task in tasks:
        task_id = task.get('ID')
        host = get_task_ip(task, overlay_network_name)
        if not task_id or not host or applied_bundles.get(task_id) == bundle_fingerprint:
            continue
        last_attempt = last_attempts.get(task_id)
        if last_attempt and last_attempt[0] == bundle_fingerprint and now - last_attempt[1] < 30:
            continue

        last_attempts[task_id] = (bundle_fingerprint, now)
        try:
            logger.info("Reloading TLS certificates on MongoDB task {} ({})".format(task_id[:12], host))
            if rotate_task_certificate(
                host, mongo_port, mongo_root_username, mongo_root_password,
                expected_fingerprint, ca_file
            ):
                applied_bundles[task_id] = bundle_fingerprint
                logger.info("MongoDB task {} now serves the current TLS certificate.".format(task_id[:12]))
            else:
                logger.warning("MongoDB task {} has not presented the new certificate yet; will retry.".format(task_id[:12]))
        except Exception as error:
            logger.warning("Could not rotate TLS certificate on MongoDB task {}: {}".format(task_id[:12], error))


def setup_initial_database(current_member_ips, mongo_port, mongo_root_username, mongo_root_password, initdb_database, initdb_user, initdb_password):
    """
    Sets up the initial database with a user and an entry in the users collection.

    :param current_member_ips: list of replica members.
    :param mongo_port: Port on which MongoDB is running.
    :mongo_root_username: root user.
    :mongo_root_password: root password.
    :param initdb_database: The name of the database to use or create.
    :param initdb_user: The username for the new user.
    :param initdb_password: The password for the new user.
    """
    logger = logging.getLogger(__name__)

    primary_ip = get_primary_ip(current_member_ips, mongo_port, mongo_root_username, mongo_root_password)
    if primary_ip is None:
        logger.error("No primary MongoDB instance found. Initial user/database setup cannot be completed!")
        return

    # Connect to the primary node
    client = pm.MongoClient(
        host=primary_ip,
        port=mongo_port,
        username=mongo_root_username,
        password=mongo_root_password,
        authSource='admin',
        directConnection=True,
        serverSelectionTimeoutMS=15000,  # 15 seconds timeout server selection
        connectTimeoutMS=30000,  # 30 seconds connection timeout
        socketTimeoutMS=30000    # 30 seconds socket operation timeout
    )

    try:
        logger.info(f"Attempting to create initial user {initdb_user} in initial db {initdb_database} ...")
        # Select the database
        db = client[initdb_database]
        logger.info(f"Selected the {initdb_database} database on {primary_ip}")

        # Create the user with dbOwner role
        logger.info(f"Creating user '{initdb_user}' with dbOwner role.")
        db.command("createUser", initdb_user, pwd=initdb_password, roles=["dbOwner"])
        logger.info(f"User '{initdb_user}' created successfully!")

        # Insert a document into the 'users' collection
        logger.info(f"Inserting document into the 'users' collection for user '{initdb_user}'.")
        db.users.insert_one({"name": initdb_user})
        logger.info(f"Document for user '{initdb_user}' inserted successfully!")
        logger.info("Initial database and initial user setup completed!")

    except Exception as e:
        if hasattr(e, 'code') and e.code == 51003:  # user already exists
            logger.info(f"User '{initdb_user}' already exists in database '{initdb_database}'. No action needed.")
        else:
            logger.error(f"An error occurred while setting up the initial database: {e}")

    finally:
        client.close()


def init_replica(mongo_tasks, mongo_tasks_ips, replicaset_name, mongo_port, mongo_root_username, mongo_root_password, member_labels=None, horizon_suffix='', horizon_name='external', retry_attempts=6, retry_delay=10):
    """
    Init a MongoDB replicaset from the scratch.
    """
    logger = logging.getLogger(__name__)

    # Initial checks for MongoDB task IDs | IPs
    for attempt in range(retry_attempts):
        if not mongo_tasks or not mongo_tasks_ips:
            logger.warning(f"No MongoDB task IDs or IPs found. Retry attempt {attempt + 1}/{retry_attempts}.")
            time.sleep(retry_delay)
        else:
            break
    else:
        logger.error("No MongoDB task IDs or IPs found after all retry attempts. Cannot initialize replica!")
        return

    logger.debug("List of MongoDB task IPs: {}".format(mongo_tasks_ips))
    for task, task_ip in zip(mongo_tasks, mongo_tasks_ips):
        task_id = task['ID']
        task_status = task['Status']['State']
        task_container_id = task['Status']['ContainerStatus']['ContainerID']
        logger.debug("Task ID: {}, Status: {}, Container ID: {}, IP: {}".format(task_id, task_status, task_container_id, task_ip))

    config = create_mongo_config(mongo_tasks_ips, replicaset_name, mongo_port, member_labels, horizon_suffix, horizon_name)
    config_str = json.dumps(config)  # Serialize config to json formatted string
    logger.debug("Initial config Built: {}".format(config))

    # Choose a primary and configure replicaset (picking first IP | first docker container)
    primary_ip = list(mongo_tasks_ips)[0]
    time.sleep(15)
    logger.info("=== Starting replica set initialization ===")
    logger.debug("Searching for containers matching service: {}".format(mongo_service_name))

    try:
        with docker_client() as dc:
            # Get the first MongoDB container using container name matching (more reliable than Container ID)
            all_containers = dc.containers.list()
            logger.debug("Available containers: {}".format([c.name for c in all_containers]))

            mongoContainer = next((c for c in dc.containers.list() if c.name.split('.')[0] == mongo_service_name), None)
            if not mongoContainer:
                logger.error("No MongoDB containers found matching service name: {}".format(mongo_service_name))
                logger.error("Available container names: {}".format([c.name for c in all_containers]))
                return

            logger.info("Found MongoDB container: {} for initialization".format(mongoContainer.name))            # Initialize replica set on localhost using docker container
            initCommand = f"rs.initiate({config_str});"
            clusterCreateExecRes = mongoContainer.exec_run(f"mongosh --quiet --eval '{initCommand}'")
            if clusterCreateExecRes.exit_code != 0:
                raise PyMongoError(clusterCreateExecRes.output.decode())
            logger.info("Creating initial ReplicaSet - result: {}".format(clusterCreateExecRes))
    except PyMongoError as e:
            error_message = str(e)
            if "replSetInitiate requires authentication" in error_message:
                # Handle re-deployment scenario where auth already exists
                logger.info("Re-deployment detected (authentication required) - forcing re-configuration...")
                reconfigure_replica_set(primary_ip, mongo_port, mongo_root_username, mongo_root_password, config, logger)

                # Redeployment handled - configuration has been updated
                logger.info("Redeployment scenario handled successfully")
                return

            elif "No primary detected" in error_message or "Invalid replica set" in error_message:
                logger.error(f"ReplicaSet initiation error: {error_message}")
                # Optional: Implement logic to recover from this state
            else:
                logger.error(f"Failed to initiate replica set: {error_message}")


    #NOTE: PyMongo replica set initiation approach is unworkable when using a keyfile on Mongo's deployment, as it implies authentication that does not yet exist (documented issue) -  tabling this approach in favor of docker container method above
    # time.sleep(15)
    # primary_ip = list(mongo_tasks_ips)[0]
    # primaryNoAuth = pm.MongoClient(
    #     host=primary_ip,
    #     port=mongo_port,
    #     directConnection=True,
    #     serverSelectionTimeoutMS=15000,  # 15 seconds timeout server selection
    #     connectTimeoutMS=30000,  # 30 seconds connection timeout
    #     socketTimeoutMS=30000    # 30 seconds socket operation timeout
    # )
    # try:
    #     res = primaryNoAuth.admin.command("replSetInitiate", config)
    # except PyMongoError as e:
    #     error_message = str(e)
    #     if "replSetInitiate requires authentication" in error_message:
    #         logger.debug("replSetInitiate error output: ({})".format(e))
    #         # Handle re-deployment scenario
    #         logger.info("Re-deployment detected - forcing re-configuration...")
    #         reconfigure_replica_set(primary_ip, mongo_port, mongo_root_username, mongo_root_password, config, logger)
    #     elif "No primary detected" in error_message or "Invalid replica set" in error_message:
    #         logger.error(f"ReplicaSet initiation error: {error_message}")
    #         # Optional: Implement logic to recover from this state
    #     else:
    #         logger.error(f"Failed to initiate replica set: {error_message}")
    # finally:
    #     primaryNoAuth.close()
    # logger.info("replSetInitiate: {}".format(res))

    # Initialize mongodb admin, initial db user in initial db
    # The initialize_mongodb_admin function handles PRIMARY detection and admin user creation
    with docker_client() as dc:
        initialize_mongodb_admin(mongo_tasks, primary_ip, mongo_port, mongo_root_username, mongo_root_password, mongo_service_name, logger, dc)


def reconfigure_replica_set(primary_ip, mongo_port, mongo_root_username, mongo_root_password, config, logger):
    """
    Reconfigure the MongoDB replica set in case of re-deployment.
    """

    # LEGACY method via containers - (deprecating in favor of PyMongo)
    # Re-deployment detected, using rs.reconfig to reconfigure replicaset
    # authCommand = f"db.getSiblingDB('admin').auth('{mongo_root_username}', '{mongo_root_password}');"
    # reconfigCommand = f"rs.reconfig({config}, {{force: true}});"
    # fullCommand = authCommand + reconfigCommand
    # reconfigExecRes = mongoContainer.exec_run(f"mongosh --quiet --eval \"{fullCommand}\"")
    # logger.info("Attempted replicaSet re-configuration - result: {}".format(reconfigExecRes))

    try:
        primary = pm.MongoClient(
            host=primary_ip,
            port=mongo_port,
            username=mongo_root_username,
            password=mongo_root_password,
            directConnection=True,
            serverSelectionTimeoutMS=15000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000
        )
        res = primary.admin.command("replSetReconfig", config, force=True)
        logger.info("Reconfiguring ReplicaSet - result: {}".format(res))
    except OperationFailure as opfail:
        logger.debug("replSetReconfig error: ({})".format(opfail))
    finally:
        primary.close()


def initialize_mongodb_admin(mongo_tasks, primary_ip, mongo_port, mongo_root_username, mongo_root_password, mongo_service_name, logger, dc):
    """
    Initialize MongoDB admin by checking which container is primary, then create root account.
    """

    logger.info("Configuring mongodb admin...")

    retry_attempts = 8
    retry_delay = 10

    for attempt in range(retry_attempts):
        try:
            # Wait for config reconciliation & proper docker container identification
            logger.info(f"Attempt {attempt+1}/{retry_attempts}: Waiting for configuration reconciliation...")
            time.sleep(retry_delay)

            primaryNoAuth = pm.MongoClient(
                host=primary_ip,
                port=mongo_port,
                directConnection=True,  #NOTE: Mongo > 4 defaults to 'false' which forces discovery instead of direct interrogation - this always returns writeablePrimary=true on all members (multiple primaries issue)
                serverSelectionTimeoutMS=15000, # 15 seconds timeout server selection
                connectTimeoutMS=30000,  # 30 seconds connection timeout
                socketTimeoutMS=30000  # 30 seconds socket operation timeout
            )
            replicaSetTopology = primaryNoAuth.admin.command('hello')  #'hello' command pulls topology
            logger.debug("ReplicaSet Topology: {}".format(replicaSetTopology))

            # Check if primary exists in topology (during fresh initialization, it may not exist yet)
            primaryIp = None
            if "primary" in replicaSetTopology and replicaSetTopology["primary"]:
                primaryIp = replicaSetTopology["primary"].split(':')[0]

            primaryNoAuth.close()

            if primaryIp:
                # Find MongoDB container using container name matching (more reliable than Container ID)
                primary_container = next((c for c in dc.containers.list() if c.name.split('.')[0] == mongo_service_name), None)
                if primary_container:
                    create_mongodb_root_user(primary_container.id, mongo_root_username, mongo_root_password, logger, dc, primaryIp)
                    return
                else:
                    logger.warning(f"Attempt {attempt+1}/{retry_attempts}: No container found for service {mongo_service_name} - Retrying...")
            else:
                # This is normal during fresh initialization - primary election takes time
                if attempt < 3:  # First few attempts - this is expected
                    logger.info(f"Attempt {attempt+1}/{retry_attempts}: Waiting for primary election to complete...")
                else:  # Later attempts - more concerning
                    logger.warning(f"Attempt {attempt+1}/{retry_attempts}: Primary still not elected in replica set topology - Retrying...")

        except Exception as e:
            # Only log as ERROR if this is a real unexpected error, not normal primary election timing
            if attempt < 3:
                logger.info(f"Attempt {attempt+1}/{retry_attempts}: Waiting for replica set stabilization - {e}")
            else:
                logger.error(f"Attempt {attempt+1}/{retry_attempts}: Error while initializing MongoDB admin, still polling: {e} from topology - retrying...")

    logger.error(f"Failed to initialize MongoDB admin after {retry_attempts} attempts. Primary election may have failed or containers are not accessible.")


def create_mongodb_root_user(containerId, mongo_root_username, mongo_root_password, logger, dc, primaryIp):
    """
    Create MongoDB root user on the primary container.
    """
    logger.info(f"Creating mongo admin user on primary container...")
    try:
        primaryContainer = dc.containers.get(containerId)
        logger.debug("Found primary container: {} and primary ip: {} in topology".format(primaryContainer, primaryIp))
        createAdmin = "admin = db.getSiblingDB('admin'); admin.createUser({ user: '%s', pwd: '%s', roles: [ 'root' ] } );" % (mongo_root_username, mongo_root_password)
        adminCreateExecRes = primaryContainer.exec_run("mongosh --quiet --eval \"%s\"" % (createAdmin))

        if adminCreateExecRes.exit_code == 0:
            logger.info("Root user created successfully.")
        elif "Command createUser requires authentication" in adminCreateExecRes.output.decode():
            logger.info(f"Root user '{mongo_root_username}' already exists - skipping admin user creation.")
        else:
            logger.error(f"Failed to create root user: {adminCreateExecRes.output.decode()}")
    except PyMongoError as e:
        logger.error(f"MongoDB error occurred: {e}")
    except docker.errors.NullResource:
        logger.error(f"Could not find container with ID {containerId}")
    except Exception as e:
        logger.error(f"Unexpected error creating root user: {e}")


def create_mongo_config(tasks_ips, replicaset_name, mongo_port, member_labels=None, horizon_suffix='', horizon_name='external'):
    logger = logging.getLogger(__name__)

    members = []
    if member_labels:
        tasks_ips = sorted(tasks_ips, key=lambda ip: (member_labels[ip], ip))
    for i, ip in enumerate(tasks_ips):
        member = {
            '_id': i,
            'host': "{}:{}".format(ip, mongo_port)
        }
        if horizon_suffix:
            member['horizons'] = {
                horizon_name: external_horizon_address(member_labels[ip], mongo_port, horizon_suffix)
            }
        members.append(member)
    config = {
        '_id': replicaset_name,
        'members': members,
        'version': 1
        # 'term': 1  # Mongo manages 'term' automatically for config
    }
    # Logging outcome
    logger.info(f"Building MongoDB config with {len(tasks_ips)} members.")
    return config


def gather_configured_members_ips(mongo_tasks_ips, mongo_port, mongo_root_username, mongo_root_password):
    logger = logging.getLogger(__name__)
    current_ips = set()
    config_found = False
    not_yet_initialized_count = 0
    max_retries = 3
    retry_delay = 10

    logger.info("Inspecting Mongo nodes for pre-existing replicaset - this might take a few moments, please wait...")

    # Retry logic for nodes that might be in transitional "NotYetInitialized" state
    for attempt in range(max_retries):
        config_found = False
        not_yet_initialized_count = 0
        current_ips = set()

        for t in mongo_tasks_ips:
            mc = pm.MongoClient(
                host=t,
                port=mongo_port,
                directConnection=True, #NOTE: Mongo > 4 defaults to 'false' which forces discovery instead of direct interrogation - this always returns writeablePrimary=true on all members (multiple primaries issue)
                username=mongo_root_username,
                password=mongo_root_password,
                authSource='admin',
                serverSelectionTimeoutMS=15000,  # 15 seconds timeout server selection
                connectTimeoutMS=30000,  # 30 seconds connection timeout
                socketTimeoutMS=30000    # 30 seconds socket operation timeout
                )
            try:
                config = mc.admin.command("replSetGetConfig")['config']
                for m in config['members']:
                    current_ips.add(m['host'].split(":")[0])
                config_found = True
                logger.info("Pre-existing replicaSet configuration found in node {}: {}".format(t, current_ips))
                break
            except pm.errors.ServerSelectionTimeoutError as ssete:
                logger.debug(f"No pre-existing replicaSet configuration found in node {t} -- (Possible NEW build - please wait...) - ({ssete})")
            except pm.errors.OperationFailure as of:
                # Check for "NotYetInitialized" - this is a transitional state, not permanent
                if getattr(of, 'code', None) == 94 or 'NotYetInitialized' in str(of):
                    not_yet_initialized_count += 1
                    if attempt < max_retries - 1:  # Don't log on last attempt
                        logger.debug(f"Node {t} in transitional NotYetInitialized state (attempt {attempt+1}/{max_retries}): ({of})")
                    else:
                        logger.debug(f"Node {t} - final attempt shows NotYetInitialized, treating as fresh deployment: ({of})")
                else:
                    logger.debug(f"New Node - [expected] authentication failure {t} during initial config gathering (disregard this): ({of})")
            finally:
                mc.close()

        # If we found config, break out of retry loop
        if config_found:
            break

        # If all nodes are in "NotYetInitialized" state and we have retries left, wait and try again
        if not_yet_initialized_count == len(mongo_tasks_ips) and attempt < max_retries - 1:
            logger.info(f"All {not_yet_initialized_count} nodes in NotYetInitialized state - waiting {retry_delay}s for config loading (attempt {attempt+1}/{max_retries})...")
            time.sleep(retry_delay)
        else:
            break

    if not config_found:
        if not_yet_initialized_count == len(mongo_tasks_ips):
            logger.info("All nodes remained in NotYetInitialized state after retries - proceeding with fresh setup!")
        else:
            logger.info("No pre-existing configuration found across all nodes - proceeding with new setup!")
        return current_ips # Return the empty set of IPs

    logger.debug(f"Current replicaSet members in mongo configuration: {current_ips}")
    return current_ips  # Return the current set of IPs


def get_primary_ip(tasks_ips, mongo_port, mongo_root_username, mongo_root_password):
    logger = logging.getLogger(__name__)

    primary_ip = None
    uninitialized_count = 0

    for t in tasks_ips:
        # Prefer unauthenticated hello to avoid noisy auth errors during fresh deployments
        mc = pm.MongoClient(
            host=t,
            port=mongo_port,
            directConnection=True,
            serverSelectionTimeoutMS=9000,
            connectTimeoutMS=15000,
            socketTimeoutMS=15000
        )
        logger.info("Checking Task IP: {} for primary...".format(t))
        try:
            topo = mc.admin.command('hello')
            is_writable = topo.get('isWritablePrimary', False)
            set_name = topo.get('setName')
            logger.debug("Node {} hello response: isWritablePrimary={}, setName={}".format(
                t, is_writable, set_name if set_name else 'None'))

            # Count nodes that have no replica set configuration
            if set_name is None:
                uninitialized_count += 1

            if is_writable:
                primary_ip = t
                break
        except ServerSelectionTimeoutError as ssete:
            logger.debug("Cannot connect to {} to check for primary (network/startup delay): ({})".format(t, ssete))
        except OperationFailure as of:
            # 'hello' should generally be allowed unauthenticated; if not, log once per node
            if t not in AUTH_WARNED_NODES:
                AUTH_WARNED_NODES.add(t)
                logger.debug("Auth not ready on {} (expected during fresh deployment): ({})".format(t, of))
        except Exception as e:
            logger.debug("Unexpected error checking {} for primary: ({})".format(t, e))
        finally:
            mc.close()

    if primary_ip:
        logger.info("--> Mongo ReplicaSet Primary is: {} <--".format(primary_ip))
    elif uninitialized_count == len(tasks_ips) and len(tasks_ips) > 0:
        logger.warning("All {} nodes report setName=None - replica set needs initialization!".format(len(tasks_ips)))
    else:
        logger.warning("No PRIMARY found among {} nodes. ReplicaSet may be initializing or auth not ready.".format(len(tasks_ips)))
    return primary_ip


def is_replica_set_uninitialized(tasks_ips, mongo_port):
    """Check if all nodes are uninitialized (setName=None)"""
    logger = logging.getLogger(__name__)

    if not tasks_ips:
        return True

    uninitialized_count = 0
    reachable_count = 0

    for t in tasks_ips:
        mc = pm.MongoClient(
            host=t,
            port=mongo_port,
            directConnection=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=8000,
            socketTimeoutMS=8000
        )
        try:
            topo = mc.admin.command('hello')
            reachable_count += 1
            set_name = topo.get('setName')
            if set_name is None:
                uninitialized_count += 1
            logger.debug("Node {} initialization check: setName={}".format(
                t, set_name if set_name else 'None (uninitialized)'))
        except Exception as e:
            logger.debug("Cannot reach node {} during initialization check: {}".format(t, e))
        finally:
            mc.close()

    # If all reachable nodes are uninitialized, replica set needs init
    if reachable_count > 0 and uninitialized_count == reachable_count:
        logger.info("Detected {} uninitialized nodes out of {} reachable - replica set needs initialization".format(
            uninitialized_count, reachable_count))
        return True
    return False


# NOTE: alternate get_primary_ip method - boiler plate rn / in testing...
# def get_primary_ip(tasks_ips, mongo_port, mongo_root_username, mongo_root_password):
#     logger = logging.getLogger(__name__)
#     primary_ip = None
#     for t in tasks_ips:
#         mc = pm.MongoClient(
#             host=t,
#             port=mongo_port,
#             username=mongo_root_username,
#             password=mongo_root_password,
#             directConnection=True,
#             serverSelectionTimeoutMS=9000  # 9 seconds timeout
#         )
#         try:
#             # Use the 'hello' command to ensure we get the latest information
#             server_status = mc.admin.command('hello')
#             if server_status.get('isWritablePrimary', False):
#                 primary_ip = t
#                 break  # We found the primary, no need to continue
#         except ServerSelectionTimeoutError as ssete:
#             logger.debug(f"Cannot connect to {t} to check if primary, failed ({ssete})")
#         except OperationFailure as of:
#             logger.debug(f"No configuration found in node {t} ({of})")
#         finally:
#             mc.close()

#     if primary_ip:
#         logger.info(f"Primary is: {primary_ip}")
#     else:
#         logger.warning("No primary found in the given task IPs.")
#     return primary_ip


def update_config(primary_ip, current_ips, new_ips, mongo_port, mongo_root_username, mongo_root_password, member_labels=None, horizon_suffix='', horizon_name='external'):

    logger = logging.getLogger(__name__)

    to_remove = set(current_ips) - set(new_ips)
    to_add = set(new_ips) - set(current_ips)

    if not (to_remove or to_add) and not horizon_suffix:
        logger.info("Config update - no IP or horizon changes requested")
        return

    force = len(to_remove) > 1 or len(to_add) > 1

    if primary_ip in to_remove or primary_ip is None:
        logger.info("Config update - Primary ({}) no longer available".format(primary_ip))
        force = True

        # Let's see if a new primary was elected
        attempts = 6
        primary_ip = None

        # Check if all nodes need initialization (full redeployment scenario)
        all_nodes_uninitialized = True
        for t in new_ips:
            mc = pm.MongoClient(
                host=t,
                port=mongo_port,
                directConnection=True,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000
            )
            try:
                topo = mc.admin.command('hello')
                if topo.get('setName') is not None:
                    all_nodes_uninitialized = False
                    break
            except Exception:
                pass  # Connection issues during redeployment are expected
            finally:
                mc.close()

        if all_nodes_uninitialized and len(new_ips) > 0:
            logger.info("Config update - All nodes uninitialized (full redeployment), skipping primary wait and forcing reconfiguration")
            attempts = 0  # Skip the waiting loop
        else:
            while attempts and not primary_ip:
                time.sleep(10)
                primary_ip = get_primary_ip(list(new_ips), mongo_port, mongo_root_username, mongo_root_password)
                attempts -= 1
                logger.info("Config update - No new primary yet automatically elected, please wait..".format(primary_ip))

        if primary_ip is None:
            old_members = list(new_ips - to_add)
            primary_ip = old_members[0] if old_members else list(new_ips)[0]
            logger.info("Config update - Choosing {} as the new primary".format(primary_ip))

    cli = pm.MongoClient(
        host=primary_ip,
        port=mongo_port,
        directConnection=True,
        username=mongo_root_username,
        password=mongo_root_password,
        authSource='admin',
        serverSelectionTimeoutMS=15000,  # 15 seconds timeout server selection
        connectTimeoutMS=30000,  # 30 seconds connection timeout
        socketTimeoutMS=30000    # 30 seconds socket operation timeout
        )
    try:
        config = cli.admin.command("replSetGetConfig")['config']
        logger.debug("Config Update - Old Replica Members: {}".format(config['members']))

        if to_remove:
            logger.info("Config Update - Replica Members to remove: {}".format(to_remove))
            new_members = [m for m in config['members'] if m['host'].split(":")[0] not in to_remove]
            config['members'] = new_members

        if to_add:
            logger.info("Config Update - Replica members to add: {}".format(to_add))
            if config['members']:
                offset = max([m['_id'] for m in config['members']]) + 1
            else:
                offset = 0

            ordered_additions = sorted(to_add, key=lambda ip: (member_labels.get(ip, float('inf')), ip))
            for i, ip in enumerate(ordered_additions):
                config['members'].append({
                    '_id': offset + i,
                    'host': "{}:{}".format(ip, mongo_port)
                })

        horizons_changed = False
        if horizon_suffix:
            for member in config['members']:
                ip = member['host'].rsplit(':', 1)[0]
                if ip not in member_labels:
                    raise RuntimeError("No Swarm mongo label found for replica-set member {}.".format(ip))
                horizons = dict(member.get('horizons') or {})
                address = external_horizon_address(member_labels[ip], mongo_port, horizon_suffix)
                if horizons.get(horizon_name) != address:
                    horizons[horizon_name] = address
                    member['horizons'] = horizons
                    horizons_changed = True

        if not (to_remove or to_add or horizons_changed):
            logger.info("Config update - replica-set members and horizons are already current")
            return

        # Incrementing 'version' (not incrementing 'term' as mongo handles it automatically
        config['version'] += 1

        logger.debug("Config Update - Built Updated config: {}".format(config))

        # Apply new config
        # Try reconfig with a small retry window for stepdown/transient errors
        attempts = 3
        while attempts:
            try:
                res = cli.admin.command("replSetReconfig", config, force=force)
                logger.info("Config Update - Applied Updated Config - result: {}".format(res))
                break
            except OperationFailure as of:
                attempts -= 1
                logger.warning(f"Config Update - reconfig failed (attempts left {attempts}): {of}")
                time.sleep(5)

    except ServerSelectionTimeoutError as e:
        logger.error(f"Config Update - Failed to connect to MongoDB for reconfiguration: {e}")

    except PyMongoError as e:
        code = getattr(e, 'code', None)
        details = getattr(e, 'details', None)
        logger.error(f"Config Update - MongoDB error during reconfiguration: {e}")
        logger.error(f"Error details: Code: {code}, Details: {details}")

    except Exception as e:
        logger.error(f"Config Update - General exception occurred during reconfiguration: {e}")
        logger.error(f"Exception traceback: {traceback.format_exc()}")

    finally:
        cli.close()


# NOTE: method for stepping down primaries (not needed so far)
# def step_down_primary(primary_ip, mongo_port, mongo_root_username, mongo_root_password):
#     logger = logging.getLogger(__name__)
#     with pm.MongoClient(host=primary_ip, port=mongo_port, username=mongo_root_username, password=mongo_root_password) as primary:
#         try:
#             result = primary.admin.command("replSetStepDown")
#             logger.info(f"Step-down result: {result}")
#         except Exception as e:
#             logger.error(f"Failed to step down primary ({primary_ip}): {str(e)}")


# NOTE: method to reconfig changeStreamOptions (mongo > 6) -  not using, (defaults work)
# def set_change_stream_options(client, expire_after_seconds=None):
#     """
#     Set the changeStreamOptions parameter on the MongoDB cluster.
#     :param client: pymongo.MongoClient, connected to the primary node.
#     :param expire_after_seconds: int, the time in seconds after which the change stream events expire.
#     """
#     command = {
#         "setClusterParameter": {
#             "changeStreamOptions": {
#                 "preAndPostImages": {
#                     "expireAfterSeconds": expire_after_seconds
#                 }
#             }
#         }
#     }
#     try:
#         result = client.admin.command(command)
#         logging.info(f'Set changeStreamOptions: {result}')
#     except Exception as e:
#         logging.error(f'Failed to set changeStreamOptions: {str(e)}')


# NOTE: method to adjust election timeout and heartbeats - not using (defaults work)
# def adjust_election_timeout(primary_ip, mongo_port, mongo_root_username, mongo_root_password):
#     """
#     Adjust the election timeout and heartbeat settings.
#     """
#     logger = logging.getLogger(__name__)
#     with pm.MongoClient(host=primary_ip, port=mongo_port, username=mongo_root_username, password=mongo_root_password) as client:
#         try:
#             config = client.admin.command("replSetGetConfig")
#             config['config']['settings'] = {'electionTimeoutMillis': 10000,
#                                             'heartbeatTimeoutSecs': 10}
#             client.admin.command("replSetReconfig", config['config'])
#         except Exception as e:
#             logger.error(f"Failed to adjust election timeout: {str(e)}")


def manage_replica(mongo_service, overlay_network_name, replicaset_name, mongo_port, mongo_root_username, mongo_root_password, initdb_database, initdb_user, initdb_password, mongo_horizon_suffix='', mongo_horizon_name='external', mongo_certificate_file='', mongo_ca_file=''):
    """
    To manage the replica:
    - Configure replicaset
        If there was no replica before, create one from scratch.
        If there was a replica (e.g, this script or docker stack was restarted), the replicaset could be either fine or broken.
            If the replicaset is healthy, move on to the "monitoring" phase.
            Else, force a reconfiguration.
    - Watch for changes in tasks ips
        When IP changes are detected, the replica will break, so we must fix it on the fly.

    :param mongo_service:
    :param overlay_network_name:
    :param replicaset_name:
    :param mongo_port:
    :initdb_user:
    :initdb_password:
    :return:
    """
    logger = logging.getLogger(__name__)

    # Get mongo tasks ips
    mongo_tasks = get_running_tasks(mongo_service)
    member_labels = get_task_member_labels(mongo_tasks, overlay_network_name)
    current_member_labels = member_labels
    mongo_tasks_ips = list(member_labels)
    logger.info("Mongo tasks ips: {}".format(mongo_tasks_ips))

    current_member_ips = gather_configured_members_ips(mongo_tasks_ips, mongo_port, mongo_root_username, mongo_root_password)
    logger.debug("Current mongo ips in returned configuration: {}".format(current_member_ips))

    # If we found existing config but task IPs have changed, use task IPs for primary detection (redeployment scenario)
    if current_member_ips and set(current_member_ips) != set(mongo_tasks_ips):
        logger.info("Detected redeployment - existing config found but task IPs changed. Using current task IPs for primary detection.")
        primary_ip = get_primary_ip(mongo_tasks_ips, mongo_port, mongo_root_username, mongo_root_password)
    else:
        primary_ip = get_primary_ip(current_member_ips, mongo_port, mongo_root_username, mongo_root_password)

    logger.debug("Current primary ip in returned configuration: {}".format(primary_ip))

    if len(current_member_ips) == 0:
        # Starting from the scratch or a fresh deployment
        logger.info("No previous replicaSet configuration found - proceeding with fresh initialization...")
        current_member_ips = set(mongo_tasks_ips)
        init_replica(
            mongo_tasks, current_member_ips, replicaset_name, mongo_port,
            mongo_root_username, mongo_root_password, member_labels,
            mongo_horizon_suffix, mongo_horizon_name
        )
        setup_initial_database(current_member_ips, mongo_port, mongo_root_username, mongo_root_password, initdb_database, initdb_user, initdb_password)
    elif set(current_member_ips) != set(mongo_tasks_ips):
        # Redeployment detected - existing config but different IPs
        logger.info("Redeployment detected - updating configuration immediately...")
        update_config(
            primary_ip, current_member_ips, set(mongo_tasks_ips), mongo_port,
            mongo_root_username, mongo_root_password, member_labels,
            mongo_horizon_suffix, mongo_horizon_name
        )
        current_member_ips = set(mongo_tasks_ips)
        primary_ip = get_primary_ip(mongo_tasks_ips, mongo_port, mongo_root_username, mongo_root_password)
    else:
        logger.info("Existing replicaSet configuration matches current deployment - monitoring for changes...")
        if mongo_horizon_suffix:
            update_config(
                primary_ip, current_member_ips, set(mongo_tasks_ips), mongo_port,
                mongo_root_username, mongo_root_password, member_labels,
                mongo_horizon_suffix, mongo_horizon_name
            )

    applied_certificate_bundles = {}
    certificate_rotation_attempts = {}
    rotate_changed_certificates(
        mongo_tasks, overlay_network_name, mongo_port, mongo_root_username,
        mongo_root_password, mongo_certificate_file, mongo_ca_file,
        applied_certificate_bundles, certificate_rotation_attempts
    )

    # Watch for IP changes. If IPs remain stable we assume MongoDB maintains the replicaset working fine.
    while True:
        time.sleep(10)
        mongo_tasks = get_running_tasks(mongo_service)
        rotate_changed_certificates(
            mongo_tasks, overlay_network_name, mongo_port, mongo_root_username,
            mongo_root_password, mongo_certificate_file, mongo_ca_file,
            applied_certificate_bundles, certificate_rotation_attempts
        )
        new_member_labels = get_task_member_labels(mongo_tasks, overlay_network_name)
        new_member_ips = set(new_member_labels)
        if current_member_ips.symmetric_difference(new_member_ips) or current_member_labels != new_member_labels:
            logger.info("Detected change in member IPs - Updating configuration...")
            update_config(
                primary_ip, current_member_ips, new_member_ips, mongo_port,
                mongo_root_username, mongo_root_password, new_member_labels,
                mongo_horizon_suffix, mongo_horizon_name
            )
        current_member_ips = new_member_ips
        current_member_labels = new_member_labels
        primary_ip = get_primary_ip(new_member_ips, mongo_port, mongo_root_username, mongo_root_password)


# Global variable for service name - used by multiple functions
mongo_service_name = None

if __name__ == '__main__':
    # Get environment variables
    envs = get_required_env_variables()
    mongo_service_name = envs.pop('mongo_service_name')

    # Configure colored logging
    if '1' == os.environ.get('DEBUG'):
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    # Set up colored formatter
    formatter = ColoredFormatter(
        fmt='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers and add colored console handler
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Set logging of HTTP entries to WARNING-level only
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Set logging for docker.utils.config to WARNING-level only
    logging.getLogger("docker.utils.config").setLevel(logging.WARNING)
    # Suppress PyMongo driver debug noise unless troubleshooting
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("pymongo.pool").setLevel(logging.WARNING)
    logging.getLogger("pymongo.topology").setLevel(logging.WARNING)
    logging.getLogger("pymongo.server").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)

    try:
        with docker_client() as dc:
            logger.info('Waiting for mongo service ({}) tasks to start, please be patient...'.format(mongo_service_name))

            # Make sure Mongo is up and running
            attempts = 40  # Total number of attempts to check if the service is up - increase for large # of nodes (tested on 7 nodes)
            mongo_service = None
            service_down = True
            while service_down and attempts > 0:
                logger.info("Waiting for all MongoDB replicas to be up - attempts remaining: {}".format(attempts))
                time.sleep(10)  # Check every 10 seconds
                mongo_service = get_mongo_service(dc, mongo_service_name)
                service_down = not is_service_up(mongo_service)
                attempts -= 1

            if attempts <= 0 or not mongo_service:
                logger.error('Exhausted attempts waiting for mongo service ({}) - restarting task...'.format(mongo_service_name))
                sys.exit(1)

            logger.info("Mongo service nodes are up and running!")
            manage_replica(mongo_service, **envs)

    except docker.errors.DockerException as e:
        logger.error(f"An error occurred with Docker: {e}")
        sys.exit(1)
