import os
import json
import logging
import time
from indexing import process_file
from rabbitmq_manager import get_rabbitmq_connection
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting indexer...")
    print(" [*] Waiting for messages. Press CTRL-C to exit.")
    while True:
        try:
            start_consuming_rabbit()
        except KeyboardInterrupt as e:
            logger.info("Stopping consuming...")
            break
        except Exception as e:
            logger.error(f"Error in indexer loop: {e}")
            logger.exception(e)
            time.sleep(1) 

def start_consuming_rabbit():
    connection, channel = get_rabbitmq_connection()

    def callback(ch, method, properties, body):
        message = json.loads(body)
        process_file(message)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=os.environ.get("RABBITMQ_QUEUE", "file_paths"), on_message_callback=callback)
    channel.start_consuming()
    
if __name__ == "__main__":
    main()