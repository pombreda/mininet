"""
Node objects for Mininet.

Nodes provide a simple abstraction for interacting with hosts, switches
and controllers. Local nodes are simply one or more processes on the local
machine.

Node: superclass for all (primarily local) network nodes.

Host: a virtual host. By default, a host is simply a shell; commands
    may be sent using Cmd (which waits for output), or using sendCmd(),
    which returns immediately, allowing subsequent monitoring using
    monitor(). Examples of how to run experiments using this
    functionality are provided in the examples/ directory.

Switch: superclass for switch nodes.

UserSwitch: a switch using the user-space switch from the OpenFlow
    reference implementation.

KernelSwitch: a switch using the kernel switch from the OpenFlow reference
    implementation.

OVSKernelSwitchNew: a switch using the OpenVSwitch OpenFlow-compatible switch
    implementation (openvswitch.org). Supports all 1.x version. Uses 
    ovsdb-server and vswitchd (which will be started on demand if 
    necessary.

OVSKernelSwitch: a switch using the OpenVSwitch OpenFlow-compatible switch
    implementation (openvswitch.org). Only works with 1.0.x and 1.1.x BUT
    does not use ovsdb-server. For environments in which the ovsdb file
    hasn't been set up. 

OVSUserSwitch: a switch using the user-space OpenVSwitch 
    OpenFlow-compatible switch implementation (openvswitch.org).
    Not working.

Controller: superclass for OpenFlow controllers. The default controller
    is controller(8) from the reference implementation.

NOXController: a controller node using NOX (noxrepo.org).

RemoteController: a remote controller node, which may use any
    arbitrary OpenFlow-compatible controller, and which is not
    created or managed by mininet.

LinuxBridge: a non-openflow switch that simply uses a linux bridge interface

Future enhancements:

- Possibly make Node, Switch and Controller more abstract so that
  they can be used for both local and remote nodes

- Create proxy objects for remote nodes (Mininet: Cluster Edition)
"""

import os
import pty
import re
import signal
import select
import sys
from subprocess import Popen, PIPE, STDOUT
from time import sleep

from mininet.log import info, error, debug
from mininet.util import quietRun, makeIntfPair, moveIntf, isShellBuiltin
from mininet.moduledeps import moduleDeps, pathCheck, checkRunning, OVS_KMOD, OF_KMOD, TUN

SWITCH_PORT_BASE = 1  # For OF > 0.9, switch ports start at 1 rather than zero

