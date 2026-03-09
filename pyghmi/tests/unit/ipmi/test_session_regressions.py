import unittest
from unittest import mock

from pyghmi.ipmi.private import session


class FakeEvent(object):
    def __init__(self):
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        return True


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
