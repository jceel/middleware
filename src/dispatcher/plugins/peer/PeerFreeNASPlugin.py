#
# Copyright 2016 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import os
import socket
import errno
import logging
from freenas.dispatcher.client import Client
from paramiko import AuthenticationException
from utils import get_replication_client, call_task_and_check_state
from freenas.utils import exclude, query as q
from freenas.dispatcher.rpc import RpcException, SchemaHelper as h, description, accepts, private, generator
from task import Task, Provider, TaskException, TaskWarning, VerifyException, query, TaskDescription


logger = logging.getLogger(__name__)

REPL_USR_HOME = '/var/tmp/replication'
AUTH_FILE = os.path.join(REPL_USR_HOME, '.ssh/authorized_keys')

ssh_port = None


@description('Provides information about known FreeNAS peers')
class PeerFreeNASProvider(Provider):
    @query('peer')
    @generator
    def query(self, filter=None, params=None):
        peers = self.datastore.query_stream('peers', ('type', '=', 'freenas'))

        return q.query(peers, *(filter or []), stream=True, **(params or {}))


@description('Exchanges SSH keys with remote FreeNAS machine')
@accepts(h.all_of(
    h.ref('peer'),
    h.required('type', 'credentials'),
    h.forbidden('name')
))
class FreeNASPeerCreateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Exchanging SSH keys with remote host'

    def describe(self, peer):
        return TaskDescription('Exchanging SSH keys with the remote {name}', name=q.get(peer, 'credentials.address', ''))

    def verify(self, peer):
        credentials = peer['credentials']
        remote = credentials.get('address')
        username = credentials.get('username')
        password = credentials.get('password')

        if not username:
            raise VerifyException(errno.EINVAL, 'Username has to be specified')

        if not remote:
            raise VerifyException(errno.EINVAL, 'Address of remote host has to be specified')

        if not password:
            raise VerifyException(errno.EINVAL, 'Password has to be specified')

        if credentials.get('type') != 'ssh':
            raise VerifyException(errno.EINVAL, 'SSH credentials type is needed to perform FreeNAS peer pairing')

        return ['system']

    def run(self, peer):
        hostid = self.dispatcher.call_sync('system.info.host_uuid')
        hostname = self.dispatcher.call_sync('system.general.get_config')['hostname']
        remote_peer_name = hostname
        credentials = peer['credentials']
        remote = credentials.get('address')
        username = credentials.get('username')
        port = credentials.get('port', 22)
        password = credentials.get('password')

        if self.datastore.exists('peers', ('credentials.address', '=', remote), ('type', '=', 'freenas')):
            raise TaskException(
                errno.EEXIST,
                'FreeNAS peer entry for {0} already exists'.format(remote)
            )

        remote_client = Client()
        try:
            try:
                remote_client.connect('ws+ssh://{0}@{1}'.format(username, remote), port=port, password=password)
                remote_client.login_service('replicator')
            except (AuthenticationException, OSError, ConnectionRefusedError):
                raise TaskException(errno.ECONNABORTED, 'Cannot connect to {0}:{1}'.format(remote, port))

            local_host_key, local_pub_key = self.dispatcher.call_sync('peer.get_ssh_keys')
            remote_host_key, remote_pub_key = remote_client.call_sync('peer.get_ssh_keys')
            ip_at_remote_side = remote_client.call_sync('management.get_sender_address').split(',', 1)[0]

            remote_hostname = remote_client.call_sync('system.general.get_config')['hostname']

            remote_host_key = remote_host_key.rsplit(' ', 1)[0]
            local_host_key = local_host_key.rsplit(' ', 1)[0]

            local_ssh_config = self.dispatcher.call_sync('service.sshd.get_config')

            if remote_client.call_sync('peer.query', [('id', '=', hostid)]):
                raise TaskException(errno.EEXIST, 'Peer entry of {0} already exists at {1}'.format(hostname, remote))

            peer['credentials'] = {
                'pubkey': remote_pub_key,
                'hostkey': remote_host_key,
                'port': port,
                'type': 'freenas',
                'address': remote_hostname
            }

            local_id = remote_client.call_sync('system.info.host_uuid')
            peer['id'] = local_id
            peer['name'] = remote_hostname
            ip = socket.gethostbyname(remote)

            self.join_subtasks(self.run_subtask(
                'peer.freenas.create_local',
                peer,
                ip
            ))

            peer['id'] = hostid
            peer['name'] = remote_peer_name

            peer['credentials'] = {
                'pubkey': local_pub_key,
                'hostkey': local_host_key,
                'port': local_ssh_config['port'],
                'type': 'freenas',
                'address': hostname
            }

            try:
                call_task_and_check_state(
                    remote_client,
                    'peer.freenas.create_local',
                    peer,
                    ip_at_remote_side
                )
            except TaskException:
                self.datastore.delete('peers', local_id)
                self.dispatcher.dispatch_event('peer.changed', {
                    'operation': 'delete',
                    'ids': [local_id]
                })
                raise
        finally:
            remote_client.disconnect()