class Node( object ):
    """A virtual network node is simply a shell in a network namespace.
       We communicate with it using pipes."""

    inToNode = {}  # mapping of input fds to nodes
    outToNode = {}  # mapping of output fds to nodes

    portBase = 0  # Nodes always start with eth0/port0, even in OF 1.0

    def __init__( self, name, inNamespace=True,
        defaultMAC=None, defaultIP=None, prefix='n', **kwargs ):
        """name: name of node
           inNamespace: in network namespace?
           defaultMAC: default MAC address for intf 0
           defaultIP: default IP address for intf 0"""
        self.name = name
        self.inNamespace = inNamespace
        self.defaultIP = defaultIP
        self.defaultMAC = defaultMAC
        self.prefix = prefix
        opts = '-cdp'
        if self.inNamespace:
            opts += 'n'
        cmd = [ 'sudo', '-E', 'env', 'PATH=%s' % os.environ['PATH'],
                'PS1=' + chr( 127 ), 'mnexec', opts, 'bash', '--norc' ]
        # Spawn a shell subprocess in a pseudo-tty, to disable buffering
        # in the subprocess and insulate it from signals (e.g. SIGINT)
        # received by the parent
        master, slave = pty.openpty()
        self.shell = Popen( cmd, stdin=slave, stdout=slave, stderr=slave,
            close_fds=False )
        self.stdin = os.fdopen( master )
        self.stdout = self.stdin
        self.pollOut = select.poll()
        self.pollOut.register( self.stdout )
        # Maintain mapping between file descriptors and nodes
        # This is useful for monitoring multiple nodes
        # using select.poll()
        self.outToNode[ self.stdout.fileno() ] = self
        self.inToNode[ self.stdin.fileno() ] = self
        self.intfs = {}  # dict of port numbers to interface names
        self.ports = {}  # dict of interface names to port numbers
                         # replace with Port objects, eventually ?
        self.ips = {}  # dict of interfaces to ip addresses as strings
        self.macs = {}  # dict of interfacesto mac addresses as strings
        self.connection = {}  # remote node connected to each interface
        self.execed = False
        self.lastCmd = None
        self.lastPid = None
        self.readbuf = ''
        self.waiting = False
        # Stash additional information as desired
        self.args = kwargs
        x = ""
        while "\n" not in x:
            self.waitReadable()
            x += self.read(1)
        self.pid = int(x[1:-1])
        self.serial = 0

    @classmethod
    def fdToNode( cls, fd ):
        """Return node corresponding to given file descriptor.
           fd: file descriptor
           returns: node"""
        node = Node.outToNode.get( fd )
        return node or Node.inToNode.get( fd )

    def cleanup( self ):
        "Help python collect its garbage."
        self.shell = None

    # Subshell I/O, commands and control
    def read( self, bytes=1024 ):
        """Buffered read from node, non-blocking.
           bytes: maximum number of bytes to return"""
        count = len( self.readbuf )
        if count < bytes:
            data = os.read( self.stdout.fileno(), bytes - count )
            self.readbuf += data
        if bytes >= len( self.readbuf ):
            result = self.readbuf
            self.readbuf = ''
        else:
            result = self.readbuf[ :bytes ]
            self.readbuf = self.readbuf[ bytes: ]
        return result

    def readline( self ):
        """Buffered readline from node, non-blocking.
           returns: line (minus newline) or None"""
        self.readbuf += self.read( 1024 )
        if '\n' not in self.readbuf:
            return None
        pos = self.readbuf.find( '\n' )
        line = self.readbuf[ 0 : pos ]
        self.readbuf = self.readbuf[ pos + 1: ]
        return line

    def write( self, data ):
        """Write data to node.
           data: string"""
        os.write( self.stdin.fileno(), data )

    def terminate( self ):
        "Send kill signal to Node and clean up after it."
        quietRun( 'kill ' + str( self.pid ) )
        self.cleanup()

    def stop( self ):
        "Stop node."
        self.terminate()

    def waitReadable( self, timeoutms=None ):
        """Wait until node's output is readable.
           timeoutms: timeout in ms or None to wait indefinitely."""
        if len( self.readbuf ) == 0:
            self.pollOut.poll( timeoutms )

    def sendCmd( self, *args, **kwargs ):
        """Send a command, and return without waiting for the command
           to complete.
           args: command and arguments, or string
           printPid: print command's PID?"""
        assert not self.waiting
        self.serial += 1
        self.write( 'echo __   %s   __\n' % self.serial )
        match = '__ %s __' % self.serial
        buf = ''
        while True:
            i = buf.find( match )
            if i >= 0:
                buf = buf[ i + len( match ): ]
                break
            buf += self.read( 1024 )
        while True:
            if chr( 127 ) in buf:
                break
            buf += self.read( 1024 )
        printPid = kwargs.get( 'printPid', True )
        if len( args ) > 0:
            cmd = args
        if not isinstance( cmd, str ):
            cmd = ' '.join( cmd )
        if not re.search( r'\w', cmd ):
            # Replace empty commands with something harmless
            cmd = 'echo -n'
        if printPid and not isShellBuiltin( cmd ):
            use_mnexec = kwargs.get( 'mn_use_mnexec', True)
            if use_mnexec:
                cmd = 'mnexec -p ' + cmd
            disable_io_buf = kwargs.get( 'mn_disable_io_buf', False)
            if disable_io_buf:
                cmd = 'stdbuf -i0 -o0 -e0 ' + cmd
        self.write( cmd + '\n' )
        wait_flag = kwargs.get( 'mn_wait', True)
        if wait_flag:
            self.lastCmd = cmd
            self.lastPid = None
            self.waiting = True

    def sendInt( self, sig=signal.SIGINT ):
        "Interrupt running command."
        self.write( chr( 3 ) )

    def monitor( self, timeoutms=None ):
        """Monitor and return the output of a command.
           Set self.waiting to False if command has completed.
           timeoutms: timeout in ms or None to wait indefinitely."""
        self.waitReadable( timeoutms )
        data = self.read( 1024 )
        # Look for PID
        marker = chr( 1 ) + r'\d+\r\n'
        if chr( 1 ) in data:
            markers = re.findall( marker, data )
            if markers:
                self.lastPid = int( markers[ 0 ][ 1: ] )
                data = re.sub( marker, '', data )
        # Look for sentinel/EOF
        if len( data ) > 0 and data[ -1 ] == chr( 127 ):
            self.waiting = False
            data = data[ :-1 ]
        elif chr( 127 ) in data:
            self.waiting = False
            data = data.replace( chr( 127 ), '' )
        return data

    def waitOutput( self, verbose=False, pattern=None ):
        """Wait for a command to complete or generate certain output.
           Completion is signaled by a sentinel character, ASCII(127)
           appearing in the output stream.  Wait for the sentinel or
           output matched by a certain pattern, and return the output.
           verbose: print output interactively
           pattern: compiled regexp or None"""
        log = info if verbose else debug
        output = ''
        while self.waiting and (pattern is None or
                                not pattern.search(output)):
            data = self.monitor()
            output += data
            log( data )
        return output

    def cmd( self, *args, **kwargs ):
        """Send a command, wait for output, and return it.
           cmd: string"""
        verbose = kwargs.get( 'verbose', False )
        log = info if verbose else debug
        log( '*** %s : %s\n' % ( self.name, args ) )
        self.sendCmd( *args, **kwargs )
        return self.waitOutput( verbose )

    def cmdPrint( self, *args):
        """Call cmd and printing its output
           cmd: string"""
        return self.cmd( *args, **{ 'verbose': True } )

    # Interface management, configuration, and routing

    # BL notes: This might be a bit redundant or over-complicated.
    # However, it does allow a bit of specialization, including
    # changing the canonical interface names. It's also tricky since
    # the real interfaces are created as veth pairs, so we can't
    # make a single interface at a time.

    def intfName( self, n ):
        "Construct a canonical interface name node-ethN for interface n."
        return self.name + '-eth' + repr( n )

    def intfToPort(self, intf):
        index = intf.rfind('-eth')
        if index < 0:
            return None
        return int(intf[index + len('-eth'):])
            
    def newPort( self ):
        "Return the next port number to allocate."
        if len( self.ports ) > 0:
            return max( self.ports.values() ) + 1
        return self.portBase

    def addIntf( self, intf, port=None ):
        """Add an interface.
           intf: interface name (e.g. nodeN-ethM)
           port: port number (optional, typically OpenFlow port number)"""
        if port is None:
            port = self.newPort()
        self.intfs[ port ] = intf
        self.ports[ intf ] = port
        #info( '\n' )
        #info( 'added intf %s:%d to node %s\n' % ( intf,port, self.name ) )
        if self.inNamespace:
            #info( 'moving w/inNamespace set\n' )
            moveIntf( intf, self )

    def registerIntf( self, intf, dstNode, dstIntf ):
        "Register connection of intf to dstIntf on dstNode."
        self.connection[ intf ] = ( dstNode, dstIntf )

    def connectionsTo( self, node):
        "Return [(srcIntf, dstIntf)..] for connections to dstNode."
        # We could optimize this if it is important
        connections = []
        for intf in self.connection.keys():
            dstNode, dstIntf = self.connection[ intf ]
            if dstNode == node:
                connections.append( ( intf, dstIntf ) )
        return connections

    # This is a symmetric operation, but it makes sense to put
    # the code here since it is tightly coupled to routines in
    # this class. For a more symmetric API, you can use
    # mininet.util.createLink()

    def linkTo( self, node2, port1=None, port2=None ):
        """Create link to another node, making two new interfaces.
           node2: Node to link us to
           port1: our port number (optional)
           port2: node2 port number (optional)
           returns: intf1 name, intf2 name"""
        node1 = self
        if port1 is None:
            port1 = node1.newPort()
        if port2 is None:
            port2 = node2.newPort()
        intf1 = node1.intfName( port1 )
        intf2 = node2.intfName( port2 )
        makeIntfPair( intf1, intf2 )
        node1.addIntf( intf1, port1 )
        node2.addIntf( intf2, port2 )
        node1.registerIntf( intf1, node2, intf2 )
        node2.registerIntf( intf2, node1, intf1 )
        return intf1, intf2

    def unlinkFrom( self, node2=None ):
        if node2:
            unlinkList = [node2]
        else:
            unlinkList = [c[0] for c in self.connection.values()]
        
        for node in unlinkList:
            self.deleteIntfsToNode(node)
            node.deleteIntfsToNode(self)
        
    def deleteIntfsToNode( self, dstNode, dstPort=None ):
        
        dstIntf = dstNode.intfName(dstPort) if dstPort else None
        intfs = []
        for intf in self.connection.keys():
            nextNode, nextIntf = self.connection[intf]
            if dstIntf:
                if nextIntf == dstIntf:
                    intfs.append(intf)
            elif nextNode == dstNode:
                intfs.append(intf)

        #connections = self.connectionsTo(dstNode)
        #if dstPort:
        #    dstIntf = dstNode.intfName(dstPort)
        #    intfs = [connection[0] for connection in connections if connection[1] == dstIntf]
        #else:
        #    intfs = [connection[0] for connection in connections]
        
        for intf in intfs:
            self.deleteIntf(intf)
    
    def deleteIntf(self, intf):
        del self.connection[intf]
        port = self.intfToPort(intf)
        if port is not None:
            del self.intfs[port]
        del self.ports[intf]
        
        quietRun( 'ip link del ' + intf )
        sleep( 0.001 )

    def deletePort(self, port):
        self.deleteIntf(self.intfName(port))
        
    def deleteIntfs( self ):
        "Delete all of our interfaces."
        # In theory the interfaces should go away after we shut down.
        # However, this takes time, so we're better off removing them
        # explicitly so that we won't get errors if we run before they
        # have been removed by the kernel. Unfortunately this is very slow,
        # at least with Linux kernels before 2.6.33
        for intf in self.intfs.values():
            quietRun( 'ip link del ' + intf )
            info( '.' )
            # Does it help to sleep to let things run?
            sleep( 0.001 )

    def setMAC( self, intf, mac ):
        """Set the MAC address for an interface.
           mac: MAC address as string"""
        result = self.cmd( 'ifconfig', intf, 'down' )
        result += self.cmd( 'ifconfig', intf, 'hw', 'ether', mac )
        result += self.cmd( 'ifconfig', intf, 'up' )
        return result

    def setARP( self, ip, mac ):
        """Add an ARP entry.
           ip: IP address as string
           mac: MAC address as string"""
        result = self.cmd( 'arp', '-s', ip, mac )
        return result

    def setIP( self, intf, ip, prefixLen=8 ):
        """Set the IP address for an interface.
           intf: interface name
           ip: IP address as a string
           prefixLen: prefix length, e.g. 8 for /8 or 16M addrs"""
        ipSub = '%s/%d' % ( ip, prefixLen )
        result = self.cmd( 'ifconfig', intf, ipSub, 'up' )
        self.ips[ intf ] = ip
        return result

    def setHostRoute( self, ip, intf ):
        """Add route to host.
           ip: IP address as dotted decimal
           intf: string, interface name"""
        return self.cmd( 'route add -host ' + ip + ' dev ' + intf )

    def setDefaultRoute( self, intf ):
        """Set the default route to go through intf.
           intf: string, interface name"""
        self.cmd( 'ip route flush root 0/0' )
        return self.cmd( 'route add default ' + intf )

    def defaultIntf( self ):
        "Return interface for lowest port"
        ports = self.intfs.keys()
        if ports:
            return self.intfs[ min( ports ) ]

    _ipMatchRegex = re.compile( r'\d+\.\d+\.\d+\.\d+' )
    _macMatchRegex = re.compile( r'..:..:..:..:..:..' )
    _ipSubnetMatchRegex = re.compile( r'(\d+\.\d+\.\d+\.\d+)/(\d+)' )

    def IP( self, intf=None ):
        "Return IP address of a node or specific interface."
        if intf is None:
            intf = self.defaultIntf()
        if intf and not self.waiting:
            self.updateIP( intf )
        return self.ips.get( intf, None )

    def MAC( self, intf=None ):
        "Return MAC address of a node or specific interface."
        if intf is None:
            intf = self.defaultIntf()
        if intf and not self.waiting:
            self.updateMAC( intf )
        return self.macs.get( intf, None )

    def updateIP( self, intf ):
        "Update IP address for an interface"
        assert not self.waiting
        ifconfig = self.cmd( 'ifconfig ' + intf )
        ips = self._ipMatchRegex.findall( ifconfig )
        if ips:
            self.ips[ intf ] = ips[ 0 ]
        else:
            self.ips[ intf ] = None

    def updateMAC( self, intf ):
        "Update MAC address for an interface"
        assert not self.waiting
        ifconfig = self.cmd( 'ifconfig ' + intf )
        macs = self._macMatchRegex.findall( ifconfig )
        if macs:
            self.macs[ intf ] = macs[ 0 ]
        else:
            self.macs[ intf ] = None

    def intfIsUp( self, intf ):
        "Check if an interface is up."
        return 'UP' in self.cmd( 'ifconfig ' + intf )

    # Other methods
    def __str__( self ):
        intfs = sorted( self.intfs.values() )
        return '%s: IP=%s intfs=%s pid=%s' % (
            self.name, self.IP(), ','.join( intfs ), self.pid )

