#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock
import oslo_messaging as messaging

from heat.common import exception
from heat.engine import stack_lock
from heat.objects import stack_lock as stack_lock_object
from heat.tests import common
from heat.tests import utils


class StackLockTest(common.HeatTestCase):
    def setUp(self):
        super(StackLockTest, self).setUp()
        self.context = utils.dummy_context()
        self.stack = mock.MagicMock()
        self.stack.id = "aae01f2d-52ae-47ac-8a0d-3fde3d220fea"
        self.stack.name = "test_stack"
        self.stack.action = "CREATE"
        self.engine_id = stack_lock.StackLock.generate_engine_id()

    class TestThreadLockException(Exception):
            pass

    def test_successful_acquire_new_lock(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value=None)

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        slock.acquire()

        mock_create.assert_called_once_with(self.stack.id, self.engine_id)

    def test_failed_acquire_existing_lock_current_engine(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value=self.engine_id)

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)

        self.assertRaises(exception.ActionInProgress, slock.acquire)
        mock_create.assert_called_once_with(self.stack.id, self.engine_id)

    def test_successful_acquire_existing_lock_engine_dead(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value='fake-engine-id')
        mock_steal = self.patchobject(stack_lock_object.StackLock,
                                      'steal',
                                      return_value=None)

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        self.patchobject(slock, 'engine_alive', return_value=False)
        slock.acquire()

        mock_create.assert_called_once_with(self.stack.id, self.engine_id)
        mock_steal.assert_called_once_with(self.stack.id, 'fake-engine-id',
                                           self.engine_id)

    def test_failed_acquire_existing_lock_engine_alive(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value='fake-engine-id')

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        self.patchobject(slock, 'engine_alive', return_value=True)
        self.assertRaises(exception.ActionInProgress, slock.acquire)

        mock_create.assert_called_once_with(self.stack.id, self.engine_id)

    def test_failed_acquire_existing_lock_engine_dead(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value='fake-engine-id')
        mock_steal = self.patchobject(stack_lock_object.StackLock,
                                      'steal',
                                      return_value='fake-engine-id2')

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        self.patchobject(slock, 'engine_alive', return_value=False)
        self.assertRaises(exception.ActionInProgress, slock.acquire)

        mock_create.assert_called_once_with(self.stack.id, self.engine_id)
        mock_steal.assert_called_once_with(self.stack.id, 'fake-engine-id',
                                           self.engine_id)

    def test_successful_acquire_with_retry(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value='fake-engine-id')
        mock_steal = self.patchobject(stack_lock_object.StackLock,
                                      'steal',
                                      side_effect=[True, None])

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        self.patchobject(slock, 'engine_alive', return_value=False)
        slock.acquire()

        mock_create.assert_has_calls(
            [mock.call(self.stack.id, self.engine_id)] * 2)
        mock_steal.assert_has_calls(
            [mock.call(self.stack.id, 'fake-engine-id', self.engine_id)] * 2)

    def test_failed_acquire_one_retry_only(self):
        mock_create = self.patchobject(stack_lock_object.StackLock,
                                       'create',
                                       return_value='fake-engine-id')
        mock_steal = self.patchobject(stack_lock_object.StackLock,
                                      'steal',
                                      return_value=True)

        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        self.patchobject(slock, 'engine_alive', return_value=False)
        self.assertRaises(exception.ActionInProgress, slock.acquire)

        mock_create.assert_has_calls(
            [mock.call(self.stack.id, self.engine_id)] * 2)
        mock_steal.assert_has_calls(
            [mock.call(self.stack.id, 'fake-engine-id', self.engine_id)] * 2)

    def test_thread_lock_context_mgr_exception_acquire_success(self):
        stack_lock_object.StackLock.create = mock.Mock(return_value=None)
        stack_lock_object.StackLock.release = mock.Mock(return_value=None)
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)

        def check_thread_lock():
            with slock.thread_lock(self.stack.id):
                self.assertEqual(1,
                                 stack_lock_object.StackLock.create.call_count)
                raise self.TestThreadLockException
        self.assertRaises(self.TestThreadLockException, check_thread_lock)
        self.assertEqual(1, stack_lock_object.StackLock.release.call_count)

    def test_thread_lock_context_mgr_exception_acquire_fail(self):
        stack_lock_object.StackLock.create = mock.Mock(
            return_value=self.engine_id)
        stack_lock_object.StackLock.release = mock.Mock()
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)

        def check_thread_lock():
            with slock.thread_lock(self.stack.id):
                self.assertEqual(1,
                                 stack_lock_object.StackLock.create.call_count)
                raise exception.ActionInProgress
        self.assertRaises(exception.ActionInProgress, check_thread_lock)
        assert not stack_lock_object.StackLock.release.called

    def test_thread_lock_context_mgr_no_exception(self):
        stack_lock_object.StackLock.create = mock.Mock(return_value=None)
        stack_lock_object.StackLock.release = mock.Mock(return_value=None)
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        with slock.thread_lock(self.stack.id):
            self.assertEqual(1, stack_lock_object.StackLock.create.call_count)
        assert not stack_lock_object.StackLock.release.called

    def test_try_thread_lock_context_mgr_exception(self):
        stack_lock_object.StackLock.create = mock.Mock(return_value=None)
        stack_lock_object.StackLock.release = mock.Mock(return_value=None)
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)

        def check_thread_lock():
            with slock.try_thread_lock(self.stack.id):
                self.assertEqual(1,
                                 stack_lock_object.StackLock.create.call_count)
                raise self.TestThreadLockException
        self.assertRaises(self.TestThreadLockException, check_thread_lock)
        self.assertEqual(1, stack_lock_object.StackLock.release.call_count)

    def test_try_thread_lock_context_mgr_no_exception(self):
        stack_lock_object.StackLock.create = mock.Mock(return_value=None)
        stack_lock_object.StackLock.release = mock.Mock(return_value=None)
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        with slock.try_thread_lock(self.stack.id):
            self.assertEqual(1, stack_lock_object.StackLock.create.call_count)
        assert not stack_lock_object.StackLock.release.called

    def test_try_thread_lock_context_mgr_existing_lock(self):
        stack_lock_object.StackLock.create = mock.Mock(return_value=1234)
        stack_lock_object.StackLock.release = mock.Mock(return_value=None)
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)

        def check_thread_lock():
            with slock.try_thread_lock(self.stack.id):
                self.assertEqual(1,
                                 stack_lock_object.StackLock.create.call_count)
                raise self.TestThreadLockException
        self.assertRaises(self.TestThreadLockException, check_thread_lock)
        assert not stack_lock_object.StackLock.release.called

    def test_engine_alive_ok(self):
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        mget_client = self.patchobject(stack_lock.rpc_messaging,
                                       'get_rpc_client')
        mclient = mget_client.return_value
        mclient_ctx = mclient.prepare.return_value
        mclient_ctx.call.return_value = True
        ret = slock.engine_alive(self.context, self.engine_id)
        self.assertTrue(ret)
        mclient.prepare.assert_called_once_with(timeout=2)
        mclient_ctx.call.assert_called_once_with(self.context, 'listening')

    def test_engine_alive_timeout(self):
        slock = stack_lock.StackLock(self.context, self.stack, self.engine_id)
        mget_client = self.patchobject(stack_lock.rpc_messaging,
                                       'get_rpc_client')
        mclient = mget_client.return_value
        mclient_ctx = mclient.prepare.return_value
        mclient_ctx.call.side_effect = messaging.MessagingTimeout('too slow')
        ret = slock.engine_alive(self.context, self.engine_id)
        self.assertIs(False, ret)
        mclient.prepare.assert_called_once_with(timeout=2)
        mclient_ctx.call.assert_called_once_with(self.context, 'listening')
