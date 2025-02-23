# new file
import os
import logging
import re
import base64
import datetime
import mimetypes
import dateparser
from bs4 import BeautifulSoup
from es_manager import get_es_client, index_document
from openai_manager import call_openai_api_text, call_openai_api_image

WEBSITES_PATH = "websites/"
MAILING_LISTS_PATH = "mailing-lists/"
NEWSGROUPS_PATH = "newsgroups/"

logger = logging.getLogger(__name__)

def strip_index_html_from_url(url):
    if "/index.html" in url:
        return re.sub(r'/index\.html$', '', url)
    return url

def convert_to_archive_url(file_path):
    try:
        if WEBSITES_PATH in file_path:
            pieces = file_path.split(WEBSITES_PATH)
            chop = pieces[1]
            chop_pieces = chop.split("/")
            chop_domain = chop_pieces[0]
            chop_date = chop_pieces[1]
            chop_path_pieces = chop.split(f"/{chop_date}/")
            if len(chop_path_pieces) < 2:
                return file_path
            chop_path = chop_path_pieces[1]
            final_url = f"https://web.archive.org/web/{chop_date}/http://{chop_domain}/{chop_path}"
            return final_url
        elif MAILING_LISTS_PATH in file_path:
            pieces = file_path.split(MAILING_LISTS_PATH)
            github_path = pieces[1]
            return f"https://dbsanfte.github.io/eq-archives/mailing-lists/{github_path}"
        elif NEWSGROUPS_PATH in file_path:
            pieces = file_path.split(NEWSGROUPS_PATH)
            github_path = pieces[1]
            return f"https://dbsanfte.github.io/eq-archives/newsgroups/{github_path}"
        return ""
    except Exception as e:
        logger.error(f"Error converting file path to archive URL: {e}")
        logger.exception(e)
        return ""

def extract_domain_and_date(relative_path, full_path):
    parts = relative_path.split(os.sep)
    domain_name = None
    capture_date = None
    
    try:
        if len(parts) > 3 and parts[0] == "websites":
            domain_name = parts[1]
            capture_date = parts[2]
        
        if len(parts) > 0 and parts[0] == "newsgroups":
            # These are .txt files that have a line in them like this:
            # Date: Tue, 19 Dec 2000 14:33:46 GMT
            domain_name = "groups.google.com"
            try:
                with open(full_path, 'r') as file:
                    for line in file:
                        if line.startswith('Date: '):
                            capture_date = line[6:].strip()
                            break
                    logger.debug(f"Extracted newsgroup date: {capture_date}")
            except Exception as e:
                logger.error(f"Error extracting newsgroup date from {full_path}: {e}")
                logger.exception(e)
        
        if len(parts) > 0 and parts[0] == "mailing-lists":
            # These files have dates in them like this:
            # <b>Date:</b> Mon May 31 06:31:04 BST 1999 <br/>
            domain_name = "groups.yahoo.com"
            try:
                with open(full_path, 'r') as file:
                    for line in file:
                        if '<b>Date:</b>' in line:
                            match = re.search(r'<b>Date:</b>\s*(.*?)\s*<br/>', line)
                            if match:
                                capture_date = match.group(1).strip()
                                break
                    logger.debug(f"Extracted mailing list date: {capture_date}")
            except Exception as e:
                logger.error(f"Error extracting mailing list date from {full_path}: {e}")
                logger.exception(e)
    except Exception as e:
        logger.error(f"Error extracting domain and date from path {relative_path}: {e}")
        logger.exception(e)

    return domain_name, capture_date

def resolve_thumbnail_url(url, file_type):
    if "web.archive.org/" in url:
        if "allakhazam.com/" in url:
            return "thumbnails/allakhazam.webp"
        elif "castersrealm.com/" in url or "crgaming.com/" in url:
            return "thumbnails/castersrealm.webp"
        elif file_type == "image":
            return "thumbnails/website-image.webp"
        elif file_type == "text":
            return "thumbnails/website.webp"
        else:
            return "thumbnails/other.webp"
    elif MAILING_LISTS_PATH in url:
        return "thumbnails/mailing-list.webp"
    elif NEWSGROUPS_PATH in url:
        return "thumbnails/newsgroup.webp"
    
    if file_type == "image":
        return "thumbnails/other-image.webp"
    return "thumbnails/other.webp"