class Host( Node ):
    "A host is simply a Node."

    def addvlan( self, vlan):
        """Add a vlan to a host. Replace the current default interface"""
        assert not self.waiting
        intf = self.defaultIntf()
        x = self.cmd("ip addr show %s" % intf)
        res = self._ipSubnetMatchRegex.search(x) 
        if res == None:
            error("Could not read current IP/prefixLen from interface")
            return
        try:
            ip = res.group(1)
            prefixLen = res.group(2)
        except IndexError:
            error("Could not read current IP/prefixLen from interface")
            return
        self.cmd("ifconfig %s inet 0.0.0.0" % intf)
        self.cmd("vconfig add %s %d" % (intf, vlan))
        self.cmd("ifconfig %s.%d inet %s/%s" % (intf, vlan, ip, prefixLen) )
        # Make the new vlan interface the default intf
        port = self.ports[intf]
        intf = "%s.%d" % (intf, vlan)
        self.intfs[ port ] = intf
        self.ports[ intf ] = port


class Switch( Node ):
    """A Switch is a Node that is running (or has execed?)
       an OpenFlow switch."""

    portBase = SWITCH_PORT_BASE  # 0 for OF < 1.0, 1 for OF >= 1.0

    def __init__( self, name, prefix = 's', opts='', listenPort=None, dpid=None, **kwargs):
        Node.__init__( self, name, prefix=prefix, **kwargs )
        self.opts = opts
        self.listenPort = listenPort
        if self.listenPort:
            self.opts += ' --listen=ptcp:%i ' % self.listenPort
        if dpid:
            self.dpid = dpid
        elif self.defaultMAC:
            self.dpid = "00:00:" + self.defaultMAC
        else:
            self.dpid = None

    def defaultIntf( self ):
        "Return interface for HIGHEST port"
        ports = self.intfs.keys()
        if ports:
            intf = self.intfs[ max( ports ) ]
        return intf

    def startIntfs( self ):
        "Default function to start interfaces"
        self.cmd("ifconfig lo up")
        for intf in self.intfs.values():
            self.cmd("ifconfig " + intf + " up" )

    def sendCmd( self, *cmd, **kwargs ):
        """Send command to Node.
           cmd: string"""
        kwargs.setdefault( 'printPid', False )
        if not self.execed:
            return Node.sendCmd( self, *cmd, **kwargs )
        else:
            error( '*** Error: %s has execed and cannot accept commands' %
                     self.name )

