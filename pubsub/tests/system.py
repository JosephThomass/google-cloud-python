# Copyright 2017, Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import datetime
import itertools
import threading
import time

import mock
import pytest
import six

import google.auth
from google.cloud import pubsub_v1
from google.cloud.pubsub_v1 import exceptions
from google.cloud.pubsub_v1 import futures
from google.cloud.pubsub_v1 import types


from test_utils.system import unique_resource_id


@pytest.fixture(scope=u"module")
def project():
    _, default_project = google.auth.default()
    yield default_project


@pytest.fixture(scope=u"module")
def publisher():
    yield pubsub_v1.PublisherClient()


@pytest.fixture(scope=u"module")
def subscriber():
    yield pubsub_v1.SubscriberClient()


@pytest.fixture
def topic_path(project, publisher):
    topic_name = "t" + unique_resource_id("-")
    yield publisher.topic_path(project, topic_name)


@pytest.fixture
def subscription_path(project, subscriber):
    sub_name = "s" + unique_resource_id("-")
    yield subscriber.subscription_path(project, sub_name)


@pytest.fixture
def cleanup():
    registry = []
    yield registry

    # Perform all clean up.
    for to_call, argument in registry:
        to_call(argument)


def test_publish_messages(publisher, topic_path, cleanup):
    futures = []
    # Make sure the topic gets deleted.
    cleanup.append((publisher.delete_topic, topic_path))

    publisher.create_topic(topic_path)
    for index in six.moves.range(500):
        futures.append(
            publisher.publish(
                topic_path,
                b"The hail in Wales falls mainly on the snails.",
                num=str(index),
            )
        )
    for future in futures:
        result = future.result()
        assert isinstance(result, six.string_types)


def test_subscribe_to_messages(
    publisher, topic_path, subscriber, subscription_path, cleanup
):
    # Make sure the topic and subscription get deleted.
    cleanup.append((publisher.delete_topic, topic_path))
    cleanup.append((subscriber.delete_subscription, subscription_path))

    # Create a topic.
    publisher.create_topic(topic_path)

    # Subscribe to the topic. This must happen before the messages
    # are published.
    subscriber.create_subscription(subscription_path, topic_path)

    # Publish some messages.
    futures = [
        publisher.publish(topic_path, b"Wooooo! The claaaaaw!", num=str(index))
        for index in six.moves.range(50)
    ]

    # Make sure the publish completes.
    for future in futures:
        future.result()

    # Actually open the subscription and hold it open for a few seconds.
    # The callback should process the message numbers to prove
    # that we got everything at least once.
    callback = AckCallback()
    future = subscriber.subscribe(subscription_path, callback)
    for second in six.moves.range(10):
        time.sleep(1)

        # The callback should have fired at least fifty times, but it
        # may take some time.
        if callback.calls >= 50:
            return

    # Okay, we took too long; fail out.
    assert callback.calls >= 50

    future.cancel()


def test_subscribe_to_messages_async_callbacks(
    publisher, topic_path, subscriber, subscription_path, cleanup
):
    # Make sure the topic and subscription get deleted.
    cleanup.append((publisher.delete_topic, topic_path))
    cleanup.append((subscriber.delete_subscription, subscription_path))

    # Create a topic.
    publisher.create_topic(topic_path)

    # Subscribe to the topic. This must happen before the messages
    # are published.
    subscriber.create_subscription(subscription_path, topic_path)

    # Publish some messages.
    futures = [
        publisher.publish(topic_path, b"Wooooo! The claaaaaw!", num=str(index))
        for index in six.moves.range(2)
    ]

    # Make sure the publish completes.
    for future in futures:
        future.result()

    # We want to make sure that the callback was called asynchronously. So
    # track when each call happened and make sure below.
    callback = TimesCallback(2)

    # Actually open the subscription and hold it open for a few seconds.
    future = subscriber.subscribe(subscription_path, callback)
    for second in six.moves.range(5):
        time.sleep(4)

        # The callback should have fired at least two times, but it may
        # take some time.
        if callback.calls >= 2:
            first, last = sorted(callback.call_times[:2])
            diff = last - first
            # "Ensure" the first two callbacks were executed asynchronously
            # (sequentially would have resulted in a difference of 2+
            # seconds).
            assert diff.days == 0
            assert diff.seconds < callback.sleep_time

    # Okay, we took too long; fail out.
    assert callback.calls >= 2

    future.cancel()


