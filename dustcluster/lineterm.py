# Copyright (c) Ran Dugal 2014
#
# This file is part of dust.
#
# Licensed under the GNU Affero General Public License v3, which is available at
# http://www.gnu.org/licenses/agpl-3.0.html
# 
# This program is distributed in the hope that it will be useful, but WITHOUT 
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero GPL for more details.
#

''' invoke commands or a shell over ssh sessions,  demultiplex the ssh output '''  

import getpass
from threading import Thread
import select
import socket
import sys
import os

from paramiko.py3compat import u
import paramiko

from dustcluster.util import setup_logger
logger = setup_logger( __name__ )


# Once a session has been setup a program at the remote end can be  
# executed with SSH_MSG_CHANNEL_REQUEST, with string 'shell', 'exec', or 
# 'subsystem' for a default shell, single command, or subsystem. 
# Here we start a full interactive shell and invoke commands on it, 
# demultiplexing the output from all chanells onto stdout locally. 
# This allows us to execute arbitrarily interactive scripts. 

class ReceiveDemux(object):
    ''' receive demultiplexer for all open ssh interactive shells ''' 

    def __init__(self, session_mgr):
        self.refresh_callback = None
        self.chans = {} # { chan : receive_buffer }
        self.session_mgr = session_mgr
        self.state = 'created'
        self.thread = Thread(target=self.receive_loop)
        self.thread.daemon = True
        self.thread.start()

    def start(self, sshterm):
        ''' start demuxing output on this term '''
        self.chans[sshterm.chan] = sshterm

    def stop(self, chan):
        ''' stop receiving on chan, remove session ''' 
        term = self.chans[chan]
        del self.chans[chan]
        self.session_mgr.remove_session(term)

    def shutdown(self):
        ''' shut down receiver thread '''
        self.state = 'shutdown'
        self.thread.join()

    def receive_loop(self):
        ''' demux receive loop '''


        while self.state != 'shutdown':
            r, w, e = select.select(self.chans, [], self.chans, 0.5)
            if r:
                for achan in r:
                    try:
                        readbytes = u(achan.recv(1024))
                        if len(readbytes) == 0:
                            sys.stdout.write('\r\SSH session disconnected.\r\n')
                            sys.stdout.flush()
                            self.stop(achan)
                            break

                        sshterm = self.chans[achan]
                        if sshterm.raw_shell_mode:
                            sys.stdout.write(readbytes)
                            sys.stdout.flush()
                            
                        else:
                            sshterm.recvbuf = sshterm.recvbuf + readbytes

                    except socket.timeout:
                        pass

            if e:
                for achan in e:
                    self.stop(achan)

            if not r and not e:

                wrote_output = False
                for chan, sshterm in  self.chans.iteritems():

                    if not sshterm.recvbuf:
                        continue

                    if sshterm.raw_shell_mode:
                        continue

                    if not sshterm.login_guid_found:
                    # surpress login banner. disabled for now.
                    # Note: RFC-4254 reccomends the use of magic cookeis to surpress spurious
                    # output when starting a subsystem via the shell "to distinguish it from 
                    # arbitrary output generated by shell initialization scripts, etc. This spurious 
                    # output from the shell may be filtered out either at the server or at the client"
                    # We can reuse this trick to surpress login banner text and echo enable/disable.
                        pos1 = sshterm.recvbuf.find( SSHTerm.login_complete_guid )
                        if pos1 != -1:
                            pos2 = sshterm.recvbuf.rfind( SSHTerm.login_complete_guid )
                            if pos1 != pos2:
                                sshterm.login_guid_found = True
                                guid_len = len(SSHTerm.login_complete_guid)
                                sshterm.recvbuf = sshterm.recvbuf[pos2 + guid_len:]

                    if sshterm.recvbuf.strip():
                        sys.stdout.write('\n')
                        prefix = "\n\033[1m[%s]\033[21m " % sshterm.node.name
                        sys.stdout.write(prefix)
                        sys.stdout.write(sshterm.recvbuf.replace('\n', prefix))
                        sys.stdout.flush()
                        sshterm.recvbuf = u('')
                        sys.stdout.write('\n')
                        sys.stdout.flush()
                        wrote_output = True

                #reset prompt
                if wrote_output and self.refresh_callback:
                    self.refresh_callback()

        return 0