class LinuxBridge( Switch ):
    "Linux bridge"

    def __init__ ( self, name, **kwargs ):
        """Init.
           name: name for switch"""
        Switch.__init__( self, name, **kwargs )
        self.dp = "lxbr-%s" % name
        self.intf = self.dp
        
    @staticmethod
    def setup():
        pathCheck('brctl')
        brlist = quietRun('brctl show')
        for line in brlist.split("\n"):
            line = line.rstrip()
            m = re.match('^lxbr-(\w+)', line)
            if (m):
                print "Cleaning old bridge lxbr-%s" % m.group(1)
                self.cmd ('brctl', 'delbr', 'lxbr-%s' % m.group(1))

    def start(self, controllers):
        self.startIntfs()
        self.cmd('brctl', 'addbr', self.dp)
        self.cmd('brctl', 'stp', self.dp, 'on')
        for port, intf in self.intfs.items():
            self.doadd(intf)
        self.cmd('ifconfig', self.dp, 'up')
        self.cmd('brctl', 'setfd', self.dp, '2')

    def stop( self ):
        "Terminate kernel datapath."
        self.deleteIntfs()
        self.cmd('ifconfig', self.dp, 'down')
        self.cmd('brctl', 'delbr', self.dp)

    def doadd ( self, intf ):
        self.cmd('brctl', 'addif', self.dp, intf)

    def addIntf( self, intf, port ):
        super(LinuxBridge, self).addIntf(intf, port)
        self.doadd(intf)
    
    def deleteIntf( self, intf ):
        super(LinuxBridge, self).deleteIntf(intf)
        self.cmd('brctl', 'delif', self.dp, intf)