class TestStreamingPull(object):
    def test_streaming_pull_callback_error_propagation(
        self, publisher, topic_path, subscriber, subscription_path, cleanup
    ):
        # Make sure the topic and subscription get deleted.
        cleanup.append((publisher.delete_topic, topic_path))
        cleanup.append((subscriber.delete_subscription, subscription_path))

        # create a topic and subscribe to it
        publisher.create_topic(topic_path)
        subscriber.create_subscription(subscription_path, topic_path)

        # publish a messages and wait until published
        future = publisher.publish(topic_path, b"hello!")
        future.result(timeout=30)

        # Now subscribe to the topic and verify that an error in the callback
        # is propagated through the streaming pull future.
        class CallbackError(Exception):
            pass

        callback = mock.Mock(side_effect=CallbackError)
        future = subscriber.subscribe(subscription_path, callback)

        with pytest.raises(CallbackError):
            future.result(timeout=30)

    def test_streaming_pull_max_messages(
        self, publisher, topic_path, subscriber, subscription_path, cleanup
    ):
        # Make sure the topic and subscription get deleted.
        cleanup.append((publisher.delete_topic, topic_path))
        cleanup.append((subscriber.delete_subscription, subscription_path))

        # create a topic and subscribe to it
        publisher.create_topic(topic_path)
        subscriber.create_subscription(subscription_path, topic_path)

        batch_sizes = (7, 4, 8, 2, 10, 1, 3, 8, 6, 1)  # total: 50
        self._publish_messages(publisher, topic_path, batch_sizes=batch_sizes)

        # now subscribe and do the main part, check for max pending messages
        total_messages = sum(batch_sizes)
        flow_control = types.FlowControl(max_messages=5)
        callback = StreamingPullCallback(
            processing_time=1, resolve_at_msg_count=total_messages
        )

        subscription_future = subscriber.subscribe(
            subscription_path, callback, flow_control=flow_control
        )

        # Expected time to process all messages in ideal case:
        #     (total_messages / FlowControl.max_messages) * processing_time
        #
        # With total=50, max messages=5, and processing_time=1 this amounts to
        # 10 seconds (+ overhead), thus a full minute should be more than enough
        # for the processing to complete. If not, fail the test with a timeout.
        try:
            callback.done_future.result(timeout=60)
        except exceptions.TimeoutError:
            pytest.fail(
                "Timeout: receiving/processing streamed messages took too long."
            )

        # The callback future gets resolved once total_messages have been processed,
        # but we want to wait for just a little bit longer to possibly catch cases
        # when the callback gets invoked *more* than total_messages times.
        time.sleep(3)

        try:
            # All messages should have been processed exactly once, and no more
            # than max_messages simultaneously at any time.
            assert callback.completed_calls == total_messages
            assert sorted(callback.seen_message_ids) == list(
                range(1, total_messages + 1)
            )
            assert callback.max_pending_ack <= flow_control.max_messages
        finally:
            subscription_future.cancel()  # trigger clean shutdown

    def _publish_messages(self, publisher, topic_path, batch_sizes):
        """Publish ``count`` messages in batches and wait until completion."""
        publish_futures = []
        msg_counter = itertools.count(start=1)

        for batch_size in batch_sizes:
            msg_batch = self._make_messages(count=batch_size)
            for msg in msg_batch:
                future = publisher.publish(
                    topic_path, msg, seq_num=str(next(msg_counter))
                )
                publish_futures.append(future)
            time.sleep(0.1)

        # wait untill all messages have been successfully published
        for future in publish_futures:
            future.result(timeout=30)

    def _make_messages(self, count):
        messages = [
            u"message {}/{}".format(i, count).encode("utf-8")
            for i in range(1, count + 1)
        ]
        return messages


class AckCallback(object):
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, message):
        message.ack()
        # Only increment the number of calls **after** finishing.
        with self.lock:
            self.calls += 1


class TimesCallback(object):
    def __init__(self, sleep_time):
        self.sleep_time = sleep_time
        self.calls = 0
        self.call_times = []
        self.lock = threading.Lock()

    def __call__(self, message):
        now = datetime.datetime.now()
        time.sleep(self.sleep_time)
        message.ack()
        # Only increment the number of calls **after** finishing.
        with self.lock:
            # list.append() is thread-safe, but we still wait until
            # ``calls`` is incremented to do it.
            self.call_times.append(now)
            self.calls += 1


class StreamingPullCallback(object):
    def __init__(self, processing_time, resolve_at_msg_count):
        self._lock = threading.Lock()
        self._processing_time = processing_time
        self._pending_ack = 0
        self.max_pending_ack = 0
        self.completed_calls = 0
        self.seen_message_ids = []

        self._resolve_at_msg_count = resolve_at_msg_count
        self.done_future = futures.Future()

    def __call__(self, message):
        with self._lock:
            self._pending_ack += 1
            self.max_pending_ack = max(self.max_pending_ack, self._pending_ack)
            self.seen_message_ids.append(int(message.attributes["seq_num"]))

        time.sleep(self._processing_time)

        with self._lock:
            self._pending_ack -= 1
            message.ack()
            self.completed_calls += 1

            if self.completed_calls >= self._resolve_at_msg_count:
                if not self.done_future.done():
                    self.done_future.set_result(None)
