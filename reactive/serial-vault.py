import os
import tempfile
import shutil

from subprocess import call
from subprocess import check_call
from subprocess import check_output

from charmhelpers.core import hookenv
from charmhelpers.core.hookenv import (
    charm_dir, local_unit, log, relation_get, relation_id, relation_set, related_units)
from charmhelpers.core import templating
from charmhelpers.fetch import install_remote
from charms.reactive import hook
from charms.reactive import is_state
from charms.reactive import set_state

from swiftclient.service import SwiftService, SwiftError
from helpers import dequote


PORTS = {
    'admin': {'open': 8081, 'close': [8080, 8082]},
    'signing': {'open': 8080, 'close': [8081, 8082]},
    'system-user': {'open': 8082, 'close': [8080, 8081]},
}


PROJECT = 'serial-vault'
SERVICE = '{}.service'.format(PROJECT)
AVAILABLE = '{}.available'.format(PROJECT)
ACTIVE = '{}.active'.format(PROJECT)

SYSTEMD_UNIT_FILE = os.path.join(charm_dir(), 'files', 'systemd', SERVICE)

DATABASE_NAME = 'serialvault'


@hook('install')
def install():
    """Charm install hook

    Fetches the Serial Vault snap and installs it. Configuration cannot
    be done until the database is available.
    """
    if is_state(AVAILABLE):
        return

    # Open the relevant port for the service
    open_port()

    # Set the proxy server and restart the snapd service, if required
    set_proxy_server()

    # Deploy binaries and systemd configuration, but it won't be ready until it has a db connection
    download_and_deploy_service()

    hookenv.status_set('maintenance', 'Waiting for database')
    set_state(AVAILABLE)


@hook('config-changed')
def config_changed():
    rel_ids = list(hookenv.relation_ids('database'))
    if len(rel_ids) == 0:
        log("Database not ready yet... skipping it for now")
        return

    # Get the database settings
    db_id = rel_ids[0]
    relations = hookenv.relations()['database'][db_id]
    database = None
    for key, value in relations.items():
        if key.startswith('postgresql'):
            database = value
    if not database:
        log("Database not ready yet... skipping it for now")
        return

    # Open the relevant port for the service
    open_port()

    # Update the config file with the service_type and database settings
    update_config(database)

    # Refresh the service payload and restart the service
    refresh_service()

    hookenv.status_set('active', '')
    set_state(ACTIVE)


@hook('database-relation-joined')
def db_relation_joined(*args):
    # Use a specific database name
    relation_set(database=DATABASE_NAME)


@hook('database-relation-changed')
def db_relation_changed(*args):
    configure_service()


@hook('website-relation-changed')
def website_relation_changed(*args):
    """
    Set the hostname and the port for reverse proxy relations
    """
    config = hookenv.config()
    port_config = PORTS.get(config['service_type'])
    if port_config:
        port = port_config['open']
    else:
        port = PORTS['signing']['open']

    relation_set(
        relation_id(), {'port': port, 'hostname': local_unit().split('/')[0]})


@hook('upgrade-charm')
def refresh_service():
    hookenv.status_set('maintenance', 'Refresh the service')

    # Overrides previous deployment
    download_and_deploy_service()

    restart_service(SERVICE)

    hookenv.status_set('active', '')
    set_state(ACTIVE)


def configure_service():
    """Create snap config file and send it to the snap

    Get the database settings and create the service config file. Pipe it to
    the service using the config command. This will overwrite the settings on
    the snap's filesystem.
    """

    hookenv.status_set('maintenance', 'Configure the service')

    # Open the relevant port for the service
    open_port()

    database = get_database()
    if not database:
        return

    update_config(database)


def update_config(database):
    # Create the configuration file for the snap
    create_settings(database)

    # Send the configuration file to its right path
    check_call(['sudo', 'mv', 'settings.yaml', '/usr/local/etc/'], shell=True)

    # Restart the snap
    restart_service(SERVICE)

    hookenv.status_set('active', '')
    set_state(ACTIVE)


def get_database():
    if not relation_get('database'):
        log("Database not ready yet... skipping it for now")
        return None

    database = None
    for db_unit in related_units():
        # Make sure that we have the specific database for the serial vault
        if relation_get('database', db_unit) != DATABASE_NAME:
            continue

        remote_state = relation_get('state', db_unit)
        if remote_state in ('master', 'standalone'):
            database = relation_get(unit=db_unit)

    if not database:
        log("Database not ready yet... skipping it for now")
        hookenv.status_set('maintenance', 'Waiting for database')
        return None

    return database