class UserSwitch( Switch ):
    "User-space switch."

    saved_contr = None

    def __init__( self, name, **kwargs ):
        """Init.
           name: name for the switch"""
        Switch.__init__( self, name, **kwargs )
        pathCheck( 'ofdatapath', 'ofprotocol',
            moduleName='the OpenFlow reference user switch (openflow.org)' )
        self.killjob('%ofdatapath')
        self.killjob('%ofprotocol')

    @staticmethod
    def setup():
        "Ensure any dependencies are loaded; if not, try to load them."
        if not os.path.exists( '/dev/net/tun' ):
            moduleDeps( add=TUN )

    def start( self, controllers):
        """Start OpenFlow reference user datapath.
           Log to /tmp/sN-{ofd,ofp}.log.
           controllers: list of controller objects"""
        self.saved_contr = controllers

        ofdlog = '/tmp/' + self.name + '-ofd.log'
        ofplog = '/tmp/' + self.name + '-ofp.log'
        self.startIntfs()
        mac_str = ''
        if self.defaultMAC:
            # ofdatapath expects a string of hex digits with no colons.
            mac_str = ' -d ' + ''.join( self.defaultMAC.split( ':' ) )
        intfs = sorted( self.intfs.values() )
        if self.inNamespace:
            intfs = intfs[ :-1 ]
        self.cmd( 'ofdatapath -i ' + ','.join( intfs ) +
            ' punix:/tmp/' + self.name + mac_str + ' --no-slicing ' +
            ' 1> ' + ofdlog + ' 2> ' + ofdlog + ' &' )
        self.cmd( 'ofprotocol unix:/tmp/' + self.name + ' ' +
            ','.join( [ 'tcp:%s:%d' % ( c.IP(), c.port ) \
                        for c in controllers ] ) +
            ' --fail=closed ' + self.opts +
            ' 1> ' + ofplog + ' 2>' + ofplog + ' &' )

    def killjob( self, job ):
        self.cmd('kill %s' % job)
        self.cmd('if jobs -l %s; then sleep 1; kill -9 %s; fi' % (job, job))

    def stopprocs( self ):
        self.killjob('%ofdatapath')
        self.killjob('%ofprotocol')

    def restart( self ):
        if (self.saved_contr):
            self.stopprocs()
            self.start(self.saved_contr)
        
    def stop( self ):
        "Stop OpenFlow reference user datapath."
        self.stopprocs()
        self.deleteIntfs()

    def addIntf( self, intf, port ):
        super(UserSwitch, self).addIntf(intf, port)
        self.restart()
    
    def deleteIntf( self, intf ):
        super(UserSwitch, self).deleteIntf(intf)
        self.restart()

class KernelSwitch( Switch ):
    """Kernel-space switch.
       Currently only works in root namespace."""

    def __init__( self, name, dp=None, **kwargs ):
        """Init.
           name: name for switch
           dp: netlink id (0, 1, 2, ...)
           defaultMAC: default MAC as string; random value if None"""
        Switch.__init__( self, name, **kwargs )
        self.dp = 'nl:%i' % dp
        self.intf = 'of%i' % dp
        if self.inNamespace:
            error( "KernelSwitch currently only works"
                " in the root namespace." )
            exit( 1 )

    @staticmethod
    def setup():
        "Ensure any dependencies are loaded; if not, try to load them."
        moduleName=('the OpenFlow reference kernel switch'        
                ' (openflow.org) (NOTE: not available in OpenFlow 1.0+!)' )
        pathCheck( 'ofprotocol', moduleName=moduleName)
        moduleDeps( subtract=OVS_KMOD, add=OF_KMOD, moduleName=moduleName )

    def start( self, controllers ):
        "Start up reference kernel datapath."
        ofplog = '/tmp/' + self.name + '-ofp.log'
        self.startIntfs()
        # Delete local datapath if it exists;
        # then create a new one monitoring the given interfaces
        quietRun( 'dpctl deldp ' + self.dp )
        self.cmd( 'dpctl adddp ' + self.dp )
        if self.defaultMAC:
            self.cmd( 'ifconfig', self.intf, 'hw', 'ether', self.defaultMAC )
        ports = sorted( self.ports.values() )
        if len( ports ) != ports[ -1 ] + 1 - self.portBase:
            raise Exception( 'only contiguous, zero-indexed port ranges'
                            'supported: %s' % ports )
        intfs = [ self.intfs[ port ] for port in ports ]
        self.cmd( 'dpctl', 'addif', self.dp, ' '.join( intfs ) )
        # Run protocol daemon
        self.cmd( 'ofprotocol ' + self.dp +
            ' '.join( [ ' tcp:%s:%d' % ( c.IP(), c.port ) \
                        for c in controllers ] ) +
            ' --fail=closed ' + self.opts +
            ' 1> ' + ofplog + ' 2>' + ofplog + ' &' )
        self.execed = False

    def stop( self ):
        "Terminate kernel datapath."
        quietRun( 'dpctl deldp ' + self.dp )
        self.cmd( 'kill %ofprotocol' )
        self.deleteIntfs()


