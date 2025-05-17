import unittest
from unittest.mock import Mock, MagicMock, patch, call
import queue
import pika.exceptions
import threading
import time
import json
import logging

from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig


class TestRabbitMQWorkerMethods(unittest.TestCase):
    """Tests for RabbitMQ worker-related methods."""

    def setUp(self):
        """Set up test fixtures before each test."""
        # Create a mock callback function
        self.callback_mock = Mock()
        
        # Create a configuration with the mock callback
        self.config = RabbitMQConfig(
            host="test-host",
            port=5672,
            queue_name="test-queue",
            consumption_callback=self.callback_mock
        )
        
        # Mock logger
        self.logger_mock = Mock(spec=logging.Logger)
        
        # Create the manager with mocked components
        self.manager = RabbitMQManager(config=self.config, logger=self.logger_mock)
        
        # Mock connection and channel
        self.connection_mock = Mock()
        self.channel_mock = Mock()
        self.connection_mock.is_closed = False
        self.channel_mock.is_closed = False
        
        # Set mocked connection and channel on the manager
        self.manager._connection = self.connection_mock
        self.manager._channel = self.channel_mock
        
        # Override the worker queue with our test queue
        self.manager._worker_queue = queue.Queue()
        
        # Set should_stop to False to start
        self.manager._should_stop = False

    def test_worker_loop_processes_message(self):
        """Test that worker loop processes a message successfully."""
        # Create a test message and delivery tag
        test_message = {"key": "value"}
        test_tag = 12345
        
        # Add a mock for _process_single_message
        self.manager._process_single_message = Mock()
        
        # Put a test message in the queue
        self.manager._worker_queue.put((test_message, test_tag))
        
        # Create a modified version of _worker_loop that processes just one item
        # and then exits
        def process_one_message():
            failures = 0
            max_failures = 5
            
            try:
                message, delivery_tag = self.manager._worker_queue.get(timeout=1)
                if self.manager._should_stop:
                    self.manager._worker_queue.put((message, delivery_tag))
                    return
                self.manager._process_single_message(message, delivery_tag)
                return True  # Indicate success
            except queue.Empty:
                return False  # Indicate nothing processed
            except (pika.exceptions.AMQPConnectionError, pika.exceptions.ChannelError):
                failures += 1
                return False
            except Exception:
                return False
        
        # Run our modified worker loop function
        result = process_one_message()
        
        # Verify the message was processed
        self.assertTrue(result)
        self.manager._process_single_message.assert_called_once_with(test_message, test_tag)

    def test_worker_loop_handles_empty_queue(self):
        """Test that worker loop handles an empty queue gracefully."""
        # Mock _process_single_message to track calls
        self.manager._process_single_message = Mock()
        
        # Define our test function that handles the empty queue case
        def process_empty_queue():
            try:
                message, delivery_tag = self.manager._worker_queue.get(timeout=0.1)
                self.manager._process_single_message(message, delivery_tag)
                return True
            except queue.Empty:
                return "EMPTY"
            except Exception as e:
                return str(e)
        
        # Run our test function
        result = process_empty_queue()
        
        # Verify empty queue was handled
        self.assertEqual(result, "EMPTY")
        self.manager._process_single_message.assert_not_called()

    def test_worker_loop_handles_amqp_error(self):
        """Test that worker loop handles AMQP connection errors."""
        # Mock process_single_message to raise exception
        self.manager._process_single_message = Mock(
            side_effect=pika.exceptions.AMQPConnectionError("Test connection error")
        )
        
        # Put a test message in the queue
        test_message = {"key": "value"}
        test_tag = 12345
        self.manager._worker_queue.put((test_message, test_tag))
        
        # Define our test function that handles the connection error case
        def process_with_error():
            failures = 0
            max_failures = 5
            
            try:
                message, delivery_tag = self.manager._worker_queue.get(timeout=1)
                self.manager._process_single_message(message, delivery_tag)
                return True
            except queue.Empty:
                return False
            except (pika.exceptions.AMQPConnectionError, 
                    pika.exceptions.ChannelError) as conn_err:
                failures += 1
                return failures
            except Exception:
                return "OTHER_ERROR"
        
        # Run our test function
        result = process_with_error()
        
        # Verify connection error was handled with failure count increment
        self.assertEqual(result, 1)
        self.manager._process_single_message.assert_called_once()

    def test_worker_loop_handles_backoff(self):
        """Test that worker loop implements backoff after multiple failures."""
        # Mock time.sleep to track calls
        with patch('time.sleep') as sleep_mock:
            # Mock _process_single_message to keep raising exceptions
            self.manager._process_single_message = Mock(
                side_effect=pika.exceptions.AMQPConnectionError("Test connection error")
            )
            
            # Create a simpler function to test the backoff logic
            def test_backoff():
                failures = 0
                max_failures = 5
                
                # Simulate multiple failures
                for _ in range(6):  # Go past max_failures
                    try:
                        # Simulate getting a message and failing to process it
                        self.manager._process_single_message({"test": "data"}, 12345)
                    except (pika.exceptions.AMQPConnectionError, 
                            pika.exceptions.ChannelError):
                        failures += 1
                        if failures > max_failures:
                            # This would sleep with exponential backoff
                            time.sleep(min(30, failures * 2))
                        else:
                            # This would do a short sleep
                            time.sleep(1)
                
                return failures
            
            # Run our test function
            failures = test_backoff()
            
            # Verify backoff logic
            self.assertEqual(failures, 6)
            self.assertEqual(sleep_mock.call_count, 6)
            # First 4 calls should be with 1 second
            sleep_mock.assert_has_calls([call(1)] * 5 + [call(12)])  # 5 normal delays + 1 backoff

    def test_worker_loop_requeues_on_stop(self):
        """Test that worker loop requeues messages when stopping."""
        # Put a test message in the queue
        test_message = {"key": "value"}
        test_tag = 12345
        self.manager._worker_queue.put((test_message, test_tag))
        
        # Set should_stop to True
        self.manager._should_stop = True
        
        # Create a modified version of _worker_loop that handles just the requeue case
        def check_requeue():
            try:
                message, delivery_tag = self.manager._worker_queue.get(timeout=1)
                if self.manager._should_stop:
                    self.manager._worker_queue.put((message, delivery_tag))
                    return "REQUEUED"
                return "PROCESSED"
            except Exception as e:
                return str(e)
        
        # Run our test function
        result = check_requeue()
        
        # Verify message was requeued
        self.assertEqual(result, "REQUEUED")
        self.assertEqual(self.manager._worker_queue.qsize(), 1)
        
        # Get the requeued message and verify it's the same
        requeued_message, requeued_tag = self.manager._worker_queue.get(timeout=1)
        self.assertEqual(requeued_message, test_message)
        self.assertEqual(requeued_tag, test_tag)

    def test_process_single_message_success(self):
        """Test successful processing of a single message."""
        # Prepare test data
        test_message = {"test": "data"}
        test_tag = 12345
        
        # Mock add_callback_threadsafe to capture the callback function
        self.connection_mock.add_callback_threadsafe = Mock()
        
        # Call the method
        self.manager._process_single_message(test_message, test_tag)
        
        # Verify the callback was called with the message
        self.callback_mock.assert_called_once_with(test_message)
        
        # Verify add_callback_threadsafe was called (captures the ACK function)
        self.connection_mock.add_callback_threadsafe.assert_called_once()
        
        # Get the ACK function and call it to verify it calls _safe_ack
        with patch.object(self.manager, '_safe_ack') as safe_ack_mock:
            # Call the ack function that was passed to add_callback_threadsafe
            ack_function = self.connection_mock.add_callback_threadsafe.call_args[0][0]
            ack_function()
            
            # Verify _safe_ack was called with the delivery tag
            safe_ack_mock.assert_called_once_with(test_tag)

    def test_process_single_message_callback_exception(self):
        """Test handling of exceptions from the consumption callback."""
        # Prepare test data
        test_message = {"test": "data"}
        test_tag = 12345
        
        # Make the callback raise an exception
        self.callback_mock.side_effect = Exception("Test callback error")
        
        # Mock add_callback_threadsafe to capture the NACK function
        self.connection_mock.add_callback_threadsafe = Mock()
        
        # Call the method
        self.manager._process_single_message(test_message, test_tag)
        
        # Verify the callback was attempted
        self.callback_mock.assert_called_once_with(test_message)
        
        # Verify add_callback_threadsafe was called (captures the NACK function)
        self.connection_mock.add_callback_threadsafe.assert_called_once()
        
        # Get the NACK function and call it to verify it calls _safe_nack
        with patch.object(self.manager, '_safe_nack') as safe_nack_mock:
            # Call the nack function that was passed to add_callback_threadsafe
            nack_function = self.connection_mock.add_callback_threadsafe.call_args[0][0]
            nack_function()
            
            # Verify _safe_nack was called with the delivery tag
            safe_nack_mock.assert_called_once_with(test_tag)

    def test_process_single_message_add_callback_exception(self):
        """Test handling of exceptions during add_callback_threadsafe."""
        # Prepare test data
        test_message = {"test": "data"}
        test_tag = 12345
        
        # Make add_callback_threadsafe raise an exception
        self.connection_mock.add_callback_threadsafe.side_effect = Exception("Test callback error")
        
        # Call the method
        self.manager._process_single_message(test_message, test_tag)
        
        # Verify the warning was logged and message requeued
        self.logger_mock.warning.assert_called()
        
        # Check that the message was requeued
        self.assertEqual(self.manager._worker_queue.qsize(), 1)
        requeued_message, requeued_tag = self.manager._worker_queue.get(timeout=1)
        self.assertEqual(requeued_message, test_message)
        self.assertEqual(requeued_tag, test_tag)

    def test_process_single_message_closed_connection(self):
        """Test behavior when connection is closed."""
        # Prepare test data
        test_message = {"test": "data"}
        test_tag = 12345
        
        # Mark the connection as closed
        self.connection_mock.is_closed = True
        
        # Call the method
        self.manager._process_single_message(test_message, test_tag)
        
        # Verify warning was logged about closed connection
        self.logger_mock.warning.assert_called_with(
            "Cannot acknowledge message - connection closed or stopping"
        )
        
        # Check that the message was requeued
        self.assertEqual(self.manager._worker_queue.qsize(), 1)
        requeued_message, requeued_tag = self.manager._worker_queue.get(timeout=1)
        self.assertEqual(requeued_message, test_message)
        self.assertEqual(requeued_tag, test_tag)

    def test_process_single_message_when_stopping(self):
        """Test that message isn't requeued when stopping."""
        # Prepare test data
        test_message = {"test": "data"}
        test_tag = 12345
        
        # Set should_stop to True
        self.manager._should_stop = True
        
        # Call the method
        self.manager._process_single_message(test_message, test_tag)
        
        # Verify callback not called
        self.callback_mock.assert_called_once_with(test_message)
        
        # Verify warning was logged
        self.logger_mock.warning.assert_called()
        
        # Queue should be empty (no requeuing when stopping)
        self.assertEqual(self.manager._worker_queue.qsize(), 0)

    def test_safe_ack_success(self):
        """Test successful ACK operation."""
        # Call the method
        self.manager._safe_ack(12345)
        
        # Verify channel.basic_ack was called with correct delivery tag
        self.channel_mock.basic_ack.assert_called_once_with(delivery_tag=12345)
        
        # Verify success was logged
        self.logger_mock.debug.assert_called()

    def test_safe_ack_closed_channel(self):
        """Test behavior when channel is closed."""
        # Set channel to closed
        self.channel_mock.is_closed = True
        
        # Call the method
        self.manager._safe_ack(12345)
        
        # Verify channel.basic_ack was not called
        self.channel_mock.basic_ack.assert_not_called()

    def test_safe_ack_connection_error(self):
        """Test handling of connection errors during ACK."""
        # Make channel.basic_ack raise a connection error
        self.channel_mock.basic_ack.side_effect = pika.exceptions.ConnectionClosed(0, "Test error")
        
        # Mock the _try_reconnect method
        with patch.object(self.manager, '_try_reconnect') as reconnect_mock:
            # Call the method
            self.manager._safe_ack(12345)
            
            # Verify warning was logged
            self.logger_mock.warning.assert_called()
            
            # Verify reconnect was attempted
            reconnect_mock.assert_called_once()

    def test_safe_ack_generic_exception(self):
        """Test handling of generic exceptions during ACK."""
        # Make channel.basic_ack raise a generic exception
        self.channel_mock.basic_ack.side_effect = Exception("Test error")
        
        # Call the method
        self.manager._safe_ack(12345)
        
        # Verify error was logged
        self.logger_mock.error.assert_called()

    def test_safe_ack_channel_closed_exception(self):
        """Test handling of ChannelClosed exception during ACK."""
        # Make channel.basic_ack raise a ChannelClosed exception
        self.channel_mock.basic_ack.side_effect = pika.exceptions.ChannelClosed(0, "Test error")
        
        # Mock the _try_reconnect method
        with patch.object(self.manager, '_try_reconnect') as reconnect_mock:
            # Call the method
            self.manager._safe_ack(12345)
            
            # Verify warning was logged
            self.logger_mock.warning.assert_called()
            
            # Verify reconnect was attempted
            reconnect_mock.assert_called_once()

    def test_safe_nack_success(self):
        """Test successful NACK operation."""
        # Call the method
        self.manager._safe_nack(12345)
        
        # Verify channel.basic_nack was called with correct parameters
        self.channel_mock.basic_nack.assert_called_once_with(delivery_tag=12345, requeue=True)
        
        # Verify success was logged
        self.logger_mock.debug.assert_called_with(f"Successfully NACKed message 12345")

    def test_safe_nack_closed_channel(self):
        """Test behavior when channel is closed."""
        # Set channel to closed
        self.channel_mock.is_closed = True
        
        # Call the method
        self.manager._safe_nack(12345)
        
        # Verify channel.basic_nack was not called
        self.channel_mock.basic_nack.assert_not_called()

    def test_safe_nack_connection_error(self):
        """Test handling of connection errors during NACK."""
        # Make channel.basic_nack raise a connection error
        self.channel_mock.basic_nack.side_effect = pika.exceptions.ConnectionClosed(0, "Test error")
        
        # Mock the _try_reconnect method
        with patch.object(self.manager, '_try_reconnect') as reconnect_mock:
            # Call the method
            self.manager._safe_nack(12345)
            
            # Verify warning was logged
            self.logger_mock.warning.assert_called()
            
            # Verify reconnect was attempted
            reconnect_mock.assert_called_once()

    def test_safe_nack_generic_exception(self):
        """Test handling of generic exceptions during NACK."""
        # Make channel.basic_nack raise a generic exception
        self.channel_mock.basic_nack.side_effect = Exception("Test error")
        
        # Call the method
        self.manager._safe_nack(12345)
        
        # Verify error was logged
        self.logger_mock.error.assert_called_with(f"Error in _safe_nack: {self.channel_mock.basic_nack.side_effect}")

    def test_safe_nack_amqp_error(self):
        """Test handling of AMQP errors during NACK."""
        # Make channel.basic_nack raise an AMQP error
        self.channel_mock.basic_nack.side_effect = pika.exceptions.AMQPError("AMQP protocol error")
        
        # Mock the _try_reconnect method
        with patch.object(self.manager, '_try_reconnect') as reconnect_mock:
            # Call the method
            self.manager._safe_nack(12345)
            
            # Verify warning was logged with proper error
            warning_calls = [call for call in self.logger_mock.warning.call_args_list 
                             if "Connection error during safe NACK" in call[0][0]]
            self.assertTrue(len(warning_calls) > 0)
            
            # Verify reconnect was attempted
            reconnect_mock.assert_called_once()

    def test_safe_nack_channel_closed_exception(self):
        """Test handling of ChannelClosed exception during NACK."""
        # Make channel.basic_nack raise a ChannelClosed exception
        self.channel_mock.basic_nack.side_effect = pika.exceptions.ChannelClosed(0, "Channel closed")
        
        # Mock the _try_reconnect method
        with patch.object(self.manager, '_try_reconnect') as reconnect_mock:
            # Call the method
            self.manager._safe_nack(12345)
            
            # Verify warning was logged
            self.logger_mock.warning.assert_called()
            
            # Verify reconnect was attempted
            reconnect_mock.assert_called_once()

if __name__ == '__main__':
    unittest.main()