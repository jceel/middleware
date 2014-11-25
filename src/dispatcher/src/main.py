#!/usr/local/bin/python2.7
#+
# Copyright 2014 iXsystems, Inc.
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
import sys
import fnmatch
import glob
import imp
import json
import logging
import logging.config
import logging.handlers
import argparse
import signal
import time
import uuid
import errno
import setproctitle

import gevent
from pyee import EventEmitter
from gevent import monkey, Greenlet
from gevent.event import AsyncResult
from gevent.wsgi import WSGIServer
from geventwebsocket import WebSocketServer, WebSocketApplication, Resource

from datastore import get_datastore
from dispatcher.rpc import RpcContext, RpcException
from services import ManagementService, EventService, TaskService, PluginService
from api.handler import ApiHandler
from balancer import Balancer
from auth import PasswordAuthenticator


DEFAULT_CONFIGFILE = '/data/middleware.conf'


class Dispatcher(object):
    def __init__(self):
        self.started_at = None
        self.preserved_files = []
        self.plugin_dirs = []
        self.event_types = []
        self.event_sources = {}
        self.event_handlers = {}
        self.plugins = {}
        self.threads = []
        self.queues = {}
        self.providers = {}
        self.tasks = {}
        self.logger = logging.getLogger('Main')
        self.rpc = None
        self.balancer = None
        self.datastore = None
        self.auth = None
        self.ws_server = None
        self.http_server = None
        self.pidfile = None
        self.use_tls = False
        self.certfile = None
        self.keyfile = None

    def init(self):
        self.datastore = get_datastore(
            self.config['datastore']['driver'],
            self.config['datastore']['dsn']
        )

        self.logger.info('Connected to datastore')
        self.require_collection('events', 'serial', 'log')
        self.require_collection('tasks', 'serial', 'log')

        self.balancer = Balancer(self)
        self.auth = PasswordAuthenticator(self)
        self.rpc = ServerRpcContext(self)
        self.rpc.register_service('management', ManagementService)
        self.rpc.register_service('event', EventService)
        self.rpc.register_service('task', TaskService)
        self.rpc.register_service('plugin', PluginService)

    def start(self):
        for name, clazz in self.event_sources.items():
            source = clazz(self)
            self.threads.append(gevent.spawn(source.run))

        self.started_at = time.time()
        self.balancer.start()

    def read_config_file(self, file):
        try:
            f = open(file, 'r')
            data = json.load(f)
            f.close()
        except (IOError, ValueError):
            raise

        if data['dispatcher']['logging'] == 'syslog':
            handler = logging.handlers.SysLogHandler('/var/run/log', facility='local3')
            logging.root.setLevel(logging.DEBUG)
            logging.root.handlers = []
            logging.root.addHandler(handler)
            self.preserved_files.append(handler.socket.fileno())
            self.logger.info('Initialized syslog logger')

        self.config = data
        self.plugin_dirs = data['dispatcher']['plugin-dirs']
        self.pidfile = data['dispatcher']['pidfile']

        if 'tls' in data['dispatcher'] and data['dispatcher']['tls'] == True:
            self.use_tls = True
            self.certfile = data['dispatcher']['tls-certificate']
            self.keyfile = data['dispatcher']['tls-keyfile']

    def discover_plugins(self):
        for dir in self.plugin_dirs:
            self.logger.debug("Searching for plugins in %s", dir)
            self.__discover_plugin_dir(dir)

    def reload_plugins(self):
        # Reload existing modules
        for i in self.plugins.values():
                imp.reload(i)

        # And look for new ones
        self.discover_plugins()

    def __discover_plugin_dir(self, dir):
        for i in glob.glob1(dir, "*.py"):
            self.__try_load_plugin(os.path.join(dir, i))

    def __try_load_plugin(self, path):
        if path in self.plugins:
            return

        self.logger.debug("Loading plugin from %s", path)
        plugin = imp.load_source("plugin", path)
        if hasattr(plugin, "_init"):
            plugin._init(self)
            self.plugins[path] = plugin

        self.dispatch_event("server.plugin.loaded", {"name": os.path.basename(path)})

    def dispatch_event(self, name, args):
        if 'timestamp' not in args:
            # If there's no timestamp, assume event fired right now
            args['timestamp'] = time.time()

        self.logger.debug("New event of type %s. Params: %s", name, args)
        self.ws_server.broadcast_event(name, args)

        if name in self.event_handlers:
            for h in self.event_handlers[name]:
                h(args)

        # Persist event
        event_data = args.copy()
        del event_data['timestamp']

        self.datastore.insert('events', {
            'name': name,
            'timestamp': args['timestamp'],
            'args': event_data
        })

    def register_event_handler(self, name, handler):
        if name not in self.event_handlers:
            self.event_handlers[name] = []

        self.event_handlers[name].append(handler)

    def register_event_source(self, name, clazz):
        self.logger.debug("New event source: %s provided by %s", name, clazz.__module__)
        self.event_sources[name] = clazz

    def register_task_handler(self, name, clazz):
        self.logger.debug("New task handler: %s", name)
        self.tasks[name] = clazz

    def register_provider(self, name, clazz):
        self.logger.debug("New provider: %s", name)
        self.providers[name] = clazz
        self.rpc.register_service(name, clazz)

    def register_schema_definition(self, name, definition):
        self.rpc.register_schema_definition(name, definition)

    def require_collection(self, collection, pkey_type='uuid', type='config'):
        if not self.datastore.collection_exists(collection):
            self.datastore.collection_create(collection, pkey_type, {'type': type})

    def die(self):
        self.logger.warning('Exiting from "die" command')
        gevent.killall(self.threads)
        sys.exit(0)