class OVSKernelSwitchNew( Switch ):
    """Open VSwitch kernel-space switch.
       Currently only works in the root namespace."""

    numSwitch = 0
    ovsdbServerPid = None
    ovsVswitchdPid = None

    def __init__( self, name, dp=None, **kwargs ):
        """Init.
           name: name for switch
           dp: netlink id (0, 1, 2, ...)
           defaultMAC: default MAC as unsigned int; random value if None"""
        Switch.__init__( self, name, **kwargs )
        self.dp = 'mn-dp%i' % dp
        self.intf = self.dp
        OVSKernelSwitchNew.numSwitch += 1
        if self.inNamespace:
            error( "OVSKernelSwitch currently only works"
                " in the root namespace.\n" )
            exit( 1 )

    @staticmethod
    def setup():
        "Ensure any dependencies are loaded; if not, try to load them."
        moduleName='Open vSwitch (openvswitch.org)'
        pathCheck( 'ovs-vsctl', 'ovsdb-server', 'ovs-vswitchd', moduleName=moduleName )

        output = quietRun('ovs-vsctl --version')
        if "1.7" in output:
            # The kernel module name is changed some time in 2012
            # http://openvswitch.org/cgi-bin/gitweb.cgi?p=openvswitch;
            # a=commitdiff;h=9b80f761bed9a32c1b0eb22ee3361966057ea973
            moduleDeps( subtract=OF_KMOD, add="openvswitch", moduleName=moduleName )
        else:
            moduleDeps( subtract=OF_KMOD, add=OVS_KMOD, moduleName=moduleName )

        if not checkRunning('ovsdb-server', 'ovs-vswitchd'):
            # db, socket, pid, log file paths
            ovsInstanceDir = "/var/run/openvswitch"
            quietRun('mkdir -p %s' % ovsInstanceDir)
            confDbPath = "%s/conf.db" % ovsInstanceDir
            dbSockPath = "%s/db.sock" % ovsInstanceDir
            ovsdbServerPidPath = "%s/ovsdb-server.pid" % ovsInstanceDir
            ovsdbServerLogPath = "%s/ovsdb-server.log" % ovsInstanceDir
            ovsVswitchdPidPath = "%s/ovs-vswitchd.pid" % ovsInstanceDir
            ovsVswitchdLogPath = "%s/ovs-vswitchd.log" % ovsInstanceDir

            # Create ovs database
            quietRun("ovsdb-tool create %s" % confDbPath)

            # Start ovsdb-server
            ovsdb_instance = Popen(['ovsdb-server',
                                    confDbPath,
                                    '--remote=punix:%s' % dbSockPath,
                                    '--remote=db:Open_vSwitch,manager_options',
                                    '--detach',
                                    '--pidfile=%s' % ovsdbServerPidPath,
                                    '--log-file=%s' % ovsdbServerLogPath],
                                   stderr = sys.stderr,
                                   stdout = sys.stdout)
            assert ovsdb_instance.wait() == 0

            # Start ovs-vswitchd
            vswitchd_instance = Popen(['ovs-vswitchd',
                                       'unix:%s' % dbSockPath,
                                       '--detach',
                                       '--pidfile=%s' % ovsVswitchdPidPath,
                                       '--log-file=%s' % ovsVswitchdLogPath],
                                      stderr = sys.stderr,
                                      stdout = sys.stdout)
            assert vswitchd_instance.wait() == 0

            # Append ovs-vsctl with database information
            OVSKernelSwitchNew.vsctl_cmd = \
                'ovs-vsctl -t 2 --db=unix:%s ' % dbSockPath

            # Save the pids of ovsdb-server and ovs-vswitchd
            OVSKernelSwitchNew.ovsdbServerPid = \
                int(file(ovsdbServerPidPath).read().strip())
            OVSKernelSwitchNew.ovsVswitchdPid = \
                int(file(ovsVswitchdPidPath).read().strip())
        else:
            OVSKernelSwitchNew.vsctl_cmd = 'ovs-vsctl -t 2 '

        # Remove old mininet datapaths to make sure they don't interfere
        brlist = quietRun ( OVSKernelSwitchNew.vsctl_cmd + ' list-br' )
        for line in brlist.split("\n"):
            line = line.rstrip()
            if re.match('^mn-dp[0-9]+$', line):
                quietRun ( OVSKernelSwitchNew.vsctl_cmd + ' del-br ' + line )


    def start( self, controllers, failopen=False ):
        "Start up kernel datapath."
        self.startIntfs()
        # Delete local datapath if it exists;
        # then create a new one monitoring the given interfaces
        quietRun( self.vsctl_cmd + ' -- --if-exists del-br ' + self.dp )
        self.cmd( self.vsctl_cmd + ' add-br ' + self.dp )
        failmode = 'secure'
        if (failopen):
            failmode = 'standalone'
        self.cmd('%s set-fail-mode %s %s '  % (self.vsctl_cmd, self.dp, failmode))
        mac_str = ''
        if self.defaultMAC:
            # ovs-openflowd expects a string of exactly 16 hex digits with no
            # colons.
            dpid_str = '0000' + \
                      ''.join( self.defaultMAC.split( ':' ) ) + ' '
            self.cmd(self.vsctl_cmd + ' --no-wait set bridge %s other-config:datapath_type=system other-config:datapath-id=%s' % (self.dp, dpid_str))
        ports = sorted( self.ports.values() )
        if len( ports ) != ports[ -1 ] + 1 - self.portBase:
            raise Exception( 'only contiguous, one-indexed port ranges '
                            'supported: %s' % self.intfs )
        intfs = [ self.intfs[ port ] for port in ports ]
        for i in intfs:
            self.cmd( self.vsctl_cmd, 'add-port', self.dp, i ) 
        if (controllers != None and len(controllers)):
            self.cmd( self.vsctl_cmd + ' set-controller ' + self.dp +
                      ' '.join( [ ' tcp:%s:%d' % ( c.IP(), c.port ) \
                      for c in controllers ] ))
        self.execed = False

    def stop( self ):
        "Terminate kernel datapath."
        quietRun( self.vsctl_cmd + ' -- --if-exists del-br ' + self.dp )
        self.deleteIntfs()
        OVSKernelSwitchNew.numSwitch -= 1

        # Stop ovsdb-server and ovs-vswitchd if applicable
        if OVSKernelSwitchNew.numSwitch == 0:
            if OVSKernelSwitchNew.ovsdbServerPid:
                quietRun("kill %d" % OVSKernelSwitchNew.ovsdbServerPid)
            if OVSKernelSwitchNew.ovsVswitchdPid:
                quietRun("kill %d" % OVSKernelSwitchNew.ovsVswitchdPid)

    def addIntf( self, intf, port ):
        super(OVSKernelSwitchNew, self).addIntf(intf, port)
        self.cmd( self.vsctl_cmd + ' -- --may-exist', 'add-port', self.dp, intf )
    
    def deleteIntf( self, intf ):
        super(OVSKernelSwitchNew, self).deleteIntf(intf)
        self.cmd( self.vsctl_cmd, ' -- --if-exists', 'del-port', self.dp, intf )


