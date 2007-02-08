import sys, os
import py
import time
import thread, threading 
from py.__.test.rsession.master import MasterNode
from py.__.test.rsession.slave import setup_slave

from py.__.test.rsession import repevent

class HostInfo(object):
    """ Class trying to store all necessary attributes
    for host
    """
    _hostname2list = {}
    localdest = None
    
    def __init__(self, spec):
        parts = spec.split(':', 1)
        self.hostname = parts.pop(0)
        if parts and parts[0]:
            self.relpath = parts[0]
        else:
            self.relpath = "pytestcache-" + self.hostname
        if spec.find(':') == -1 and self.hostname == 'localhost':
            self.rsync_flag = False
        else:
            self.rsync_flag = True
        self.hostid = self._getuniqueid(self.hostname) 

    def _getuniqueid(self, hostname):
        l = self._hostname2list.setdefault(hostname, [])
        hostid = hostname + "[%d]" % len(l)
        l.append(hostid)
        return hostid

    def initgateway(self, python="python"):
        assert not hasattr(self, 'gw')
        if self.hostname == "localhost":
            gw = py.execnet.PopenGateway(python=python)
        else:
            gw = py.execnet.SshGateway(self.hostname, 
                                       remotepython=python)
        self.gw = gw
        channel = gw.remote_exec(py.code.Source(
            gethomedir, 
            getpath_relto_home, """
            import os
            os.chdir(gethomedir())
            newdir = getpath_relto_home(%r)
            # we intentionally don't ensure that 'newdir' exists 
            channel.send(newdir)
            """ % str(self.relpath)
        ))
        self.gw_remotepath = channel.receive()

    def __str__(self):
        return "<HostInfo %s:%s>" % (self.hostname, self.relpath)

    def __repr__(self):
        return str(self)

    def __hash__(self):
        return hash(self.hostid)

    def __eq__(self, other):
        return self.hostid == other.hostid

    def __ne__(self, other):
        return not self == other

class HostRSync(py.execnet.RSync):
    """ RSyncer that filters out common files 
    """
    def __init__(self, source, *args, **kwargs):
        self._synced = {}
        ignores= None
        if 'ignores' in kwargs:
            ignores = kwargs.pop('ignores')
        self._ignores = ignores or []
        kwargs['delete'] = True
        super(HostRSync, self).__init__(source, **kwargs)

    def filter(self, path):
        path = py.path.local(path)
        if not path.ext in ('.pyc', '.pyo'):
            if not path.basename.endswith('~'): 
                if path.check(dotfile=0):
                    for x in self._ignores:
                        if path == x:
                            break
                    else:
                        return True

    def add_target_host(self, host, reporter=lambda x: None,
                        destrelpath=None, finishedcallback=None):
        key = host.hostname, host.relpath 
        if not host.rsync_flag or key in self._synced:
            if finishedcallback:
                finishedcallback()
            return False
        self._synced[key] = True
        # the follow attributes are set from host.initgateway()
        gw = host.gw
        remotepath = host.gw_remotepath
        if destrelpath is not None:
            remotepath = os.path.join(remotepath, destrelpath)
        super(HostRSync, self).add_target(gw, 
                                          remotepath, 
                                          finishedcallback)
        return remotepath 

class HostManager(object):
    def __init__(self, config, hosts=None):
        self.config = config
        if hosts is None:
            hosts = self.config.getvalue("dist_hosts")
            hosts = [HostInfo(x) for x in hosts]
        self.hosts = hosts
        roots = self.config.getvalue_pathlist("dist_rsync_roots")
        if roots is None:
            roots = [self.config.topdir]
        self.roots = roots

    def prepare_gateways(self, reporter):
        dist_remotepython = self.config.getvalue("dist_remotepython")
        for host in self.hosts:
            host.initgateway(python=dist_remotepython)
            reporter(repevent.HostGatewayReady(host, self.roots))
            host.gw.host = host

    def init_rsync(self, reporter):
        # send each rsync root
        ignores = self.config.getvalue_pathlist("dist_rsync_ignore")
        self.prepare_gateways(reporter)
        for root in self.roots:
            rsync = HostRSync(root, ignores=ignores, 
                              verbose=self.config.option.verbose)
            destrelpath = root.relto(self.config.topdir)
            for host in self.hosts:
                def donecallback(host, root):
                    reporter(repevent.HostRSyncRootReady(host, root))
                remotepath = rsync.add_target_host(
                    host, reporter, destrelpath, finishedcallback=
                    lambda host=host, root=root: donecallback(host, root))
                reporter(repevent.HostRSyncing(host, root, remotepath))
            rsync.send_if_targets()

    def setup_hosts(self, reporter):
        self.init_rsync(reporter)
        nodes = []
        for host in self.hosts:
            if hasattr(host.gw, 'remote_exec'): # otherwise dummy for tests :/
                ch = setup_slave(host, self.config)
                nodes.append(MasterNode(ch, reporter))
        return nodes

    def teardown_hosts(self, reporter, channels, nodes,
                       waiter=lambda : time.sleep(.1), exitfirst=False):
        for channel in channels:
            channel.send(None)
    
        clean = exitfirst
        while not clean:
            clean = True
            for node in nodes:
                if node.pending:
                    clean = False
            waiter()
        self.teardown_gateways(reporter, channels)

    def kill_channels(self, channels):
        for channel in channels:
            channel.send(42)

    def teardown_gateways(self, reporter, channels):
        for channel in channels:
            #try:
            try:
                repevent.wrapcall(reporter, channel.waitclose, 1)
            except IOError: # timeout
                # force closing
                channel.close()
            channel.gateway.exit()

def gethomedir():
    import os
    homedir = os.environ.get('HOME', '')
    if not homedir:
        homedir = os.environ.get('HOMEPATH', '.')
    return os.path.abspath(homedir)

def getpath_relto_home(targetpath):
    import os
    if not os.path.isabs(targetpath):
        homedir = gethomedir()
        targetpath = os.path.join(homedir, targetpath)
    return targetpath