class ServerRpcContext(RpcContext):
    def __init__(self, dispatcher):
        super(ServerRpcContext, self).__init__()
        self.dispatcher = dispatcher

    def call_sync(self, name, *args):
        svcname, method = name.rpartition('.')
        svc = self.get_service(svcname)
        if svc is None:
            raise RpcException(errno.ENOENT, 'Service {0} not found'.format(svcname))

        if not hasattr(svc, method):
            raise RpcException(errno.ENOENT, 'Method {0} in service {1} not found'.format(method, svcname))

        return getattr(svc, method)(*args)


class ServerResource(Resource):
    def __init__(self, apps=None, dispatcher=None):
        super(ServerResource, self).__init__(apps)
        self.dispatcher = dispatcher

    def __call__(self, environ, start_response):
        environ = environ
        current_app = self._app_by_path(environ['PATH_INFO'])

        if current_app is None:
            raise Exception("No apps defined")

        if 'wsgi.websocket' in environ:
            ws = environ['wsgi.websocket']
            current_app = current_app(ws, self.dispatcher)
            current_app.ws = ws  # TODO: needed?
            current_app.handle()

            return None
        else:
            return current_app(environ, start_response)


class Server(WebSocketServer):
    def __init__(self, *args, **kwargs):
        super(Server, self).__init__(*args, **kwargs)
        self.connections = []

    def broadcast_event(self, event, args):
        for i in self.connections:
            i.emit_event(event, args)