class OVSKernelSwitch( Switch ):
    """Open VSwitch kernel-space switch for OVS < 1.2.0
       Currently only works in the root namespace.
       Uses the old non ovsdb / vswitchd based user-space tools.
       This class can eventually be removed!
    """

    def __init__( self, name, dp=None, **kwargs ):
        """Init.
           name: name for switch
           dp: netlink id (0, 1, 2, ...)
           defaultMAC: default MAC as unsigned int; random value if None"""
        Switch.__init__( self, name, **kwargs )
        self.dp = 'dp%i' % dp
        self.intf = self.dp
        if self.inNamespace:
            error( "OVSKernelSwitch currently only works"
                " in the root namespace.\n" )
            exit( 1 )

    @staticmethod
    def setup():
        "Ensure any dependencies are loaded; if not, try to load them."
        moduleName='Open vSwitch (openvswitch.org) < 1.2.0'
        pathCheck( 'ovs-dpctl', 'ovs-openflowd', moduleName=moduleName )
        moduleDeps( subtract=OF_KMOD, add=OVS_KMOD, moduleName=moduleName )

    def start( self, controllers ):
        "Start up kernel datapath."
        ofplog = '/tmp/' + self.name + '-ofp.log'
        self.startIntfs()
        # Delete local datapath if it exists;
        # then create a new one monitoring the given interfaces
        quietRun( 'ovs-dpctl del-dp ' + self.dp )
        self.cmd( 'ovs-dpctl add-dp ' + self.dp )
        mac_str = ''
        if self.defaultMAC:
            # ovs-openflowd expects a string of exactly 16 hex digits with no
            # colons.
            mac_str = ' --datapath-id=0000' + \
                      ''.join( self.defaultMAC.split( ':' ) ) + ' '
        ports = sorted( self.ports.values() )
        if len( ports ) != ports[ -1 ] + 1 - self.portBase:
            raise Exception( 'only contiguous, one-indexed port ranges '
                            'supported: %s' % self.intfs )
        intfs = [ self.intfs[ port ] for port in ports ]
        self.cmd( 'ovs-dpctl', 'add-if', self.dp, ' '.join( intfs ) )
        # Run protocol daemon
        self.cmd( 'ovs-openflowd ' + self.dp +
            ' '.join( [ ' tcp:%s:%d' % ( c.IP(), c.port ) \
                        for c in controllers ] ) +
            ' --fail=secure ' + self.opts + mac_str +
            ' 1>' + ofplog + ' 2>' + ofplog + '&' )
        self.execed = False

    def stop( self ):
        "Terminate kernel datapath."
        quietRun( 'ovs-dpctl del-dp ' + self.dp )
        self.cmd( 'kill %ovs-openflowd' )
        self.deleteIntfs()

    def addIntf( self, intf, port ):
        super(OVSKernelSwitch, self).addIntf(intf, port)
        self.cmd( 'ovs-dpctl', 'add-if', self.dp, intf )
    
    def deleteIntf( self, intf ):
        super(OVSKernelSwitch, self).deleteIntf(intf)
        self.cmd( 'ovs-dpctl', 'del-if', self.dp, intf )
        
