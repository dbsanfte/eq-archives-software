import os
import logging
import time
import argparse
import signal
import sys
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig
from indexer.es_manager import ElasticsearchManager
from indexer.openai_manager import OpenAIManager
from indexer.indexer import Indexer
from indexer.file_finder import FileFinder

class FixedWidthNameFormatter(logging.Formatter):
    def format(self, record):
        # Truncate or pad the name field
        if len(record.name) > 25:
            record.name = record.name[:25]
        else:
            record.name = record.name.ljust(25)
        return super().format(record)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
handler = logging.StreamHandler()
formatter = FixedWidthNameFormatter(
    '[%(asctime)s] %(name)s %(levelname)-8s %(message)s',
    '%Y-%m-%d %H:%M:%S'
)
handler.setFormatter(formatter)
logging.getLogger().handlers = [handler]
logger = logging.getLogger(__name__)

SLEEP_INTERVAL = int(os.environ.get("SLEEP_INTERVAL", 3600))

es_manager = ElasticsearchManager()
openai_client = OpenAIManager()
indexer = Indexer(es_manager, openai_client)

rabbitmq_config = RabbitMQConfig(
    consumption_callback=lambda body: indexer.process_file(body)
)
rabbitmq_manager = RabbitMQManager(rabbitmq_config)

file_finder = FileFinder(es_manager, rabbitmq_manager)

def signal_handler(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    if 'rabbitmq_manager' in globals():
        rabbitmq_manager.close()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def run_file_finder():
    logging.info("Starting file-finder loop...")
    try:
        while True:
            try:
                file_finder.walk_and_queue()
                logging.info(f"Sleeping for {SLEEP_INTERVAL} seconds until next run...")
                time.sleep(SLEEP_INTERVAL)
            except KeyboardInterrupt:
                logging.info("Stopping file-finder...")
                break
            except Exception as e:
                logging.error(f"Error in file-searcher loop: {e}")
                logging.exception(e)
    finally:
        # Ensure connections are properly closed
        logging.info("Closing RabbitMQ connection...")
        rabbitmq_manager.close()

def run_indexer():
    logger.info("Starting indexer...")
    print(" [*] Waiting for messages. Press CTRL-C to exit.")
    try:
        while True:
            try:
                rabbitmq_manager.start_consuming()
            except KeyboardInterrupt:
                logger.info("Stopping indexer...")
                break
            except Exception as e:
                logger.error(f"Error in indexer loop: {e}")
                logger.exception(e)
                time.sleep(5)  # Wait before reconnection attempt
    finally:
        # Ensure connections are properly closed
        logger.info("Closing RabbitMQ connection...")
        rabbitmq_manager.close()

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--indexer", action="store_true", help="Run the indexer")
    group.add_argument("--file-finder", action="store_true", help="Run the file finder")
    args = parser.parse_args()
    
    try:
        if args.indexer:
            run_indexer()
        elif args.file_finder:
            run_file_finder()
        else:
            logger.error("No actor specified. Exiting...")
            exit(1)
    finally:
        # Ensure connections are closed even if exceptions occur during setup
        if 'rabbitmq_manager' in globals():
            rabbitmq_manager.close()

if __name__ == "__main__":
    main()