class ServerConnection(WebSocketApplication, EventEmitter):
    def __init__(self, ws, dispatcher):
        super(ServerConnection, self).__init__(ws)
        self.server = ws.handler.server
        self.dispatcher = dispatcher
        self.server_pending_calls = {}
        self.client_pending_calls = {}
        self.user = None
        self.event_masks = set()

    def on_open(self):
        self.server.connections.append(self)
        self.dispatcher.dispatch_event('server.client_connected', {
            'address': self.ws.handler.client_address,
            'description': "Client {0} connected".format(self.ws.handler.client_address)
        })

    def on_close(self, reason):
        self.server.connections.remove(self)
        self.dispatcher.dispatch_event('server.client_disconnected', {
            'address': self.ws.handler.client_address,
            'description': "Client {0} disconnected".format(self.ws.handler.client_address)
        })

    def on_message(self, message, *args, **kwargs):
        if not type(message) is str:
            return

        if not "namespace" in message:
            return

        message = json.loads(message)

        getattr(self, "on_{}_{}".format(message["namespace"], message["name"]))(message["id"], message["args"])

    def on_events_subscribe(self, id, event_masks):
        if self.user is None:
            return

        self.event_masks = set.union(self.event_masks, event_masks)

    def on_events_unsubscribe(self, id, event_masks):
        if self.user is None:
            return

        self.event_masks = set.difference(self.event_masks, event_masks)

    def on_events_event(self, id, data):
        self.dispatcher.dispatch_event(data["name"], data["args"])

    def on_rpc_auth_service(self, id, data):
        service_name = data["name"]

        self.send_json({
            "namespace": "rpc",
            "name": "response",
            "id": id,
            "args": []
        })

        self.user = self.dispatcher.auth.get_service(service_name)
        self.dispatcher.dispatch_event('server.service_logged', {
            'address': self.ws.handler.client_address,
            'name': service_name,
            'description': "Service {0} logged in".format(service_name)
        })

    def on_rpc_auth(self, id, data):
        username = data["username"]
        password = data["password"]

        user = self.dispatcher.auth.get_user(username)

        if user is None:
            self.emit_rpc_error(id, errno.EACCES, "Incorrect username or password")
            return

        if not user.check_password(password):
            self.emit_rpc_error(id, errno.EACCES, "Incorrect username or password")
            return

        self.user = user
        self.send_json({
            "namespace": "rpc",
            "name": "response",
            "id": id,
            "args": []
        })

        self.dispatcher.dispatch_event('server.client_logged', {
            'address': self.ws.handler.client_address,
            'username': username,
            'description': "Client {0} logged in".format(username)
        })

    def on_rpc_response(self, id, data):
        if id not in self.client_pending_calls.keys():
            return

        call = self.client_pending_calls[id]
        if call['callback'] is not None:
            call['callback'](*data)

        if call['event'] is not None:
            call['event'].set(data)

        del self.client_pending_calls[id]

    def on_rpc_error(self, id, data):
        if id not in self.client_pending_calls.keys():
            return

        call = self.client_pending_calls[id]
        if call['event'] is not None:
            call['event'].set_exception(RpcException(data['code'], data['message']))

        del self.client_pending_calls[id]

    def on_rpc_call(self, id, data):
        def dispatch_call_async(id, method, args):
            try:
                result = self.dispatcher.rpc.dispatch_call(method, args, sender=self)
            except RpcException as err:
                self.send_json({
                    "namespace": "rpc",
                    "name": "error",
                    "id": id,
                    "args": {
                        "code": err.code,
                        "message": err.message
                    }
                })
            else:
                self.send_json({
                    "namespace": "rpc",
                    "name": "response",
                    "id": id,
                    "args": result
                })

        if self.user is None:
            self.emit_rpc_error(id, errno.EACCES, 'Not logged in')
            return

        method = data["method"]
        args = data["args"]

        greenlet = Greenlet(dispatch_call_async, id, method, args)
        self.server_pending_calls[id] = {
            "method": method,
            "args": args,
            "greenlet": greenlet
        }

        greenlet.start()

    def broadcast_event(self, event, args):
        for i in self.server.connections:
            i.emit_event(event, args)

    def call_client(self, method, callback, *args):
        id = uuid.uuid4()
        event = AsyncResult()
        self.client_pending_calls[str(id)] = {
            "method": method,
            "args": args,
            "callback": callback,
            "event": event
        }

        self.emit_rpc_call(id, method, args)
        return event

    def call_client_sync(self, method, *args, **kwargs):
        timeout = kwargs.pop('timeout', None)
        event = self.call_client(method, None, *args)
        return event.get(timeout=timeout)

    def emit_event(self, event, args):
        for i in self.event_masks:
            if not fnmatch.fnmatch(event, i):
                continue

            self.send_json({
                "namespace": "events",
                "name": "event",
                "id": None,
                "args": {
                    "name": event,
                    "args": args
                }
            })

    def emit_rpc_call(self, id, method, args):
        payload = {
            "namespace": "rpc",
            "name": "call",
            "id": str(id),
            "args": {
                "method": method,
                "args": args
            }
        }

        return self.send_json(payload)

    def emit_rpc_error(self, id, code, message, extra=None):
        payload = {
            "namespace": "rpc",
            "name": "error",
            "id": str(id),
            "args": {
                "code": code,
                "message": message
            }
        }

        if extra is not None:
            payload['args'].update(extra)

        return self.send_json(payload)

    def send_json(self, obj):
        self.ws.send(json.dumps(obj))


def run(d, args):
    setproctitle.setproctitle('server')
    monkey.patch_all()

    # Signal handlers
    gevent.signal(signal.SIGQUIT, d.die)
    gevent.signal(signal.SIGQUIT, d.die)
    gevent.signal(signal.SIGINT, d.die)

    # WebSockets server
    if d.use_tls:
        s = Server(('', args.p), ServerResource({
            '/socket': ServerConnection,
            '/api': ApiHandler(d)
        }, dispatcher=d), certfile=d.certfile, keyfile=d.keyfile)
    else:
        s = Server(('', args.p), ServerResource({
            '/socket': ServerConnection,
            '/api': ApiHandler(d)
        }, dispatcher=d))

    d.ws_server = s
    serv_thread = gevent.spawn(s.serve_forever)

    if args.s:
        # Debugging frontend server
        from frontend import frontend
        if d.use_tls:
            http_server = WSGIServer(('', args.s), frontend.app, certfile=d.certfile, keyfile=d.keyfile)
        else:
            http_server = WSGIServer(('', args.s), frontend.app)

        gevent.spawn(http_server.serve_forever)
        logging.info('Frontend server listening on port %d', args.s)

    d.init()
    d.discover_plugins()
    d.start()
    gevent.joinall(d.threads + [serv_thread])


def main():
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', type=int, metavar='PORT', default=8180, help="Run debug frontend server on port")
    parser.add_argument('-p', type=int, metavar='PORT', default=5000, help="WebSockets server port")
    parser.add_argument('-c', type=str, metavar='CONFIG', default=DEFAULT_CONFIGFILE, help='Configuration file path')
    args = parser.parse_args()

    # Initialization and dependency injection
    d = Dispatcher()
    try:
        d.read_config_file(args.c)
    except IOError, err:
        logging.fatal("Cannot read config file {0}: {1}".format(args.c, str(err)))
        sys.exit(1)
    except ValueError, err:
        logging.fatal("Cannot parse config file {0}: {1}".format(args.c, str(err)))
        sys.exit(1)

    run(d, args)


if __name__ == '__main__':
    main()