class SessionManager(object):
    ''' holds a map of node ids to ssh sessions
        registers/unregisters ssh sessions with the demultiplexer 
    '''

    def __init__(self):
        self.demux = ReceiveDemux(self)
        self.session_map = {}

    def remove_session(self, term):
    
        if term.raw_shell_mode:
            logger.info('Logged out of ssh session. Press enter to continue.\n\r')
            term.raw_shell_mode = True
            term.revert_tty()

        for nodeid, nodeterm in self.session_map.items():
            if nodeterm == term:
                del self.session_map[nodeid]

    def shutdown(self):
        for term in self.session_map.values():
            term.shutdown()

        self.demux.shutdown()

    def term_from_node(self, node, keyfile):

        term = self.session_map.get(node.id)

        #term = node.context.get('sshterm')

        if not term:
            term = SSHTerm(node, keyfile)
            term.login()
            self.demux.start(term)
            self.session_map[node.id] = term

        if not term.is_connected():
            logger.info('no ssh connection, logging in')
            term.login()

        return term


class SSHTerm(object):
    '''
    Ssh client session - starts an interactive ssh shell
    Takes line input commands or enters a char bufferred raw terminal
    '''

    prompt = "dust:ssh:%s:$ "

    login_complete_guid = 'B79D8677-F58A-4E09-B917-855A6619A951' # GUID

    def __init__(self, node, keyfile):
        self.prompt = "dust:ssh:%s:$ " % node.name
        self.node = node
        self.keyfile = keyfile

        self.state = 'not_connected'
        self.transport  = None
        self.chan = None
        self.newchan = None

        self.recvbuf = u('')
        self.login_guid_found = True

        self.raw_shell_mode = False
        self.oldattrs  = None

        self.sftp = None # sftp subservice

        self.echo  = True

    def is_connected(self):
        return self.transport and self.transport.is_authenticated() and self.transport.is_active()

    def login(self):
        hostname = self.node.hostname
        username = self.node.username

        logger.debug('hostname=%s, username=%s, key=%s' % (hostname, username, self.keyfile))
        if not self.is_connected():
            try:
                self.connect(hostname, username)
                self.transport.set_keepalive(60*4)
                self.state = 'connected'
                #self.disable_echo(auxcmd= "; echo %s" % self.login_complete_guid)
            except:
                logger.error('error on ssh login on host %s :' % (hostname))
                raise

    def disable_echo(self, auxcmd=''):
        logger.info( '%s: disabling echo' % self.node.name )
        cmd = "stty -echo; export PS1='' "
        self.command(cmd + auxcmd)
        self.echo = False

    def enable_echo(self, auxcmd=''):
        logger.info( '%s: enabling echo' % self.node.name )
        #prompt = "\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ "
        prompt = "rawshell:%s:\w\$ " % self.node.name
        cmd = "stty echo; export PS1='%s'" % prompt
        self.command(cmd + auxcmd)
        self.echo = True

    def raw_shell_other(self):
        print "raw mode shell not suppported on this system yet."
        return

    def raw_shell(self):

        if os.name == 'posix':
            self.raw_shell_posix()
        else:
            self.raw_shell_other()

    def raw_shell_posix(self):

        import termios
        import tty

        self.raw_shell_mode = True

        self.oldattrs = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        self.chan.settimeout(0.0)

        ctrlc_count = 0

        ctrlout = False
        while self.raw_shell_mode:
            try:
                d = sys.stdin.read(1)
                if not d:
                    break

                if ord(d) == 3:
                    ctrlc_count += 1
                else:
                    ctrlc_count = 0

                if ctrlc_count > 2:
                    ctrlout = True
                    break

                self.chan.send(d)
            except:
                logger.exception('exception in raw shell:')

        if ctrlout:
            logger.info( '%s: switching back to line buffered commands' % self.node.name )
            self.disable_echo()

        self.revert_tty()

    def revert_tty(self):
        ''' revert tty attribute '''
        import termios
        self.raw_shell_mode = False
        if ( self.oldattrs ):
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.oldattrs)

    def shutdown(self):
        ''' shutdown this ssh term '''
        self.state = 'shutdown'
        if self.chan:
            self.chan.close()
        if self.transport:
            self.transport.close()
        logger.info( '%s: closed ssh' % self.node.name )

    def command(self, line):
        ''' send a shell command to the interactive ssh shell '''

        if self.state != 'connected':
            logger.info( 'session not connected' )
            return

        if not self.is_connected():
            logger.info( 'ssh session not connected, authed, or active' )
            return

        self.chan.send(line)
        self.chan.send('\n')

    #TODO: override port from template
    def connect(self, hostname, username, port=22):
        ''' connect and authenticate ''' 

        private_key_path = self.keyfile 

        logger.info('ssh login to %s' % hostname)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((hostname, port))

        self.transport = paramiko.Transport(sock)
        self.transport.start_client()

        #TODO: check host key

        try:
            key = paramiko.RSAKey.from_private_key_file(private_key_path)
        except paramiko.PasswordRequiredException:
            password = getpass.getpass('RSA key password: ')
            key = paramiko.RSAKey.from_private_key_file(private_key_path, password)

        if key:
            self.transport.auth_publickey(username, key)

        if not self.transport.is_authenticated():
            self.transport.close()
            raise Exception('Authentication failed.')

        self.chan = self.transport.open_session()
        self.chan.get_pty()
        self.chan.invoke_shell()