@private
@description('Creates FreeNAS peer entry in database')
@accepts(h.ref('peer'), str)
class FreeNASPeerCreateLocalTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Creating FreeNAS peer entry'

    def describe(self, peer, ip):
        return TaskDescription('Creating FreeNAS peer entry {name}', name=peer['name'])

    def verify(self, peer, ip):
        return ['system']

    def run(self, peer, ip):
        def ping(address, port):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect((address, port))
            finally:
                s.close()

        if self.datastore.exists('peers', ('id', '=', peer['id'])):
            raise TaskException(errno.EEXIST, 'FreeNAS peer entry {0} already exists'.format(peer['name']))

        credentials = peer['credentials']

        try:
            ping(credentials['address'], credentials['port'])
        except socket.error:
            try:
                ping(ip, credentials['port'])
                credentials['address'] = ip
            except socket.error as err:
                raise TaskException(err.errno, '{0} is not reachable. Check connection'.format(credentials['address']))

        if ip and socket.gethostbyname(credentials['address']) != socket.gethostbyname(ip):
            raise TaskException(
                errno.EINVAL,
                'Resolved peer {0} IP {1} does not match desired peer IP {2}'.format(
                    credentials['address'],
                    socket.gethostbyname(credentials['address']),
                    ip
                )
            )

        id = self.datastore.insert('peers', peer)

        with open(AUTH_FILE, 'a') as auth_file:
            auth_file.write(peer['credentials']['pubkey'])

        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'create',
            'ids': [id]
        })


@description('Removes FreeNAS peer entry')
@accepts(str)
class FreeNASPeerDeleteTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Removing FreeNAS peer entry'

    def describe(self, id):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Removing FreeNAS peer entry: {name}', name=peer['name'])

    def verify(self, id):
        return ['system']

    def run(self, id):
        peer = self.datastore.get_by_id('peers', id)
        if not peer:
            raise TaskException(errno.ENOENT, 'Peer entry {0} does not exist'.format(id))

        remote = q.get(peer, 'credentials.address')
        remote_client = None
        hostid = self.dispatcher.call_sync('system.info.host_uuid')
        try:
            try:
                remote_client = get_replication_client(self.dispatcher, remote)

                call_task_and_check_state(
                    remote_client,
                    'peer.freenas.delete_local',
                    hostid
                )
            except RpcException as e:
                self.add_warning(TaskWarning(
                    e.code,
                    'Remote {0} is unreachable. Delete operation is performed at local side only.'.format(remote)
                ))
            except ValueError as e:
                self.add_warning(TaskWarning(
                    errno.EINVAL,
                    str(e)
                ))

            self.join_subtasks(self.run_subtask(
                'peer.freenas.delete_local',
                id
            ))

        finally:
            if remote_client:
                remote_client.disconnect()


@private
@description('Removes local FreeNAS peer entry from database')
@accepts(str)
class FreeNASPeerDeleteLocalTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Removing FreeNAS peer entry'

    def describe(self, id):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Removing FreeNAS peer entry {name}', name=peer['name'])

    def verify(self, id):
        return ['system']

    def run(self, id):
        peer = self.datastore.get_by_id('peers', id)
        if not peer:
            raise TaskException(errno.ENOENT, 'FreeNAS peer entry {0} does not exist'.format(peer['name']))
        peer_pubkey = peer['credentials']['pubkey']
        self.datastore.delete('peers', id)

        with open(AUTH_FILE, 'r') as auth_file:
            auth_keys = auth_file.read()

        new_auth_keys = ''
        for line in auth_keys.splitlines():
            if peer_pubkey not in line:
                new_auth_keys = new_auth_keys + '\n' + line

        with open(AUTH_FILE, 'w') as auth_file:
            auth_file.write(new_auth_keys)

        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'delete',
            'ids': [id]
        })


@private
@description('Updates FreeNAS peer entry in database')
@accepts(str, h.ref('peer'))
class FreeNASPeerUpdateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Updating FreeNAS peer entry'

    def describe(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Updating FreeNAS peer entry {name}', name=peer['name'])

    def verify(self, id, updated_fields):
        return ['system']

    def run(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        if not peer:
            raise TaskException(errno.ENOENT, 'FreeNAS peer entry {0} does not exist'.format(id))

        if 'name' in updated_fields:
            raise TaskException(errno.EINVAL, 'Name of FreeNAS peer cannot be updated')

        if 'type' in updated_fields:
            raise TaskException(errno.EINVAL, 'Type of FreeNAS peer cannot be updated')

        if 'id' in updated_fields:
            raise TaskException(errno.EINVAL, 'ID of FreeNAS peer cannot be updated')

        peer.update(updated_fields)

        self.datastore.update('peers', id, peer)
        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'update',
            'ids': [id]
        })