def extract_title_and_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
            soup = BeautifulSoup(content, "html.parser")
            text = soup.get_text(separator="\n")
            
            if WEBSITES_PATH in path:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.string
                else:
                    title = "Untitled"
        
            elif NEWSGROUPS_PATH in path:
                # These are .txt files that have a line in them like this:
                # Subject: Something something...
                try:
                    f.seek(0) # back to start of file
                    for line in f:
                        if line.startswith('Subject: '):
                            title = line[9:].strip()
                            break
                    logger.debug(f"Extracted newsgroup title: {title}")
                except Exception as e:
                    logger.error(f"Error extracting newsgroup subject/title from {path}: {e}")
                    logger.exception(e)
                    title = "Untitled"
                
            elif MAILING_LISTS_PATH in path:
                # These files have a line in them like this:
                # <b>Subject:</b> Re: Clerics, weight, and strength <br/>
                try:
                    f.seek(0) # back to start of file
                    for line in f:
                        if '<b>Subject:</b>' in line:
                            match = re.search(r'<b>Subject:</b>\s*(.*?)\s*<br/>', line)
                            if match:
                                title = match.group(1).strip()
                                break
                    logger.debug(f"Extracted mailing list title: {title}")
                except Exception as e:
                    logger.error(f"Error extracting mailing list subject/title from {path}: {e}")
                    logger.exception(e)
                    title = "Untitled"
            else:
                title = "Untitled"
                
        return title, text.strip()
    except Exception as e:
        logger.error(f"Error reading text file {path}: {e}")
        logger.exception(e)
        raise e