def set_proxy_server():
    """Set up the proxy server for snapd.

    Some environments may need a proxy server to access the Snap Store. The
    access is from snapd rather than the snap command, so the system-wide
    environment file needs to be updated and snapd needs to be restarted.
    """
    config = hookenv.config()
    if len(config.get('proxy', "")) == 0:
        return

    # Update the /etc/environment file
    env_command = 'echo "{}={}" | sudo tee -a /etc/environment'
    check_output(
        env_command.format('http_proxy', config['proxy']), shell=True)
    check_output(
        env_command.format('https_proxy', config['proxy']), shell=True)

    # Restart the snapd service
    restart_service('snapd')

def download_and_deploy_service():
    """ Downloads from swift container and deploys service payload
    """
    payload_local_path = download_service_payload_from_swift_container()
    
    # In case an empty path is returned, search for payload settings value
    # and treat it as a direct downloadable payload url
    if not payload_local_path:
        config = hookenv.config()
        payload_local_path = config['payload']

    deploy_service_payload(payload_local_path)

def download_service_payload_from_swift_container():
    """ Updates environment with 'environment_variables' defined ones,
    gets container and payload references from config, and use them
    to download from swift the service payload.
    Method returns the path to the downloaded file
    """
    hookenv.status_set('maintenance', 'Download service payload from swift container')
    # Update environment with vars defined in 'environment_variables' config
    update_env()

    config = hookenv.config()
    container = config['swift_container']
    payload = config['payload']
    if not container or not payload:
        return ''

    with SwiftService() as swift:
        try:
            objects = [payload]
            for down_res in swift.download(
                    container=container,
                    objects=objects):
                if down_res['success']:
                    log('downloaded from swift container: {}'.format(down_res['path']))
                    return down_res['path']
                else:
                    log('download failed for {}'.format(down_res['object']))
                    return ''
        except SwiftError as e:
            log('An error happened while trying to download from swift container. {}'.format(e.value))
            return ''

    hookenv.status_set('maintenance', 'Service payload downloaded')

def deploy_service_payload(payload_path):
    """ Gets binaries and systemd config tgz from payload path, uncompresses it in a
    temporary folder and:
    - moves serial-vault and serial-vault-admin to /usr/local/bin
    - moves serial-vault.service to /etc/systemd/system
    - creates settings and store in /usr/local/etc/settings.yaml
    """
    hookenv.status_set('maintenance', 'Deploy service payload')
    
    tmp_dir = tempfile.mkdtemp()
    payload_dir = install_remote(payload_path, dest=tmp_dir)
    if payload_dir == tmp_dir:
        log('Got binaries tgz at {}'.format(payload_dir))
        
        if not os.path.isfile(os.path.join(payload_dir, 'serial-vault')):
            log('Could not find serial-vault binary')
            return
        if not os.path.isfile(os.path.join(payload_dir, 'serial-vault-admin')):
            log('Could not find serial-vault-admin binary')
            return
    
        check_call(['sudo', 'mv', os.path.join(payload_dir, 'serial-vault'), '/usr/local/bin/'])
        check_call(['sudo', 'mv', os.path.join(payload_dir, 'serial-vault-admin'), '/usr/local/bin/'])
        check_call(['sudo', 'mv', SYSTEMD_UNIT_FILE, '/etc/systemd/system/'])

    hookenv.status_set('maintenance', 'Service payload deployed')


def create_settings(postgres):
    hookenv.status_set('maintenance', 'Configuring service')
    config = hookenv.config()
    templating.render(
        source='settings.yaml',
        target='settings.yaml',
        context={
            'keystore_secret': config['keystore_secret'],
            'service_type': config['service_type'],
            'csrf_auth_key': config['csrf_auth_key'],
            'db': postgres,
            'url_host': config['url_host'],
            'enable_user_auth': bool(config['enable_user_auth']),
        }
    )

def open_port():
    """
    Open the port that is requested for the service and close the others.
    """
    config = hookenv.config()
    port_config = PORTS.get(config['service_type'])
    if port_config:
        hookenv.open_port(port_config['open'], protocol='TCP')
        for port in port_config['close']:
            hookenv.close_port(port, protocol='TCP')


def restart_service(service):
    call(['sudo', 'systemctl', 'restart', service])

def update_env():
    config = hookenv.config()
    env_vars_string = config('environment_variables')

    if env_vars_string:
        for env_var_string in env_vars_string.split(' '):
            key, value = env_var_string.split('=')
            value = dequote(value)
            log('setting env var {}={}'.format(key, value))
            os.environ[key] = value