class LineTerm(object):
    '''
    top level api - implements ssh and raw terminal functionality for a set of nodes 
    '''

    def __init__(self):
        self.session_manager = SessionManager()

    def set_refresh_callback(self, callback):
        '''Optional callback after a block of ssh output is written to stdout. 
            for commands issued in interactive mode this need not be the end of output 
        '''
        self.session_manager.demux.refresh_callback = callback

    def command(self, keyfile, node, cmd=None):
        ''' send a command to an interactive ssh shell or enter a raw shell input loop.
            in both cases log in if not logged in. 
        '''
        term = None
        try:
            term = self.session_manager.term_from_node(node, keyfile)

            if cmd:
                if term.echo:
                    term.disable_echo()
                term.command(cmd)
            else:
                if not term.echo:
                    term.enable_echo()
                term.raw_shell()
        except Exception, e:
            logger.error(e)
        finally:
            if term:
                term.revert_tty()

    def shell(self, keyfile, node):

        logger.info(\
         '*** Entering raw shell, press ctrl-c thrice to return to cluster shell. Press Enter to continue.***')

        raw_input()
        self.command(keyfile, node, cmd=None)

    def put(self, keyfile, node, srcfile, destfile=None):

        if not os.path.isfile(srcfile):
            logger.error('file does not exist locally : %s' % srcfile)
            return

        try:
            term = self.session_manager.term_from_node(node, keyfile)
            if not term.sftp:
                term.sftp = paramiko.SFTPClient.from_transport(term.transport)

            sftp = term.sftp
            fname = os.path.basename(srcfile)
            if not destfile:
                destfile = fname
            ret = sftp.put(srcfile, destfile, confirm=True)
            
            if not getattr(ret, 'filename', None):
                ret.filename = os.path.basename(destfile)
            
            logger.info('uploaded to %s : %s' % (node.name, ret))
        except Exception, e:
            logger.error(e)


    def get(self, keyfile, node, remotefile, localdir):

        if localdir and not os.path.isdir(localdir):
            logger.error('dir does not exist locally : %s' % localdir)
            return

        try:
            term = self.session_manager.term_from_node(node, keyfile)
            if not term.sftp:
                term.sftp = paramiko.SFTPClient.from_transport(term.transport)

            fname = os.path.basename(remotefile)
            if localdir:
                localfile = os.path.join(localdir, fname)
            else:
                localfile = fname

            localfile = '%s.%s' % (localfile, node.name)

            sftp = term.sftp
            sftp.get(remotefile, localfile)

            logger.info('downloaded from %s : %s' % (node.name, localfile))
        except Exception, e:
            logger.error(e)

    def shutdown(self):
        self.session_manager.shutdown()