def process_file(message):
    file_path = message.get("file_path")
    if not file_path:
        logger.error("No file_path in RabbitMQ message!")
        return
    full_path = os.path.join(os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives"), file_path)
    if not os.path.isfile(full_path):
        logger.error(f"File not found on disk: {full_path}")
        return

    mime_type, _ = mimetypes.guess_type(full_path)
    domain_name, capture_date = extract_domain_and_date(file_path, full_path)
    
    if not mime_type:
        mime_type = "text/html"
    
    if capture_date is not None:
        parsed = dateparser.parse(capture_date)
        if parsed is not None:
            logger.debug(f"Parsed date: {parsed}")
            capture_date = parsed.isoformat()
        else:
            logger.debug(f"Could not parse date: {capture_date}")
            capture_date = None
    
    es = get_es_client(
        os.environ.get("ELASTICSEARCH_HOST", "elasticsearch"),
        os.environ.get("ELASTICSEARCH_PORT", "9200")
    )
    index_name = os.environ.get("ELASTICSEARCH_INDEX", "eq-archive")

    if mime_type.startswith("text") or mime_type in ["application/xhtml+xml", "application/xml", "text/html"]:
        if os.environ.get("SKIP_TEXT_FILES", "false") == "true":
            logging.info(f"Skipping text file: {file_path} because SKIP_TEXT_FILES is set.")
        else:
            process_text_file(file_path, full_path, mime_type, domain_name, capture_date, es, index_name)
    elif mime_type.startswith("image"):
        if os.environ.get("SKIP_IMAGE_FILES", "false") == "true":
            logging.info(f"Skipping image file: {file_path} because SKIP_IMAGE_FILES is set.")
        else:
            process_image_file(file_path, full_path, mime_type, domain_name, capture_date, es, index_name)
    else:
        if os.environ.get("SKIP_OTHER_FILES", "false") == "true":
            logging.info(f"Skipping other file: {file_path} because SKIP_OTHER_FILES is set.")
        else:
            process_other_file_type(file_path, mime_type, domain_name, capture_date, es, index_name)

def process_other_file_type(file_path, mime_type, domain_name, capture_date, es, index_name):
    title = os.path.basename(file_path)
    thumbnail = resolve_thumbnail_url(convert_to_archive_url(file_path), "other")
    
    if capture_date == "":
        capture_date = None
    if domain_name == "":
        domain_name = None
    
    url = convert_to_archive_url(file_path)
    alternate_url = strip_index_html_from_url(url)
    if url == "":
        url = None
    if alternate_url == "":
        alternate_url = None
    
    doc = {
        "last_indexed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "title": title,
        "thumbnail": thumbnail,
        "file_type": "other",
        "mime_type": mime_type,
        "text": None,
        "llm_model_name": None,
        "llm_summary": None,
        "llm_content_flavour": None,
        "llm_guessed_date": None,
        "domain_name": domain_name,
        "capture_date": capture_date,
        "url": url,
        "alternate_url": alternate_url
    }
    index_document(es, index_name, file_path, doc)
    logger.info(f"Indexed other file type: {file_path}")

def process_image_file(file_path, full_path, mime_type, domain_name, capture_date, es, index_name):
    try:
        with open(full_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        url = convert_to_archive_url(file_path)
        alternate_url = strip_index_html_from_url(url)
        if url == "":
            url = None
        if alternate_url == "":
            alternate_url = None
        if capture_date == "":
            capture_date = None
        title = os.path.basename(file_path)
        thumbnail = resolve_thumbnail_url(convert_to_archive_url(file_path), "image")
        llm_response = call_openai_api_image(image_b64, mime_type, domain_name)
        doc = {
            "last_indexed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": title,
            "file_path": file_path,
            "file_type": "image",
            "mime_type": mime_type,
            "thumbnail": thumbnail,
            "domain_name": domain_name,
            "capture_date": capture_date,
            "llm_model_name": llm_response.get("llm_model_name", ""),
            "llm_summary": llm_response.get("llm_summary", ""),
            "llm_image_text": llm_response.get("llm_image_text", []),
            "llm_content_flavour": llm_response.get("llm_content_flavour", None),
            "llm_image_text_vector": llm_response.get("llm_image_text_vector", []),
            "url": url,
            "alternate_url": alternate_url
        }
        index_document(es, index_name, file_path, doc)
        return doc
    except Exception as e:
        logger.error(f"Error processing image file: {e}")
        logger.exception(e)
        return {}

def process_text_file(file_path, full_path, mime_type, domain_name, capture_date, es, index_name):
    try:
        title, text_content = extract_title_and_text(full_path)
        url = convert_to_archive_url(file_path)
        alternate_url = strip_index_html_from_url(url)
        if url == "":
            url = None
        if alternate_url == "":
            alternate_url = None
        if capture_date == "":
            capture_date = None
        thumbnail = resolve_thumbnail_url(convert_to_archive_url(file_path), "image")
        
        if (os.environ.get("OPENAI_TEXT_RETRIES_ENABLED", "false") == "true"):
            logger.debug("OPENAI_TEST_RETRIES enabled, will retry OpenAI text API call if we get an empty response.")
            for _ in range(20):
                # Retry a few times to work around dodgy models not respecting output format
                llm_response = call_openai_api_text(text_content, domain_name)
                if llm_response.get("llm_summary"):
                    logger.debug("Got LLM summary.")
                    break
                else:
                    logger.warning("No LLM summary, retrying...")
            if not llm_response.get("llm_summary"):
                raise Exception("No LLM summary after 20 retries.")
        else:
            llm_response = call_openai_api_text(text_content, domain_name)
        
            
        doc = {
            "last_indexed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": title,
            "file_path": file_path,
            "file_type": "text",
            "thumbnail": thumbnail,
            "mime_type": mime_type,
            "domain_name": domain_name,
            "capture_date": capture_date,
            "text_content": text_content,
            "llm_model_name": llm_response.get("llm_model_name", None),
            "llm_summary": llm_response.get("llm_summary", None),
            "llm_content_flavour": llm_response.get("llm_content_flavour", None),
            "llm_guessed_date": llm_response.get("llm_guessed_date", None),
            "text_vector": llm_response.get("text_vector", []),
            "llm_summary_vector": llm_response.get("llm_summary_vector", []),
            "url": url,
            "alternate_url": alternate_url
        }
        index_document(es, index_name, file_path, doc)
        return doc
    except Exception as e:
        logger.error(f"Error processing text file: {e}")
        logger.exception(e)
        return {}