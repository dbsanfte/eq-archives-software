import os
import logging
import time
import argparse
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig
from indexer.es_manager import ElasticsearchManager
from indexer.openai_manager import OpenAIManager
from indexer.indexer import Indexer
from indexer.file_finder import FileFinder

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())
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

def run_file_finder():
    logging.info("Starting file-finder loop...")
    while True:
        try:
            file_finder.walk_and_queue()
            logging.info(f"Sleeping for {SLEEP_INTERVAL} seconds until next run...")
            time.sleep(SLEEP_INTERVAL)
        except Exception as e:
            logging.error(f"Error in file-searcher loop: {e}")
            logging.exception(e)

def run_indexer():
    logger.info("Starting indexer...")
    print(" [*] Waiting for messages. Press CTRL-C to exit.")
    while True:
        try:
            rabbitmq_manager.start_consuming()
        except KeyboardInterrupt as e:
            logger.info("Stopping consuming...")
            break
        except Exception as e:
            logger.error(f"Error in indexer loop: {e}")
            logger.exception(e)

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--indexer", action="store_true", help="Run the indexer")
    group.add_argument("--file-finder", action="store_true", help="Run the file finder")
    args = parser.parse_args()
    
    if args.indexer:
        run_indexer()
    elif args.file_finder:
        run_file_finder()
    else:
        logger.error("No actor specified. Exiting...")
        exit(1)
    
if __name__ == "__main__":
    main()