class OVSUserSwitch( Switch ):
    """Open VSwitch user-space switch.
    """

    def __init__( self, name, dp=None, **kwargs ):
        """Init.
           name: name for switch
           dp: netlink id (0, 1, 2, ...)
           defaultMAC: default MAC as unsigned int; random value if None"""
        Switch.__init__( self, name, **kwargs )
        self.dp = 'netdev@dp%i' % dp
        self.intf = self.dp
        if self.inNamespace:
            error( "OVSUserSwitch currently only works"
                " in the root namespace.\n" )
            exit( 1 )

    @staticmethod
    def setup():
        "Ensure any dependencies are loaded; if not, try to load them."
        pathCheck( 'ovs-dpctl', 'ovs-openflowd',
            moduleName='Open vSwitch (openvswitch.org)')
        if not os.path.exists( '/dev/net/tun' ):
            moduleDeps( add=TUN )

    def start( self, controllers ):
        "Start up kernel datapath."
        ofplog = '/tmp/' + self.name + '-ofp.log'
        self.startIntfs()
        mac_str = ''
        if self.defaultMAC:
            # ovs-openflowd expects a string of exactly 16 hex digits with no
            # colons.
            mac_str = ' --datapath-id=0000' + \
                      ''.join( self.defaultMAC.split( ':' ) ) + ' '
        ports = sorted( self.ports.values() )
        if len( ports ) != ports[ -1 ] + 1 - self.portBase:
            raise Exception( 'only contiguous, one-indexed port ranges '
                            'supported: %s' % self.intfs )
        intfs = [ self.intfs[ port ] for port in ports ]
        # self.cmd( 'ovs-dpctl', 'add-if', self.dp, ' '.join( intfs ) )
        # Run protocol daemon
        self.cmd( 'ovs-openflowd -v ' + self.dp +
            ' --ports=' + ','.join(intfs) +
            ' '.join( [ ' tcp:%s:%d' % ( c.IP(), c.port ) \
                        for c in controllers ] ) +
            ' --fail=secure ' + self.opts + mac_str +
            ' 1>' + ofplog + ' 2>' + ofplog + '&' )
        self.execed = False

    def stop( self ):
        "Terminate kernel datapath."
        # quietRun( 'ovs-dpctl del-dp ' + self.dp )
        self.cmd( 'kill %ovs-openflowd' )
        self.deleteIntfs()


class RemoteSwitch( Switch ):
    "Switch created outside mininet."

    def __init__( self, name, remotePorts, **kwargs ):
        Switch.__init__( self, name, inNamespace=False, **kwargs )
        self.remotePorts = remotePorts

    @staticmethod
    def setup():
        pass

    def start( self, controllers ):
        self.startIntfs()
        for port, intf in self.intfs.items():
            self.cmd( 'brctl', 'addif', self.remotePorts[ port ], intf )

    def stop(self):
        self.deleteIntfs()


class Controller( Node ):
    """A Controller is a Node that is running (or has execed?) an
       OpenFlow controller."""

    def __init__( self, name, inNamespace=False, command='controller',
                 cargs='-v ptcp:%d', cdir=None, defaultIP="127.0.0.1",
                 port=6633 ):
        self.command = command
        self.cargs = cargs
        self.cdir = cdir
        self.port = port
        Node.__init__( self, name, inNamespace=inNamespace,
            defaultIP=defaultIP )

    def start( self ):
        """Start <controller> <args> on controller.
           Log to /tmp/cN.log"""
        pathCheck( self.command )
        cout = '/tmp/' + self.name + '.log'
        if self.cdir is not None:
            self.cmd( 'cd ' + self.cdir )
        self.cmd( self.command + ' ' + self.cargs % self.port +
            ' 1>' + cout + ' 2>' + cout + '&' )
        self.execed = False

    def stop( self ):
        "Stop controller."
        self.cmd( 'kill %' + self.command )
        self.terminate()

    def IP( self, intf=None ):
        "Return IP address of the Controller"
        ip = Node.IP( self, intf=intf )
        if ip is None:
            ip = self.defaultIP
        return ip

class ControllerParams( object ):
    "Container for controller IP parameters."

    def __init__( self, ip, prefixLen ):
        """Init.
           ip: string, controller IP address
           prefixLen: prefix length, e.g. 8 for /8, covering 16M"""
        self.ip = ip
        self.prefixLen = prefixLen


class NOX( Controller ):
    "Controller to run a NOX application."

    def __init__( self, name, noxArgs=None, **kwargs ):
        """Init.
           name: name to give controller
           noxArgs: list of args, or single arg, to pass to NOX"""
        if not noxArgs:
            noxArgs = [ 'packetdump' ]
        elif type( noxArgs ) != list:
            noxArgs = [ noxArgs ]

        if 'NOX_CORE_DIR' not in os.environ:
            exit( 'exiting; please set missing NOX_CORE_DIR env var' )
        noxCoreDir = os.environ[ 'NOX_CORE_DIR' ]

        Controller.__init__( self, name,
            command=noxCoreDir + '/nox_core',
            cargs='--libdir=/usr/local/lib -v -i ptcp:%s ' +
                    ' '.join( noxArgs ),
            cdir=noxCoreDir, **kwargs )


class RemoteController( Controller ):
    "Controller running outside of Mininet's control."

    def __init__( self, name, defaultIP='127.0.0.1',
                 port=6633, **kwargs):
        """Init.
           name: name to give controller
           defaultIP: the IP address where the remote controller is
           listening
           port: the port where the remote controller is listening"""
        Controller.__init__( self, name, defaultIP=defaultIP, port=port,
            **kwargs )

    def start( self ):
        "Overridden to do nothing."
        return

    def stop( self ):
        "Overridden to do nothing."
        return
