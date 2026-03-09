import unittest
import threading
from unittest import mock

from pyghmi.ipmi.private import session
from pyghmi.ipmi.private import util


class FakeEvent(object):
    def __init__(self):
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        return True


class FakeSocket(object):
    def __init__(self, port=9000):
        self.port = port

    def getsockname(self):
        return ('127.0.0.1', self.port)


class SessionRegressionTestCase(unittest.TestCase):

    def test_pyghmi_io_wait_respects_timeout_without_iothread(self):
        fake_event = FakeEvent()

        with mock.patch.object(session.threading, 'Event',
                               return_value=fake_event):
            with mock.patch.object(session, 'ioqueue',
                                   new=mock.Mock(append=mock.Mock())):
                with mock.patch.object(session, 'iosockets', new=[]):
                    with mock.patch.object(session, 'selectdeadline', 1000):
                        with mock.patch.object(session, '_monotonic_time',
                                               return_value=10):
                            session._io_wait(0.25)

        self.assertEqual(0.25, fake_event.wait_timeout)

    def test_pyghmi_io_wait_with_negative_timeout_clamps_to_zero(self):
        fake_event = FakeEvent()

        with mock.patch.object(session.threading, 'Event',
                               return_value=fake_event):
            with mock.patch.object(session, 'ioqueue',
                                   new=mock.Mock(append=mock.Mock())):
                with mock.patch.object(session, 'iosockets', new=[]):
                    with mock.patch.object(session, 'selectdeadline', 1000):
                        with mock.patch.object(session, '_monotonic_time',
                                               return_value=10):
                            session._io_wait(-1)

        self.assertEqual(0, fake_event.wait_timeout)

    def test_pyghmi_ioworker_survives_io_graball_exception(self):
        worker = session.define_worker()()
        graball_calls = []

        def fake_graball(mysockets, directediowaiters):
            graball_calls.append((mysockets, directediowaiters))
            if len(graball_calls) == 1:
                raise RuntimeError('boom')
            worker.running = False
            return []

        with mock.patch.object(session, 'iosockets', new=[]):
            with mock.patch.object(session, 'iothreadwaiters', new=[]):
                with mock.patch.object(session.select, 'select',
                                       return_value=([], [], [])):
                    with mock.patch.object(session, '_io_graball',
                                           side_effect=fake_graball):
                        worker.run()

        self.assertEqual(2, len(graball_calls))

    def test_pyghmi_logout_does_not_double_decrement_socketpool(self):
        sock = FakeSocket()
        ipmisession = object.__new__(session.Session)
        ipmisession.cleaningup = False
        ipmisession.logged = 0
        ipmisession.sol_handler = None
        ipmisession.lastpayload = b'data'
        ipmisession.onlogpayload = b'data'
        ipmisession.logging = True
        ipmisession._customkeepalives = None
        ipmisession.broken = False
        ipmisession.socket = sock
        ipmisession.socketpool = {sock: 2}
        ipmisession.allsockaddrs = []
        ipmisession.nowait = False

        with mock.patch.object(session.Session, 'keepalive_sessions',
                               new={}):
            response = ipmisession.logout()

        self.assertEqual({'success': True}, response)
        self.assertEqual(1, ipmisession.socketpool[sock])

    def test_pyghmi_new_does_not_reuse_stale_initting_session(self):
        stale = mock.Mock()
        stale.logged = False
        stale.logging = False
        stale.broken = False
        stale.sessioncontext = None

        getaddrinfo_result = [(session.socket.AF_INET, session.socket.SOCK_DGRAM,
                               0, '', ('127.0.0.1', 623))]
        sesskey = ('bmc.example.com', 'user', 'pass', 623, None)
        initting_sessions = {sesskey: stale}

        with mock.patch.object(session.socket, 'getaddrinfo',
                               return_value=getaddrinfo_result):
            with mock.patch.object(session.Session, 'bmc_handlers', new={}):
                with mock.patch.object(session.Session, 'keepalive_sessions',
                                       new={}):
                    with mock.patch.object(
                            session.Session, 'initting_sessions',
                            new=initting_sessions):
                        created = session.Session.__new__(
                            session.Session, 'bmc.example.com', 'user', 'pass')

        self.assertIsNot(stale, created)
        self.assertIs(initting_sessions[sesskey], created)

    def test_pyghmi_is_session_valid_rejects_failed_context(self):
        failed = mock.Mock()
        failed.broken = False
        failed.sessioncontext = 'FAILED'

        with mock.patch.object(session.Session, 'keepalive_sessions', new={}):
            valid = session.Session._is_session_valid(failed)

        self.assertFalse(valid)

    def test_pyghmi_ioworker_survives_select_valueerror(self):
        worker = session.define_worker()()
        bad_socket = mock.Mock()
        bad_socket.fileno.return_value = -1
        select_calls = []

        def fake_select(sockets, _, __, timeout):
            select_calls.append(list(sockets))
            if len(select_calls) == 1:
                raise ValueError('file descriptor cannot be a negative integer')
            worker.running = False
            return ([], [], [])

        with mock.patch.object(session, 'iosockets', new=[bad_socket]):
            with mock.patch.object(session, 'iothreadwaiters', new=[]):
                with mock.patch.object(session.select, 'select',
                                       side_effect=fake_select):
                    with mock.patch.object(session, '_io_graball',
                                           return_value=[]):
                        worker.run()

        self.assertEqual(2, len(select_calls))
        self.assertEqual([], select_calls[1])

    def test_pyghmi_protect_lock_timeout_is_bounded(self):
        # IPMI v2.0 §24.1 defines finite shared-session resources on the MC.
        # A lock dead-end in host software must not become an unbounded wait.
        lock = threading.Lock()
        lock.acquire()
        try:
            start = session._monotonic_time()
            with self.assertRaisesRegex(RuntimeError, 'lock acquire timeout'):
                with util.protect(lock, timeout=0.05):
                    pass
            elapsed = session._monotonic_time() - start
            self.assertLess(elapsed, 1.0)
        finally:
            lock.release()

    def test_pyghmi_timedout_non_established_does_not_relog(self):
        ipmisession = object.__new__(session.Session)
        ipmisession.lastpayload = b'data'
        ipmisession.nowait = False
        ipmisession.timeout = 2
        ipmisession.maxtimeout = 1
        ipmisession.logontries = 1
        ipmisession.sessioncontext = 'OPENSESSION'
        ipmisession.ipmicallback = mock.Mock()
        ipmisession._mark_broken = mock.Mock()
        ipmisession._relog = mock.Mock()

        ipmisession._timedout()

        ipmisession._relog.assert_not_called()
        ipmisession._mark_broken.assert_called_once_with('timeout during login')

    def test_pyghmi_timedout_rakp_state_with_no_logontries_marks_broken(self):
        ipmisession = object.__new__(session.Session)
        ipmisession.lastpayload = b'data'
        ipmisession.nowait = False
        ipmisession.timeout = 0
        ipmisession.maxtimeout = 2
        ipmisession.logontries = 0
        ipmisession.sessioncontext = 'EXPECTINGRAKP2'
        ipmisession._mark_broken = mock.Mock()
        ipmisession._relog = mock.Mock()

        ipmisession._timedout()

        ipmisession._relog.assert_not_called()
        ipmisession._mark_broken.assert_called_once_with('timeout during login')

    def test_pyghmi_timedout_established_marks_broken_instead_of_relog(self):
        ipmisession = object.__new__(session.Session)
        ipmisession.lastpayload = b'data'
        ipmisession.last_payload_type = 1
        ipmisession.nowait = False
        ipmisession.timeout = 2
        ipmisession.maxtimeout = 1
        ipmisession.logontries = 1
        ipmisession.sessioncontext = 'ESTABLISHED'
        ipmisession._mark_broken = mock.Mock()
        ipmisession._relog = mock.Mock()

        ipmisession._timedout()

        ipmisession._relog.assert_not_called()
        ipmisession._mark_broken.assert_called_once_with(
            'timeout in established session')

    def test_pyghmi_logout_prunes_stale_bmc_handler_mapping(self):
        sock = FakeSocket()
        ipmisession = object.__new__(session.Session)
        ipmisession.cleaningup = False
        ipmisession.logged = 0
        ipmisession.sol_handler = None
        ipmisession.lastpayload = b'data'
        ipmisession.onlogpayload = b'data'
        ipmisession.logging = True
        ipmisession._customkeepalives = None
        ipmisession.broken = False
        ipmisession.socket = sock
        ipmisession.socketpool = {sock: 2}
        ipmisession.allsockaddrs = []
        ipmisession.nowait = False

        bmc_handlers = {('192.0.2.1', 623): {sock.getsockname()[1]: ipmisession}}

        with mock.patch.object(session.Session, 'keepalive_sessions', new={}):
            with mock.patch.object(session.Session, 'bmc_handlers',
                                   new=bmc_handlers):
                ipmisession.logout()

        self.assertEqual({}, bmc_handlers)
