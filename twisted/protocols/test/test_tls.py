# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.protocols.tls}.
"""

from __future__ import division, absolute_import

from zope.interface.verify import verifyObject
from zope.interface import Interface, directlyProvides, implementer

from twisted.python.compat import intToBytes, iterbytes
try:
    from twisted.protocols.tls import TLSMemoryBIOProtocol, TLSMemoryBIOFactory
    from twisted.protocols.tls import _PullToPush, _ProducerMembrane
except ImportError:
    # Skip the whole test module if it can't be imported.
    skip = "pyOpenSSL 0.10 or newer required for twisted.protocol.tls"
else:
    # Otherwise, the pyOpenSSL dependency must be satisfied, so all these
    # imports will work.
    from OpenSSL.crypto import X509Type
    from OpenSSL.SSL import (TLSv1_METHOD, TLSv1_1_METHOD, TLSv1_2_METHOD, Error, Context, ConnectionType,
                             WantReadError)
    from twisted.internet.ssl import PrivateCertificate, optionsForClientTLS
    from twisted.test.ssl_helpers import (ClientTLSContext, ServerTLSContext,
                                          certPath)
    from twisted.test.test_sslverify import certificatesForAuthorityAndServer

from twisted.test.iosim import connectedServerAndClient

from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.python import log

from twisted.internet.interfaces import (
    ISystemHandle, ISSLTransport,
    IPushProducer, IProtocolNegotiationFactory, IHandshakeListener,
    IOpenSSLServerConnectionCreator, IOpenSSLClientConnectionCreator
)

from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.internet.defer import Deferred, gatherResults
from twisted.internet.protocol import Protocol, ClientFactory, ServerFactory
from twisted.internet.task import TaskStopped
from twisted.protocols.loopback import loopbackAsync, collapsingPumpPolicy
from twisted.trial.unittest import TestCase, SynchronousTestCase
from twisted.test.test_tcp import ConnectionLostNotifyingProtocol
from twisted.test.proto_helpers import StringTransport


class HandshakeCallbackContextFactory:
    """
    L{HandshakeCallbackContextFactory} is a factory for SSL contexts which
    allows applications to get notification when the SSL handshake completes.

    @ivar _finished: A L{Deferred} which will be called back when the handshake
        is done.
    """
    # pyOpenSSL needs to expose this.
    # https://bugs.launchpad.net/pyopenssl/+bug/372832
    SSL_CB_HANDSHAKE_DONE = 0x20

    def __init__(self, method=TLSv1_METHOD):
        self._finished = Deferred()
        self._method = method


    def factoryAndDeferred(cls):
        """
        Create a new L{HandshakeCallbackContextFactory} and return a two-tuple
        of it and a L{Deferred} which will fire when a connection created with
        it completes a TLS handshake.
        """
        contextFactory = cls()
        return contextFactory, contextFactory._finished
    factoryAndDeferred = classmethod(factoryAndDeferred)


    def _info(self, connection, where, ret):
        """
        This is the "info callback" on the context.  It will be called
        periodically by pyOpenSSL with information about the state of a
        connection.  When it indicates the handshake is complete, it will fire
        C{self._finished}.
        """
        if where & self.SSL_CB_HANDSHAKE_DONE:
            self._finished.callback(None)


    def getContext(self):
        """
        Create and return an SSL context configured to use L{self._info} as the
        info callback.
        """
        context = Context(self._method)
        context.set_info_callback(self._info)
        return context



class AccumulatingProtocol(Protocol):
    """
    A protocol which collects the bytes it receives and closes its connection
    after receiving a certain minimum of data.

    @ivar howMany: The number of bytes of data to wait for before closing the
        connection.

    @ivar receiving: A C{list} of C{str} of the bytes received so far.
    """
    def __init__(self, howMany):
        self.howMany = howMany


    def connectionMade(self):
        self.received = []


    def dataReceived(self, bytes):
        self.received.append(bytes)
        if sum(map(len, self.received)) >= self.howMany:
            self.transport.loseConnection()


    def connectionLost(self, reason):
        if not reason.check(ConnectionDone):
            log.err(reason)



def buildTLSProtocol(server=False, transport=None, fakeConnection=None):
    """
    Create a protocol hooked up to a TLS transport hooked up to a
    StringTransport.
    """
    # We want to accumulate bytes without disconnecting, so set high limit:
    clientProtocol = AccumulatingProtocol(999999999999)
    clientFactory = ClientFactory()
    clientFactory.protocol = lambda: clientProtocol

    if fakeConnection:
        @implementer(IOpenSSLServerConnectionCreator,
                     IOpenSSLClientConnectionCreator)
        class HardCodedConnection(object):
            def clientConnectionForTLS(self, tlsProtocol):
                return fakeConnection
            serverConnectionForTLS = clientConnectionForTLS
        contextFactory = HardCodedConnection()
    else:
        if server:
            contextFactory = ServerTLSContext()
        else:
            contextFactory = ClientTLSContext()
    wrapperFactory = TLSMemoryBIOFactory(
        contextFactory, not server, clientFactory)
    sslProtocol = wrapperFactory.buildProtocol(None)

    if transport is None:
        transport = StringTransport()
    sslProtocol.makeConnection(transport)
    return clientProtocol, sslProtocol



class TLSMemoryBIOFactoryTests(TestCase):
    """
    Ensure TLSMemoryBIOFactory logging acts correctly.
    """

    def test_quiet(self):
        """
        L{TLSMemoryBIOFactory.doStart} and L{TLSMemoryBIOFactory.doStop} do
        not log any messages.
        """
        contextFactory = ServerTLSContext()

        logs = []
        logger = logs.append
        log.addObserver(logger)
        self.addCleanup(log.removeObserver, logger)
        wrappedFactory = ServerFactory()
        # Disable logging on the wrapped factory:
        wrappedFactory.doStart = lambda: None
        wrappedFactory.doStop = lambda: None
        factory = TLSMemoryBIOFactory(contextFactory, False, wrappedFactory)
        factory.doStart()
        factory.doStop()
        self.assertEqual(logs, [])


    def test_logPrefix(self):
        """
        L{TLSMemoryBIOFactory.logPrefix} amends the wrapped factory's log prefix
        with a short string (C{"TLS"}) indicating the wrapping, rather than its
        full class name.
        """
        contextFactory = ServerTLSContext()
        factory = TLSMemoryBIOFactory(contextFactory, False, ServerFactory())
        self.assertEqual("ServerFactory (TLS)", factory.logPrefix())


    def test_logPrefixFallback(self):
        """
        If the wrapped factory does not provide L{ILoggingContext},
        L{TLSMemoryBIOFactory.logPrefix} uses the wrapped factory's class name.
        """
        class NoFactory(object):
            pass

        contextFactory = ServerTLSContext()
        factory = TLSMemoryBIOFactory(contextFactory, False, NoFactory())
        self.assertEqual("NoFactory (TLS)", factory.logPrefix())



def handshakingClientAndServer(clientGreetingData=None,
                               clientAbortAfterHandshake=False):
    """
    Construct a client and server L{TLSMemoryBIOProtocol} connected by an IO
    pump.

    @param greetingData: The data which should be written in L{connectionMade}.
    @type greetingData: L{bytes}

    @return: 3-tuple of client, server, L{twisted.test.iosim.IOPump}
    """
    authCert, serverCert = certificatesForAuthorityAndServer()
    @implementer(IHandshakeListener)
    class Client(AccumulatingProtocol, object):
        handshook = False
        peerAfterHandshake = None

        def connectionMade(self):
            super(Client, self).connectionMade()
            if clientGreetingData is not None:
                self.transport.write(clientGreetingData)

        def handshakeCompleted(self):
            self.handshook = True
            self.peerAfterHandshake = self.transport.getPeerCertificate()
            if clientAbortAfterHandshake:
                self.transport.abortConnection()

        def connectionLost(self, reason):
            pass

    @implementer(IHandshakeListener)
    class Server(AccumulatingProtocol, object):
        handshaked = False
        def handshakeCompleted(self):
            self.handshaked = True

        def connectionLost(self, reason):
            pass

    clientF = TLSMemoryBIOFactory(
        optionsForClientTLS(u"example.com", trustRoot=authCert),
        isClient=True,
        wrappedFactory=ClientFactory.forProtocol(lambda: Client(999999))
    )
    serverF = TLSMemoryBIOFactory(
        serverCert.options(), isClient=False,
        wrappedFactory=ServerFactory.forProtocol(lambda: Server(999999))
    )
    client, server, pump = connectedServerAndClient(
        lambda: serverF.buildProtocol(None),
        lambda: clientF.buildProtocol(None),
        greet=False,
    )
    return client, server, pump



class DeterministicTLSMemoryBIOTests(SynchronousTestCase):
    """
    Test for the implementation of L{ISSLTransport} which runs over another
    transport.

    @note: Prefer to add test cases to this suite, in this style, using
        L{connectedServerAndClient}, rather than returning L{Deferred}s.
    """

    def test_handshakeNotification(self):
        """
        The completion of the TLS handshake calls C{handshakeCompleted} on
        L{Protocol} objects that provide L{IHandshakeListener}.  At the time
        C{handshakeCompleted} is invoked, the transport's peer certificate will
        have been initialized.
        """
        client, server, pump = handshakingClientAndServer()
        self.assertEqual(client.wrappedProtocol.handshook, False)
        self.assertEqual(server.wrappedProtocol.handshaked, False)
        pump.flush()
        self.assertEqual(client.wrappedProtocol.handshook, True)
        self.assertEqual(server.wrappedProtocol.handshaked, True)
        self.assertIsNot(client.wrappedProtocol.peerAfterHandshake, None)


    def test_handshakeStopWriting(self):
        """
        If some data is written to the transport in C{connectionMade}, but
        C{handshakeDone} doesn't like something it sees about the handshake, it
        can use C{abortConnection} to ensure that the application never
        receives that data.
        """
        client, server, pump = handshakingClientAndServer(b"untrustworthy",
                                                          True)
        pump.flush()
        self.assertEqual(server.wrappedProtocol.received, [])



class TLSMemoryBIOTests(TestCase):
    """
    Tests for the implementation of L{ISSLTransport} which runs over another
    L{ITransport}.
    """

    def test_interfaces(self):
        """
        L{TLSMemoryBIOProtocol} instances provide L{ISSLTransport} and
        L{ISystemHandle}.
        """
        proto = TLSMemoryBIOProtocol(None, None)
        self.assertTrue(ISSLTransport.providedBy(proto))
        self.assertTrue(ISystemHandle.providedBy(proto))


    def test_wrappedProtocolInterfaces(self):
        """
        L{TLSMemoryBIOProtocol} instances provide the interfaces provided by
        the transport they wrap.
        """
        class ITransport(Interface):
            pass

        class MyTransport(object):
            def write(self, bytes):
                pass

        clientFactory = ClientFactory()
        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            contextFactory, True, clientFactory)

        transport = MyTransport()
        directlyProvides(transport, ITransport)
        tlsProtocol = TLSMemoryBIOProtocol(wrapperFactory, Protocol())
        tlsProtocol.makeConnection(transport)
        self.assertTrue(ITransport.providedBy(tlsProtocol))


    def test_getHandle(self):
        """
        L{TLSMemoryBIOProtocol.getHandle} returns the L{OpenSSL.SSL.Connection}
        instance it uses to actually implement TLS.

        This may seem odd.  In fact, it is.  The L{OpenSSL.SSL.Connection} is
        not actually the "system handle" here, nor even an object the reactor
        knows about directly.  However, L{twisted.internet.ssl.Certificate}'s
        C{peerFromTransport} and C{hostFromTransport} methods depend on being
        able to get an L{OpenSSL.SSL.Connection} object in order to work
        properly.  Implementing L{ISystemHandle.getHandle} like this is the
        easiest way for those APIs to be made to work.  If they are changed,
        then it may make sense to get rid of this implementation of
        L{ISystemHandle} and return the underlying socket instead.
        """
        factory = ClientFactory()
        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(contextFactory, True, factory)
        proto = TLSMemoryBIOProtocol(wrapperFactory, Protocol())
        transport = StringTransport()
        proto.makeConnection(transport)
        self.assertIsInstance(proto.getHandle(), ConnectionType)


    def test_makeConnection(self):
        """
        When L{TLSMemoryBIOProtocol} is connected to a transport, it connects
        the protocol it wraps to a transport.
        """
        clientProtocol = Protocol()
        clientFactory = ClientFactory()
        clientFactory.protocol = lambda: clientProtocol

        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            contextFactory, True, clientFactory)
        sslProtocol = wrapperFactory.buildProtocol(None)

        transport = StringTransport()
        sslProtocol.makeConnection(transport)

        self.assertIsNotNone(clientProtocol.transport)
        self.assertIsNot(clientProtocol.transport, transport)
        self.assertIs(clientProtocol.transport, sslProtocol)


    def handshakeProtocols(self):
        """
        Start handshake between TLS client and server.
        """
        clientFactory = ClientFactory()
        clientFactory.protocol = Protocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverFactory = ServerFactory()
        serverFactory.protocol = Protocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol)
        return (sslClientProtocol, sslServerProtocol, handshakeDeferred,
                connectionDeferred)


    def test_handshake(self):
        """
        The TLS handshake is performed when L{TLSMemoryBIOProtocol} is
        connected to a transport.
        """
        tlsClient, tlsServer, handshakeDeferred, _ = self.handshakeProtocols()

        # Only wait for the handshake to complete.  Anything after that isn't
        # important here.
        return handshakeDeferred


    def test_handshakeFailure(self):
        """
        L{TLSMemoryBIOProtocol} reports errors in the handshake process to the
        application-level protocol object using its C{connectionLost} method
        and disconnects the underlying transport.
        """
        clientConnectionLost = Deferred()
        clientFactory = ClientFactory()
        clientFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                clientConnectionLost))

        clientContextFactory = HandshakeCallbackContextFactory()
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverConnectionLost = Deferred()
        serverFactory = ServerFactory()
        serverFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                serverConnectionLost))

        # This context factory rejects any clients which do not present a
        # certificate.
        certificateData = FilePath(certPath).getContent()
        certificate = PrivateCertificate.loadPEM(certificateData)
        serverContextFactory = certificate.options(certificate)
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol)

        def cbConnectionLost(protocol):
            # The connection should close on its own in response to the error
            # induced by the client not supplying the required certificate.
            # After that, check to make sure the protocol's connectionLost was
            # called with the right thing.
            protocol.lostConnectionReason.trap(Error)
        clientConnectionLost.addCallback(cbConnectionLost)
        serverConnectionLost.addCallback(cbConnectionLost)

        # Additionally, the underlying transport should have been told to
        # go away.
        return gatherResults([
                clientConnectionLost, serverConnectionLost,
                connectionDeferred])


    def test_getPeerCertificate(self):
        """
        L{TLSMemoryBIOProtocol.getPeerCertificate} returns the
        L{OpenSSL.crypto.X509Type} instance representing the peer's
        certificate.
        """
        # Set up a client and server so there's a certificate to grab.
        clientFactory = ClientFactory()
        clientFactory.protocol = Protocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverFactory = ServerFactory()
        serverFactory.protocol = Protocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        loopbackAsync(sslServerProtocol, sslClientProtocol)

        # Wait for the handshake
        def cbHandshook(ignored):
            # Grab the server's certificate and check it out
            cert = sslClientProtocol.getPeerCertificate()
            self.assertIsInstance(cert, X509Type)
            self.assertEqual(
                cert.digest('sha1'),
                # openssl x509 -noout -sha1 -fingerprint -in server.pem
                b'45:DD:FD:E2:BD:BF:8B:D0:00:B7:D2:7A:BB:20:F5:34:05:4B:15:80')
        handshakeDeferred.addCallback(cbHandshook)
        return handshakeDeferred


    def test_writeAfterHandshake(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} before the handshake is
        complete are received by the protocol on the other side of the
        connection once the handshake succeeds.
        """
        bytes = b"some bytes"

        clientProtocol = Protocol()
        clientFactory = ClientFactory()
        clientFactory.protocol = lambda: clientProtocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol(len(bytes))
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol)

        # Wait for the handshake to finish before writing anything.
        def cbHandshook(ignored):
            clientProtocol.transport.write(bytes)

            # The server will drop the connection once it gets the bytes.
            return connectionDeferred
        handshakeDeferred.addCallback(cbHandshook)

        # Once the connection is lost, make sure the server received the
        # expected bytes.
        def cbDisconnected(ignored):
            self.assertEqual(b"".join(serverProtocol.received), bytes)
        handshakeDeferred.addCallback(cbDisconnected)

        return handshakeDeferred


    def writeBeforeHandshakeTest(self, sendingProtocol, bytes):
        """
        Run test where client sends data before handshake, given the sending
        protocol and expected bytes.
        """
        clientFactory = ClientFactory()
        clientFactory.protocol = sendingProtocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol(len(bytes))
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol)

        # Wait for the connection to end, then make sure the server received
        # the bytes sent by the client.
        def cbConnectionDone(ignored):
            self.assertEqual(b"".join(serverProtocol.received), bytes)
        connectionDeferred.addCallback(cbConnectionDone)
        return connectionDeferred


    def test_writeBeforeHandshake(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} before the handshake is
        complete are received by the protocol on the other side of the
        connection once the handshake succeeds.
        """
        bytes = b"some bytes"

        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(bytes)

        return self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeSequence(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} with C{writeSequence} are
        received by the protocol on the other side of the connection.
        """
        bytes = b"some bytes"
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.writeSequence(list(iterbytes(bytes)))

        return self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeAfterLoseConnection(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} after C{loseConnection} is
        called are not transmitted (unless there is a registered producer,
        which will be tested elsewhere).
        """
        bytes = b"some bytes"
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(bytes)
                self.transport.loseConnection()
                self.transport.write(b"hello")
                self.transport.writeSequence([b"world"])
        return self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeUnicodeRaisesTypeError(self):
        """
        Writing C{unicode} to L{TLSMemoryBIOProtocol} throws a C{TypeError}.
        """
        notBytes = u"hello"
        result = []
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                try:
                    self.transport.write(notBytes)
                except TypeError:
                    result.append(True)
                self.transport.write(b"bytes")
                self.transport.loseConnection()
        d = self.writeBeforeHandshakeTest(SimpleSendingProtocol, b"bytes")
        return d.addCallback(lambda ign: self.assertEqual(result, [True]))


    def test_multipleWrites(self):
        """
        If multiple separate TLS messages are received in a single chunk from
        the underlying transport, all of the application bytes from each
        message are delivered to the application-level protocol.
        """
        bytes = [b'a', b'b', b'c', b'd', b'e', b'f', b'g', b'h', b'i']
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                for b in bytes:
                    self.transport.write(b)

        clientFactory = ClientFactory()
        clientFactory.protocol = SimpleSendingProtocol

        clientContextFactory = HandshakeCallbackContextFactory()
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol(sum(map(len, bytes)))
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol, collapsingPumpPolicy)

        # Wait for the connection to end, then make sure the server received
        # the bytes sent by the client.
        def cbConnectionDone(ignored):
            self.assertEqual(b"".join(serverProtocol.received), b''.join(bytes))
        connectionDeferred.addCallback(cbConnectionDone)
        return connectionDeferred


    def hugeWrite(self, method=TLSv1_METHOD):
        """
        If a very long string is passed to L{TLSMemoryBIOProtocol.write}, any
        trailing part of it which cannot be send immediately is buffered and
        sent later.
        """
        bytes = b"some bytes"
        factor = 2 ** 20
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(bytes * factor)

        clientFactory = ClientFactory()
        clientFactory.protocol = SimpleSendingProtocol

        clientContextFactory = HandshakeCallbackContextFactory(method=method)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol(len(bytes) * factor)
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext(method=method)
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectionDeferred = loopbackAsync(sslServerProtocol, sslClientProtocol)

        # Wait for the connection to end, then make sure the server received
        # the bytes sent by the client.
        def cbConnectionDone(ignored):
            self.assertEqual(b"".join(serverProtocol.received), bytes * factor)
        connectionDeferred.addCallback(cbConnectionDone)
        return connectionDeferred

    def test_hugeWrite_TLSv1(self):
        return self.hugeWrite()

    def test_hugeWrite_TLSv1_1(self):
        return self.hugeWrite(method=TLSv1_1_METHOD)

    def test_hugeWrite_TLSv1_(self):
        return self.hugeWrite(method=TLSv1_2_METHOD)

    def test_disorderlyShutdown(self):
        """
        If a L{TLSMemoryBIOProtocol} loses its connection unexpectedly, this is
        reported to the application.
        """
        clientConnectionLost = Deferred()
        clientFactory = ClientFactory()
        clientFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                clientConnectionLost))

        clientContextFactory = HandshakeCallbackContextFactory()
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        # Client speaks first, so the server can be dumb.
        serverProtocol = Protocol()

        loopbackAsync(serverProtocol, sslClientProtocol)

        # Now destroy the connection.
        serverProtocol.transport.loseConnection()

        # And when the connection completely dies, check the reason.
        def cbDisconnected(clientProtocol):
            clientProtocol.lostConnectionReason.trap(Error)
        clientConnectionLost.addCallback(cbDisconnected)
        return clientConnectionLost


    def test_loseConnectionAfterHandshake(self):
        """
        L{TLSMemoryBIOProtocol.loseConnection} sends a TLS close alert and
        shuts down the underlying connection cleanly on both sides, after
        transmitting all buffered data.
        """
        class NotifyingProtocol(ConnectionLostNotifyingProtocol):
            def __init__(self, onConnectionLost):
                ConnectionLostNotifyingProtocol.__init__(self,
                                                         onConnectionLost)
                self.data = []

            def dataReceived(self, bytes):
                self.data.append(bytes)

        clientConnectionLost = Deferred()
        clientFactory = ClientFactory()
        clientProtocol = NotifyingProtocol(clientConnectionLost)
        clientFactory.protocol = lambda: clientProtocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverConnectionLost = Deferred()
        serverProtocol = NotifyingProtocol(serverConnectionLost)
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        loopbackAsync(sslServerProtocol, sslClientProtocol)
        chunkOfBytes = b"123456890" * 100000

        # Wait for the handshake before dropping the connection.
        def cbHandshake(ignored):
            # Write more than a single bio_read, to ensure client will still
            # have some data it needs to write when it receives the TLS close
            # alert, and that simply doing a single bio_read won't be
            # sufficient. Thus we will verify that any amount of buffered data
            # will be written out before the connection is closed, rather than
            # just small amounts that can be returned in a single bio_read:
            clientProtocol.transport.write(chunkOfBytes)
            serverProtocol.transport.write(b'x')
            serverProtocol.transport.loseConnection()

            # Now wait for the client and server to notice.
            return gatherResults([clientConnectionLost, serverConnectionLost])
        handshakeDeferred.addCallback(cbHandshake)

        # Wait for the connection to end, then make sure the client and server
        # weren't notified of a handshake failure that would cause the test to
        # fail.
        def cbConnectionDone(result):
            (clientProtocol, serverProtocol) = result
            clientProtocol.lostConnectionReason.trap(ConnectionDone)
            serverProtocol.lostConnectionReason.trap(ConnectionDone)

            # The server should have received all bytes sent by the client:
            self.assertEqual(b"".join(serverProtocol.data), chunkOfBytes)

            # The server should have closed its underlying transport, in
            # addition to whatever it did to shut down the TLS layer.
            self.assertTrue(serverProtocol.transport.q.disconnect)

            # The client should also have closed its underlying transport once
            # it saw the server shut down the TLS layer, so as to avoid relying
            # on the server to close the underlying connection.
            self.assertTrue(clientProtocol.transport.q.disconnect)
        handshakeDeferred.addCallback(cbConnectionDone)
        return handshakeDeferred


    def test_connectionLostOnlyAfterUnderlyingCloses(self):
        """
        The user protocol's connectionLost is only called when transport
        underlying TLS is disconnected.
        """
        class LostProtocol(Protocol):
            disconnected = None
            def connectionLost(self, reason):
                self.disconnected = reason
        wrapperFactory = TLSMemoryBIOFactory(ClientTLSContext(),
                                             True, ClientFactory())
        protocol = LostProtocol()
        tlsProtocol = TLSMemoryBIOProtocol(wrapperFactory, protocol)
        transport = StringTransport()
        tlsProtocol.makeConnection(transport)

        # Pretend TLS shutdown finished cleanly; the underlying transport
        # should be told to close, but the user protocol should not yet be
        # notified:
        tlsProtocol._tlsShutdownFinished(None)
        self.assertTrue(transport.disconnecting)
        self.assertIsNone(protocol.disconnected)

        # Now close the underlying connection; the user protocol should be
        # notified with the given reason (since TLS closed cleanly):
        tlsProtocol.connectionLost(Failure(ConnectionLost("ono")))
        self.assertTrue(protocol.disconnected.check(ConnectionLost))
        self.assertEqual(protocol.disconnected.value.args, ("ono",))


    def test_loseConnectionTwice(self):
        """
        If TLSMemoryBIOProtocol.loseConnection is called multiple times, all
        but the first call have no effect.
        """
        tlsClient, tlsServer, handshakeDeferred, disconnectDeferred = (
            self.handshakeProtocols())
        self.successResultOf(handshakeDeferred)
        # Make sure loseConnection calls _shutdownTLS the first time (mostly
        # to make sure we've overriding it correctly):
        calls = []
        def _shutdownTLS(shutdown=tlsClient._shutdownTLS):
            calls.append(1)
            return shutdown()
        tlsClient._shutdownTLS = _shutdownTLS
        tlsClient.write(b'x')
        tlsClient.loseConnection()
        self.assertTrue(tlsClient.disconnecting)
        self.assertEqual(calls, [1])

        # Make sure _shutdownTLS isn't called a second time:
        tlsClient.loseConnection()
        self.assertEqual(calls, [1])

        # We do successfully disconnect at some point:
        return disconnectDeferred


    def test_unexpectedEOF(self):
        """
        Unexpected disconnects get converted to ConnectionLost errors.
        """
        tlsClient, tlsServer, handshakeDeferred, disconnectDeferred = (
            self.handshakeProtocols())
        serverProtocol = tlsServer.wrappedProtocol
        data = []
        reason = []
        serverProtocol.dataReceived = data.append
        serverProtocol.connectionLost = reason.append

        # Write data, then disconnect *underlying* transport, resulting in an
        # unexpected TLS disconnect:
        def handshakeDone(ign):
            tlsClient.write(b"hello")
            tlsClient.transport.loseConnection()
        handshakeDeferred.addCallback(handshakeDone)

        # Receiver should be disconnected, with ConnectionLost notification
        # (masking the Unexpected EOF SSL error):
        def disconnected(ign):
            self.assertTrue(reason[0].check(ConnectionLost), reason[0])
        disconnectDeferred.addCallback(disconnected)
        return disconnectDeferred


    def test_errorWriting(self):
        """
        Errors while writing cause the protocols to be disconnected.
        """
        tlsClient, tlsServer, handshakeDeferred, disconnectDeferred = (
            self.handshakeProtocols())
        reason = []
        tlsClient.wrappedProtocol.connectionLost = reason.append

        # Pretend TLS connection is unhappy sending:
        class Wrapper(object):
            def __init__(self, wrapped):
                self._wrapped = wrapped
            def __getattr__(self, attr):
                return getattr(self._wrapped, attr)
            def send(self, *args):
                raise Error("ONO!")
        tlsClient._tlsConnection = Wrapper(tlsClient._tlsConnection)

        # Write some data:
        def handshakeDone(ign):
            tlsClient.write(b"hello")
        handshakeDeferred.addCallback(handshakeDone)

        # Failed writer should be disconnected with SSL error:
        def disconnected(ign):
            self.assertTrue(reason[0].check(Error), reason[0])
        disconnectDeferred.addCallback(disconnected)
        return disconnectDeferred



class TLSProducerTests(TestCase):
    """
    The TLS transport must support the IConsumer interface.
    """

    def drain(self, transport, allowEmpty=False):
        """
        Drain the bytes currently pending write from a L{StringTransport}, then
        clear it, since those bytes have been consumed.

        @param transport: The L{StringTransport} to get the bytes from.
        @type transport: L{StringTransport}

        @param allowEmpty: Allow the test to pass even if the transport has no
            outgoing bytes in it.
        @type allowEmpty: L{bool}

        @return: the outgoing bytes from the given transport
        @rtype: L{bytes}
        """
        value = transport.value()
        transport.clear()
        self.assertEqual(bool(allowEmpty or value), True)
        return value


    def setupStreamingProducer(self, transport=None, fakeConnection=None,
                               server=False):
        class HistoryStringTransport(StringTransport):
            def __init__(self):
                StringTransport.__init__(self)
                self.producerHistory = []

            def pauseProducing(self):
                self.producerHistory.append("pause")
                StringTransport.pauseProducing(self)

            def resumeProducing(self):
                self.producerHistory.append("resume")
                StringTransport.resumeProducing(self)

            def stopProducing(self):
                self.producerHistory.append("stop")
                StringTransport.stopProducing(self)

        applicationProtocol, tlsProtocol = buildTLSProtocol(
            transport=transport, fakeConnection=fakeConnection,
            server=server)
        producer = HistoryStringTransport()
        applicationProtocol.transport.registerProducer(producer, True)
        self.assertTrue(tlsProtocol.transport.streaming)
        return applicationProtocol, tlsProtocol, producer


    def flushTwoTLSProtocols(self, tlsProtocol, serverTLSProtocol):
        """
        Transfer bytes back and forth between two TLS protocols.
        """
        # We want to make sure all bytes are passed back and forth; JP
        # estimated that 3 rounds should be enough:
        for i in range(3):
            clientData = self.drain(tlsProtocol.transport, True)
            if clientData:
                serverTLSProtocol.dataReceived(clientData)
            serverData = self.drain(serverTLSProtocol.transport, True)
            if serverData:
                tlsProtocol.dataReceived(serverData)
            if not serverData and not clientData:
                break
        self.assertEqual(tlsProtocol.transport.value(), b"")
        self.assertEqual(serverTLSProtocol.transport.value(), b"")


    def test_producerDuringRenegotiation(self):
        """
        If we write some data to a TLS connection that is blocked waiting for a
        renegotiation with its peer, it will pause and resume its registered
        producer exactly once.
        """
        c, ct, cp = self.setupStreamingProducer()
        s, st, sp = self.setupStreamingProducer(server=True)

        self.flushTwoTLSProtocols(ct, st)
        # no public API for this yet because it's (mostly) unnecessary, but we
        # have to be prepared for a peer to do it to us
        tlsc = ct._tlsConnection
        tlsc.renegotiate()
        self.assertRaises(WantReadError, tlsc.do_handshake)
        ct._flushSendBIO()
        st.dataReceived(self.drain(ct.transport))
        payload = b'payload'
        s.transport.write(payload)
        s.transport.loseConnection()
        # give the client the server the client's response...
        ct.dataReceived(self.drain(st.transport))
        messageThatUnblocksTheServer = self.drain(ct.transport)
        # split it into just enough chunks that it would provoke the producer
        # with an incorrect implementation...
        for fragment in (messageThatUnblocksTheServer[0:1],
                         messageThatUnblocksTheServer[1:2],
                         messageThatUnblocksTheServer[2:]):
            st.dataReceived(fragment)
        self.assertEqual(st.transport.disconnecting, False)
        s.transport.unregisterProducer()
        self.flushTwoTLSProtocols(ct, st)
        self.assertEqual(st.transport.disconnecting, True)
        self.assertEqual(b''.join(c.received), payload)
        self.assertEqual(sp.producerHistory, ['pause', 'resume'])


    def test_streamingProducerPausedInNormalMode(self):
        """
        When the TLS transport is not blocked on reads, it correctly calls
        pauseProducing on the registered producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()

        # The TLS protocol's transport pretends to be full, pausing its
        # producer:
        tlsProtocol.transport.producer.pauseProducing()
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])
        self.assertTrue(tlsProtocol._producer._producerPaused)


    def test_streamingProducerResumedInNormalMode(self):
        """
        When the TLS transport is not blocked on reads, it correctly calls
        resumeProducing on the registered producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()
        tlsProtocol.transport.producer.pauseProducing()
        self.assertEqual(producer.producerHistory, ['pause'])

        # The TLS protocol's transport pretends to have written everything
        # out, so it resumes its producer:
        tlsProtocol.transport.producer.resumeProducing()
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        self.assertFalse(tlsProtocol._producer._producerPaused)


    def test_streamingProducerPausedInWriteBlockedOnReadMode(self):
        """
        When the TLS transport is blocked on reads, it correctly calls
        pauseProducing on the registered producer.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()

        # Write to TLS transport. Because we do this before the initial TLS
        # handshake is finished, writing bytes triggers a WantReadError,
        # indicating that until bytes are read for the handshake, more bytes
        # cannot be written. Thus writing bytes before the handshake should
        # cause the producer to be paused:
        clientProtocol.transport.write(b"hello")
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])
        self.assertTrue(tlsProtocol._producer._producerPaused)


    def test_streamingProducerResumedInWriteBlockedOnReadMode(self):
        """
        When the TLS transport is blocked on reads, it correctly calls
        resumeProducing on the registered producer.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()

        # Write to TLS transport, triggering WantReadError; this should cause
        # the producer to be paused. We use a large chunk of data to make sure
        # large writes don't trigger multiple pauses:
        clientProtocol.transport.write(b"hello world" * 320000)
        self.assertEqual(producer.producerHistory, ['pause'])

        # Now deliver bytes that will fix the WantRead condition; this should
        # unpause the producer:
        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        self.assertFalse(tlsProtocol._producer._producerPaused)

        # Make sure we haven't disconnected for some reason:
        self.assertFalse(tlsProtocol.transport.disconnecting)
        self.assertEqual(producer.producerState, 'producing')


    def test_streamingProducerTwice(self):
        """
        Registering a streaming producer twice throws an exception.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        originalProducer = tlsProtocol._producer
        producer2 = object()
        self.assertRaises(RuntimeError,
            clientProtocol.transport.registerProducer, producer2, True)
        self.assertIs(tlsProtocol._producer, originalProducer)


    def test_streamingProducerUnregister(self):
        """
        Unregistering a streaming producer removes it, reverting to initial state.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        clientProtocol.transport.unregisterProducer()
        self.assertIsNone(tlsProtocol._producer)
        self.assertIsNone(tlsProtocol.transport.producer)


    def loseConnectionWithProducer(self, writeBlockedOnRead):
        """
        Common code for tests involving writes by producer after
        loseConnection is called.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)

        if not writeBlockedOnRead:
            # Do the initial handshake before write:
            self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        else:
            # In this case the write below will trigger write-blocked-on-read
            # condition...
            pass

        # Now write, then lose connection:
        clientProtocol.transport.write(b"x ")
        clientProtocol.transport.loseConnection()
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)

        # Underlying transport should not have loseConnection called yet, nor
        # should producer be stopped:
        self.assertFalse(tlsProtocol.transport.disconnecting)
        self.assertFalse("stop" in producer.producerHistory)

        # Writes from client to server should continue to go through, since we
        # haven't unregistered producer yet:
        clientProtocol.transport.write(b"hello")
        clientProtocol.transport.writeSequence([b" ", b"world"])

        # Unregister producer; this should trigger TLS shutdown:
        clientProtocol.transport.unregisterProducer()
        self.assertNotEqual(tlsProtocol.transport.value(), b"")
        self.assertFalse(tlsProtocol.transport.disconnecting)

        # Additional writes should not go through:
        clientProtocol.transport.write(b"won't")
        clientProtocol.transport.writeSequence([b"won't!"])

        # Finish TLS close handshake:
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        self.assertTrue(tlsProtocol.transport.disconnecting)

        # Bytes made it through, as long as they were written before producer
        # was unregistered:
        self.assertEqual(b"".join(serverProtocol.received), b"x hello world")


    def test_streamingProducerLoseConnectionWithProducer(self):
        """
        loseConnection() waits for the producer to unregister itself, then
        does a clean TLS close alert, then closes the underlying connection.
        """
        return self.loseConnectionWithProducer(False)


    def test_streamingProducerLoseConnectionWithProducerWBOR(self):
        """
        Even when writes are blocked on reading, loseConnection() waits for
        the producer to unregister itself, then does a clean TLS close alert,
        then closes the underlying connection.
        """
        return self.loseConnectionWithProducer(True)


    def test_streamingProducerBothTransportsDecideToPause(self):
        """
        pauseProducing() events can come from both the TLS transport layer and
        the underlying transport. In this case, both decide to pause,
        underlying first.
        """
        class PausingStringTransport(StringTransport):
            _didPause = False

            def write(self, data):
                if not self._didPause and self.producer is not None:
                    self._didPause = True
                    self.producer.pauseProducing()
                StringTransport.write(self, data)


        class TLSConnection(object):
            def __init__(self):
                self.l = []

            def send(self, bytes):
                # on first write, don't send all bytes:
                if not self.l:
                    bytes = bytes[:-1]
                # pause on second write:
                if len(self.l) == 1:
                    self.l.append("paused")
                    raise WantReadError()
                # otherwise just take in data:
                self.l.append(bytes)
                return len(bytes)

            def set_connect_state(self):
                pass

            def do_handshake(self):
                pass

            def bio_write(self, data):
                pass

            def bio_read(self, size):
                return b'X'

            def recv(self, size):
                raise WantReadError()

        transport = PausingStringTransport()
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer(
            transport, fakeConnection=TLSConnection())
        self.assertEqual(producer.producerState, 'producing')

        # Shove in fake TLSConnection that will raise WantReadError the second
        # time send() is called. This will allow us to have bytes written to
        # to the PausingStringTransport, so it will pause the producer. Then,
        # WantReadError will be thrown, triggering the TLS transport's
        # producer code path.
        clientProtocol.transport.write(b"hello")
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])

        # Now, underlying transport resumes, and then we deliver some data to
        # TLS transport so that it will resume:
        tlsProtocol.transport.producer.resumeProducing()
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        tlsProtocol.dataReceived(b"hello")
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])


    def test_streamingProducerStopProducing(self):
        """
        If the underlying transport tells its producer to stopProducing(),
        this is passed on to the high-level producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()
        tlsProtocol.transport.producer.stopProducing()
        self.assertEqual(producer.producerState, 'stopped')


    def test_nonStreamingProducer(self):
        """
        Non-streaming producers get wrapped as streaming producers.
        """
        clientProtocol, tlsProtocol = buildTLSProtocol()
        producer = NonStreamingProducer(clientProtocol.transport)

        # Register non-streaming producer:
        clientProtocol.transport.registerProducer(producer, False)
        streamingProducer = tlsProtocol.transport.producer._producer

        # Verify it was wrapped into streaming producer:
        self.assertIsInstance(streamingProducer, _PullToPush)
        self.assertEqual(streamingProducer._producer, producer)
        self.assertEqual(streamingProducer._consumer, clientProtocol.transport)
        self.assertTrue(tlsProtocol.transport.streaming)

        # Verify the streaming producer was started, and ran until the end:
        def done(ignore):
            # Our own producer is done:
            self.assertIsNone(producer.consumer)
            # The producer has been unregistered:
            self.assertIsNone(tlsProtocol.transport.producer)
            # The streaming producer wrapper knows it's done:
            self.assertTrue(streamingProducer._finished)
        producer.result.addCallback(done)

        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        return producer.result


    def test_interface(self):
        """
        L{_ProducerMembrane} implements L{IPushProducer}.
        """
        producer = StringTransport()
        membrane = _ProducerMembrane(producer)
        self.assertTrue(verifyObject(IPushProducer, membrane))


    def registerProducerAfterConnectionLost(self, streaming):
        """
        If a producer is registered after the transport has disconnected, the
        producer is not used, and its stopProducing method is called.
        """
        clientProtocol, tlsProtocol = buildTLSProtocol()
        clientProtocol.connectionLost = lambda reason: reason.trap(Error)

        class Producer(object):
            stopped = False

            def resumeProducing(self):
                return 1/0 # this should never be called

            def stopProducing(self):
                self.stopped = True

        # Disconnect the transport:
        tlsProtocol.connectionLost(Failure(ConnectionDone()))

        # Register the producer; startProducing should not be called, but
        # stopProducing will:
        producer = Producer()
        tlsProtocol.registerProducer(producer, False)
        self.assertIsNone(tlsProtocol.transport.producer)
        self.assertTrue(producer.stopped)


    def test_streamingProducerAfterConnectionLost(self):
        """
        If a streaming producer is registered after the transport has
        disconnected, the producer is not used, and its stopProducing method
        is called.
        """
        self.registerProducerAfterConnectionLost(True)


    def test_nonStreamingProducerAfterConnectionLost(self):
        """
        If a non-streaming producer is registered after the transport has
        disconnected, the producer is not used, and its stopProducing method
        is called.
        """
        self.registerProducerAfterConnectionLost(False)



class NonStreamingProducer(object):
    """
    A pull producer which writes 10 times only.
    """

    counter = 0
    stopped = False

    def __init__(self, consumer):
        self.consumer = consumer
        self.result = Deferred()

    def resumeProducing(self):
        if self.counter < 10:
            self.consumer.write(intToBytes(self.counter))
            self.counter += 1
            if self.counter == 10:
                self.consumer.unregisterProducer()
                self._done()
        else:
            if self.consumer is None:
                raise RuntimeError("BUG: resume after unregister/stop.")


    def pauseProducing(self):
        raise RuntimeError("BUG: pause should never be called.")


    def _done(self):
        self.consumer = None
        d = self.result
        del self.result
        d.callback(None)


    def stopProducing(self):
        self.stopped = True
        self._done()



class NonStreamingProducerTests(TestCase):
    """
    Non-streaming producers can be adapted into being streaming producers.
    """

    def streamUntilEnd(self, consumer):
        """
        Verify the consumer writes out all its data, but is not called after
        that.
        """
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # The producer will call unregisterProducer(), and we need to hook
        # that up so the streaming wrapper is notified; the
        # TLSMemoryBIOProtocol will have to do this itself, which is tested
        # elsewhere:
        def unregister(orig=consumer.unregisterProducer):
            orig()
            streamingProducer.stopStreaming()
        consumer.unregisterProducer = unregister

        done = nsProducer.result
        def doneStreaming(_):
            # All data was streamed, and the producer unregistered itself:
            self.assertEqual(consumer.value(), b"0123456789")
            self.assertIsNone(consumer.producer)
            # And the streaming wrapper stopped:
            self.assertTrue(streamingProducer._finished)
        done.addCallback(doneStreaming)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def test_writeUntilDone(self):
        """
        When converted to a streaming producer, the non-streaming producer
        writes out all its data, but is not called after that.
        """
        consumer = StringTransport()
        return self.streamUntilEnd(consumer)


    def test_pause(self):
        """
        When the streaming producer is paused, the underlying producer stops
        getting resumeProducing calls.
        """
        class PausingStringTransport(StringTransport):
            writes = 0

            def __init__(self):
                StringTransport.__init__(self)
                self.paused = Deferred()

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.pauseProducing()
                    d = self.paused
                    del self.paused
                    d.callback(None)


        consumer = PausingStringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # Make sure the consumer does not continue:
        def shouldNotBeCalled(ignore):
            self.fail("BUG: The producer should not finish!")
        nsProducer.result.addCallback(shouldNotBeCalled)

        done = consumer.paused
        def paused(ignore):
            # The CooperatorTask driving the producer was paused:
            self.assertEqual(streamingProducer._coopTask._pauseCount, 1)
        done.addCallback(paused)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def test_resume(self):
        """
        When the streaming producer is paused and then resumed, the underlying
        producer starts getting resumeProducing calls again after the resume.

        The test will never finish (or rather, time out) if the resume
        producing call is not working.
        """
        class PausingStringTransport(StringTransport):
            writes = 0

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.pauseProducing()
                    self.producer.resumeProducing()

        consumer = PausingStringTransport()
        return self.streamUntilEnd(consumer)


    def test_stopProducing(self):
        """
        When the streaming producer is stopped by the consumer, the underlying
        producer is stopped, and streaming is stopped.
        """
        class StoppingStringTransport(StringTransport):
            writes = 0

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.stopProducing()

        consumer = StoppingStringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        done = nsProducer.result
        def doneStreaming(_):
            # Not all data was streamed, and the producer was stopped:
            self.assertEqual(consumer.value(), b"012")
            self.assertTrue(nsProducer.stopped)
            # And the streaming wrapper stopped:
            self.assertTrue(streamingProducer._finished)
        done.addCallback(doneStreaming)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def resumeProducingRaises(self, consumer, expectedExceptions):
        """
        Common implementation for tests where the underlying producer throws
        an exception when its resumeProducing is called.
        """
        class ThrowingProducer(NonStreamingProducer):

            def resumeProducing(self):
                if self.counter == 2:
                    return 1/0
                else:
                    NonStreamingProducer.resumeProducing(self)

        nsProducer = ThrowingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # Register log observer:
        loggedMsgs = []
        log.addObserver(loggedMsgs.append)
        self.addCleanup(log.removeObserver, loggedMsgs.append)

        # Make consumer unregister do what TLSMemoryBIOProtocol would do:
        def unregister(orig=consumer.unregisterProducer):
            orig()
            streamingProducer.stopStreaming()
        consumer.unregisterProducer = unregister

        # Start streaming:
        streamingProducer.startStreaming()

        done = streamingProducer._coopTask.whenDone()
        done.addErrback(lambda reason: reason.trap(TaskStopped))
        def stopped(ign):
            self.assertEqual(consumer.value(), b"01")
            # Any errors from resumeProducing were logged:
            errors = self.flushLoggedErrors()
            self.assertEqual(len(errors), len(expectedExceptions))
            for f, (expected, msg), logMsg in zip(
                errors, expectedExceptions, loggedMsgs):
                self.assertTrue(f.check(expected))
                self.assertIn(msg, logMsg['why'])
            # And the streaming wrapper stopped:
            self.assertTrue(streamingProducer._finished)
        done.addCallback(stopped)
        return done


    def test_resumeProducingRaises(self):
        """
        If the underlying producer raises an exception when resumeProducing is
        called, the streaming wrapper should log the error, unregister from
        the consumer and stop streaming.
        """
        consumer = StringTransport()
        done = self.resumeProducingRaises(
            consumer,
            [(ZeroDivisionError, "failed, producing will be stopped")])
        def cleanShutdown(ignore):
            # Producer was unregistered from consumer:
            self.assertIsNone(consumer.producer)
        done.addCallback(cleanShutdown)
        return done


    def test_resumeProducingRaiseAndUnregisterProducerRaises(self):
        """
        If the underlying producer raises an exception when resumeProducing is
        called, the streaming wrapper should log the error, unregister from
        the consumer and stop streaming even if the unregisterProducer call
        also raise.
        """
        consumer = StringTransport()
        def raiser():
            raise RuntimeError()
        consumer.unregisterProducer = raiser
        return self.resumeProducingRaises(
            consumer,
            [(ZeroDivisionError, "failed, producing will be stopped"),
             (RuntimeError, "failed to unregister producer")])


    def test_stopStreamingTwice(self):
        """
        stopStreaming() can be called more than once without blowing
        up. This is useful for error-handling paths.
        """
        consumer = StringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        streamingProducer.startStreaming()
        streamingProducer.stopStreaming()
        streamingProducer.stopStreaming()
        self.assertTrue(streamingProducer._finished)


    def test_interface(self):
        """
        L{_PullToPush} implements L{IPushProducer}.
        """
        consumer = StringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        self.assertTrue(verifyObject(IPushProducer, streamingProducer))



@implementer(IProtocolNegotiationFactory)
class ClientNegotiationFactory(ClientFactory):
    """
    A L{ClientFactory} that has a set of acceptable protocols for NPN/ALPN
    negotiation.
    """
    def __init__(self, acceptableProtocols):
        """
        Create a L{ClientNegotiationFactory}.

        @param acceptableProtocols: The protocols the client will accept
            speaking after the TLS handshake is complete.
        @type acceptableProtocols: L{list} of L{bytes}
        """
        self._acceptableProtocols = acceptableProtocols


    def acceptableProtocols(self):
        """
        Returns a list of protocols that can be spoken by the connection
        factory in the form of ALPN tokens, as laid out in the IANA registry
        for ALPN tokens.

        @return: a list of ALPN tokens in order of preference.
        @rtype: L{list} of L{bytes}
        """
        return self._acceptableProtocols



@implementer(IProtocolNegotiationFactory)
class ServerNegotiationFactory(ServerFactory):
    """
    A L{ServerFactory} that has a set of acceptable protocols for NPN/ALPN
    negotiation.
    """
    def __init__(self, acceptableProtocols):
        """
        Create a L{ServerNegotiationFactory}.

        @param acceptableProtocols: The protocols the server will accept
            speaking after the TLS handshake is complete.
        @type acceptableProtocols: L{list} of L{bytes}
        """
        self._acceptableProtocols = acceptableProtocols


    def acceptableProtocols(self):
        """
        Returns a list of protocols that can be spoken by the connection
        factory in the form of ALPN tokens, as laid out in the IANA registry
        for ALPN tokens.

        @return: a list of ALPN tokens in order of preference.
        @rtype: L{list} of L{bytes}
        """
        return self._acceptableProtocols



class IProtocolNegotiationFactoryTests(TestCase):
    """
    Tests for L{IProtocolNegotiationFactory} inside L{TLSMemoryBIOFactory}.

    These tests expressly don't include the case where both server and client
    advertise protocols but don't have any overlap. This is because the
    behaviour here is platform-dependent and changes from version to version.
    Prior to version 1.1.0 of OpenSSL, failing the ALPN negotiation does not
    fail the handshake. At least in 1.0.2h, failing NPN *does* fail the
    handshake, at least with the callback implemented by PyOpenSSL.

    This is sufficiently painful to test that we simply don't. It's not
    necessary to validate that our offering logic works anyway: all we need to
    see is that it works in the successful case and that it degrades properly.
    """
    def handshakeProtocols(self, clientProtocols, serverProtocols):
        """
        Start handshake between TLS client and server.

        @param clientProtocols: The protocols the client will accept speaking
            after the TLS handshake is complete.
        @type clientProtocols: L{list} of L{bytes}

        @param serverProtocols: The protocols the server will accept speaking
            after the TLS handshake is complete.
        @type serverProtocols: L{list} of L{bytes}

        @return: A L{tuple} of four different items: the client L{Protocol},
            the server L{Protocol}, a L{Deferred} that fires when the client
            first receives bytes (and so the TLS connection is complete), and a
            L{Deferred} that fires when the server first receives bytes.
        @rtype: A L{tuple} of (L{Protocol}, L{Protocol}, L{Deferred},
            L{Deferred})
        """
        bytes = b'some bytes'

        class NotifyingSender(Protocol):
            def __init__(self, notifier):
                self.notifier = notifier

            def connectionMade(self):
                self.transport.writeSequence(list(iterbytes(bytes)))

            def dataReceived(self, bytes):
                if self.notifier is not None:
                    self.notifier.callback(self)
                    self.notifier = None


        clientDataReceived = Deferred()
        clientFactory = ClientNegotiationFactory(clientProtocols)
        clientFactory.protocol = lambda: NotifyingSender(
            clientDataReceived
        )

        clientContextFactory, _ = (
            HandshakeCallbackContextFactory.factoryAndDeferred())
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverDataReceived = Deferred()
        serverFactory = ServerNegotiationFactory(serverProtocols)
        serverFactory.protocol = lambda: NotifyingSender(
            serverDataReceived
        )

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        loopbackAsync(
            sslServerProtocol, sslClientProtocol
        )
        return (sslClientProtocol, sslServerProtocol, clientDataReceived,
                serverDataReceived)


    def test_negotiationWithNoProtocols(self):
        """
        When factories support L{IProtocolNegotiationFactory} but don't
        advertise support for any protocols, no protocols are negotiated.
        """
        client, server, clientDataReceived, serverDataReceived = (
            self.handshakeProtocols([], [])
        )

        def checkNegotiatedProtocol(ignored):
            self.assertEqual(client.negotiatedProtocol, None)
            self.assertEqual(server.negotiatedProtocol, None)

        clientDataReceived.addCallback(lambda ignored: serverDataReceived)
        serverDataReceived.addCallback(checkNegotiatedProtocol)

        return clientDataReceived


    def test_negotiationWithProtocolOverlap(self):
        """
        When factories support L{IProtocolNegotiationFactory} and support
        overlapping protocols, the first protocol is negotiated.
        """
        client, server, clientDataReceived, serverDataReceived = (
            self.handshakeProtocols([b'h2', b'http/1.1'], [b'h2', b'http/1.1'])
        )

        def checkNegotiatedProtocol(ignored):
            self.assertEqual(client.negotiatedProtocol, b'h2')
            self.assertEqual(server.negotiatedProtocol, b'h2')

        clientDataReceived.addCallback(lambda ignored: serverDataReceived)
        serverDataReceived.addCallback(checkNegotiatedProtocol)

        return clientDataReceived


    def test_negotiationClientOnly(self):
        """
        When factories support L{IProtocolNegotiationFactory} and only the
        client advertises, nothing is negotiated.
        """
        client, server, clientDataReceived, serverDataReceived = (
            self.handshakeProtocols([b'h2', b'http/1.1'], [])
        )

        def checkNegotiatedProtocol(ignored):
            self.assertEqual(client.negotiatedProtocol, None)
            self.assertEqual(server.negotiatedProtocol, None)

        clientDataReceived.addCallback(lambda ignored: serverDataReceived)
        serverDataReceived.addCallback(checkNegotiatedProtocol)

        return clientDataReceived


    def test_negotiationServerOnly(self):
        """
        When factories support L{IProtocolNegotiationFactory} and only the
        server advertises, nothing is negotiated.
        """
        client, server, clientDataReceived, serverDataReceived = (
            self.handshakeProtocols([], [b'h2', b'http/1.1'])
        )

        def checkNegotiatedProtocol(ignored):
            self.assertEqual(client.negotiatedProtocol, None)
            self.assertEqual(server.negotiatedProtocol, None)

        clientDataReceived.addCallback(lambda ignored: serverDataReceived)
        serverDataReceived.addCallback(checkNegotiatedProtocol)

        return clientDataReceived
