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
import logging
import crypt
import random
import string
import datetime
import errno
import hashlib
import binascii
import select
import threading
from plugin import DirectoryServicePlugin
from freenas.dispatcher.jsonenc import load, dump
from freenas.utils import first_or_default
from freenas.utils.query import wrap


PASSWD_FILE = '/etc/passwd.json'
GROUP_FILE = '/etc/group.json'
logger = logging.getLogger(__name__)


def crypted_password(cleartext):
    return crypt.crypt(cleartext, '$6$' + ''.join([
        random.choice(string.ascii_letters + string.digits) for _ in range(16)]))


class FlatFilePlugin(DirectoryServicePlugin):
    def __init__(self, context):
        self.context = context
        self.passwd = wrap([])
        self.group = wrap([])
        self.__load()
        self.watch_thread = threading.Thread(target=self.__watch, daemon=True)
        self.watch_thread.start()

    def __load(self):
        try:
            with open(PASSWD_FILE, 'r') as f:
                self.passwd = wrap(load(f))
        except (IOError, ValueError) as err:
            logger.warn('Cannot read {0}: {1}'.format(PASSWD_FILE, str(err)))

        try:
            with open(GROUP_FILE, 'r') as f:
                self.group = wrap(load(f))
        except (IOError, ValueError) as err:
            logger.warn('Cannot read {0}: {1}'.format(GROUP_FILE, str(err)))

    def __watch(self):
        kq = select.kqueue()
        passwd_fd = os.open(PASSWD_FILE, os.O_RDONLY)
        group_fd = os.open(GROUP_FILE, os.O_RDONLY)

        ev = [
            select.kevent(
                passwd_fd,
                filter=select.KQ_FILTER_VNODE, flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME
            ),
            select.kevent(
                group_fd,
                filter=select.KQ_FILTER_VNODE, flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME
            )
        ]

        kq.control(ev, 0)

        while True:
            event, = kq.control(None, 1)
            name = PASSWD_FILE if event.ident == passwd_fd else GROUP_FILE
            logger.warning('{0} was modified, reloading'.format(name))
            self.__load()

    def getpwent(self, filter=None, params=None):
        return self.passwd.query(*(filter or []), **(params or {}))

    def getpwnam(self, name):
        return self.passwd.query(('username', '=', name), single=True)

    def getpwuid(self, uid):
        return self.passwd.query(('uid', '=', uid), single=True)

    def getgrent(self, filter=None, params=None):
        return self.group.query(*(filter or []), **(params or {}))

    def getgrnam(self, name):
        return self.group.query(('name', '=', name), single=True)

    def getgrgid(self, gid):
        return self.group.query(('gid', '=', gid), single=True)

    def change_password(self, username, password):
        try:
            with open(PASSWD_FILE, 'r') as f:
                passwd = wrap(load(f))

            user = first_or_default(lambda u: u['username'] == username, passwd)
            if not user:
                raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))

            nthash = hashlib.new('md4', password.encode('utf-16le')).digest()
            user.update({
                'unixhash': crypted_password(password),
                'smbhash': binascii.hexlify(nthash).decode('utf-8'),
                'password_changed_at': datetime.datetime.utcnow()
            })

            with open(PASSWD_FILE + '.tmp', 'w') as f:
                dump(passwd, f, indent=4)

            os.rename(PASSWD_FILE + '.tmp', PASSWD_FILE)
            self.__load()
        except (IOError, ValueError) as err:
            logger.warn('Cannot change password: {1}'.format(str(err)))
            raise


def _init(context):
    context.register_plugin('file', FlatFilePlugin)