@private
@description('Updates remote FreeNAS peer entry')
@accepts(str)
class FreeNASPeerUpdateRemoteTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Updating remote FreeNAS peer'

    def describe(self, id):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Updating remote FreeNAS peer {name}', name=peer['name'])

    def verify(self, id):
        return ['system']

    def run(self, id):
        peer = self.datastore.get_by_id('peers', id)
        hostid = self.dispatcher.call_sync('system.info.host_uuid')
        remote_client = None
        if not peer:
            raise TaskException(errno.ENOENT, 'FreeNAS peer entry {0} does not exist'.format(id))

        try:
            remote_client = get_replication_client(self.dispatcher, peer['credentials']['address'])
            remote_peer = remote_client.call_sync('peer.query', [('id', '=', hostid)], {'single': True})
            if not remote_peer:
                raise TaskException(errno.ENOENT, 'Remote side of peer {0} does not exist'.format(peer['name']))

            ip_at_remote_side = remote_client.call_sync('management.get_sender_address').split(',', 1)[0]
            hostname = self.dispatcher.call_sync('system.general.get_config')['hostname']
            port = self.dispatcher.call_sync('service.sshd.get_config')['port']

            remote_peer['name'] = hostname

            remote_peer['credentials']['port'] = port
            remote_peer['credentials']['address'] = hostname

            call_task_and_check_state(
                remote_client,
                'peer.freenas.delete_local',
                hostid
            )

            remote_peer = exclude(remote_peer, 'created_at', 'updated_at')

            call_task_and_check_state(
                remote_client,
                'peer.freenas.create_local',
                remote_peer,
                ip_at_remote_side
            )
        finally:
            if remote_client:
                remote_client.disconnect()


def _depends():
    return ['PeerPlugin', 'SSHPlugin', 'SystemInfoPlugin']


def _metadata():
    return {
        'type': 'peering',
        'subtype': 'freenas'
    }


def _init(dispatcher, plugin):
    global ssh_port
    global hostname
    ssh_port = dispatcher.call_sync('service.sshd.get_config')['port']
    hostname = dispatcher.call_sync('system.general.get_config')['hostname']

    # Register schemas
    plugin.register_schema_definition('freenas-credentials', {
        'type': 'object',
        'properties': {
            'type': {'enum': ['freenas']},
            'address': {'type': 'string'},
            'port': {'type': 'number'},
            'pubkey': {'type': 'string'},
            'hostkey': {'type': 'string'}
        },
        'additionalProperties': False
    })

    # Register providers
    plugin.register_provider('peer.freenas', PeerFreeNASProvider)

    # Register tasks
    plugin.register_task_handler("peer.freenas.create", FreeNASPeerCreateTask)
    plugin.register_task_handler("peer.freenas.create_local", FreeNASPeerCreateLocalTask)
    plugin.register_task_handler("peer.freenas.delete", FreeNASPeerDeleteTask)
    plugin.register_task_handler("peer.freenas.delete_local", FreeNASPeerDeleteLocalTask)
    plugin.register_task_handler("peer.freenas.update", FreeNASPeerUpdateTask)
    plugin.register_task_handler("peer.freenas.update_remote", FreeNASPeerUpdateRemoteTask)

    # Event handlers methods
    def on_connection_change(args):
        global ssh_port
        global hostname
        new_ssh_port = dispatcher.call_sync('service.sshd.get_config')['port']
        new_hostname = dispatcher.call_sync('system.general.get_config')['hostname']
        if ssh_port != new_ssh_port or hostname != new_hostname:
            logger.debug('Address or SSH port has been updated. Populating change to FreeNAS peers')
            ssh_port = new_ssh_port
            hostname = new_hostname
            ids = dispatcher.call_sync('peer.freenas.query', {'select': 'id'})
            try:
                for id in ids:
                    dispatcher.call_task_sync('peer.freenas.update_remote', id)
            except RpcException:
                pass

    # Register event handlers
    plugin.register_event_handler('service.sshd.changed', on_connection_change)
    plugin.register_event_handler('system.general.changed', on_connection_change)

    # Create home directory and authorized keys file for replication user
    if not os.path.exists(REPL_USR_HOME):
        os.mkdir(REPL_USR_HOME)
    ssh_dir = os.path.join(REPL_USR_HOME, '.ssh')
    if not os.path.exists(ssh_dir):
        os.mkdir(ssh_dir)
    with open(AUTH_FILE, 'w') as auth_file:
        for host in dispatcher.call_sync('peer.freenas.query'):
            auth_file.write(host['credentials']['pubkey'])
