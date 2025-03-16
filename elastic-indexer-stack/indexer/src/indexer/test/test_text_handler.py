import os
import datetime
import json
import tempfile
import pytest
from bs4 import BeautifulSoup
from indexer.text_handler import TextHandler
from langchain_core.documents import Document
from langchain_experimental.text_splitter import SemanticChunker

MAILING_LIST_JSON_1 = '''
{
    "ygData": {
        "userId": 0,
        "authorName": "Virginia Renaut",
        "from": "Virginia Renaut &lt;vrenaut@...",
        "replyTo": "LIST",
        "senderId": "UmdMKMAkO3pWoQtBaFeiUaETz_mfAw9N8AT8LJ2_J8P97LqOooLDCSUIueLcF-oI1RJhmKwQnwIBGxqw5w0yBCqVeaf5A2Go",
        "spamInfo": {
            "isSpam": false,
            "reason": "0"
        },
        "subject": "Hello and welcome",
        "postDate": "923071557",
        "msgId": 1,
        "canDelete": false,
        "contentTrasformed": false,
        "systemMessage": false,
        "headers": {
            "messageIdInHeader": "PDE0NjcyNS4wLjEuOTU5Mjk3NTkwQGVHcm91cHMuY29tPg=="
        },
        "prevInTopic": 0,
        "nextInTopic": 0,
        "prevInTime": 0,
        "nextInTime": 2,
        "topicId": 1,
        "numMessagesInTopic": 1,
        "msgSnippet": "We re a small group so far, but hopefully we ll get more action soon!  Feel free to introduce yourself either /ooc or in character! I m Phoeble, Monk of the",
        "messageBody": "<div id=\\"ygrps-yiv-502654750\\">We&#39;re a small group so far, but hopefully we&#39;ll get more action soon!  Feel<br/>\\nfree to introduce yourself either /ooc or in character!<br/>\\n<br/>\\nI&#39;m Phoeble, Monk of the Ashen Order, Freeport.  Lately I&#39;ve spent most of my<br/>\\ntime in the E. Commonlands and around Rivervale.  Hobbies include kicking<br/>\\nskeletons and running from griffins.  I have recently attained the 10th rank<br/>\\nand look forward to seeing any of you should you be in Freeport.<br/>\\n<br/>\\nSafe journeys, Friends!</div>",
        "specialLinks": []
    }
}
'''

# Newsgroup file for testing
NEWSGROUP_POST_1 = """
From 8422924699967609371
X-Google-Language: ENGLISH,ASCII-7-bit
X-Google-Thread: fb739,728fbae1622fee86
X-Google-Attributes: gidfb739,public
X-Google-ArrivalTime: 2003-03-18 13:14:17 PST
Path: archiver1.google.com!news1.google.com!newsfeed.stanford.edu!news-spur1.maxwell.syr.edu!news.maxwell.syr.edu!diablo.theplanet.net!mephistopheles.news.clara.net!news.clara.net!landlord!wards.force9.net.POSTED!not-for-mail
User-Agent: Halime (MacOSX)/1.0rc1
Newsgroups: alt.games.everquest
Message-ID: <20030318211413100+0000@usenet.f9.net.uk>
References: <3e761e91$1@news.nucleus.com> 
	<c98c7vgq2921slr9n4dcf84o73tbpia463@4ax.com> 
	<MPG.18dfe4811a2869ed9896d1@news.ev1.net> 
	<48a72f2a.0303171448.439bc34c@posting.google.com> 
	<3E765684.5060209@hotmail.com> 
	<98eacbae.0303180448.551c7c29@posting.google.com> 
	<cnie7vkkcn297dbcco8i3sfic9ls8l3bnq@4ax.com> 
Content-Type: text/plain; charset="ISO-8859-15"
Subject: Re: stupid wankers
From: David Navarro <david@alcaudon.com>
Organization: <none>
Content-Transfer-Encoding: 8bit
Lines: 15
Date: Tue, 18 Mar 2003 21:17:11 GMT
NNTP-Posting-Host: 212.56.101.194
X-Complaints-To: abuse@plus.net.uk
X-Trace: wards.force9.net 1048022231 212.56.101.194 (Tue, 18 Mar 2003 21:17:11 GMT)
NNTP-Posting-Date: Tue, 18 Mar 2003 21:17:11 GMT
Xref: archiver1.google.com alt.games.everquest:272178

Strangely enough, Jekke, Just Jekke wrote:
> On 18 Mar 2003 04:48:58 -0800, mcbragg66@yahoo.com (brian) wrote:
> 
>>The day will come when the young outnumber the old. If that day
>>doesn't come soon - say 20 years ago - I'll be in trouble.
> 
> The young do and always have outnumbered the old.

Not for long now.

-- 
Hanrahan Thornhide, Human Druid, 61, Fennin Ro
			
"Grow a spine? He wouldn't know a spine if
it crawled towards him waving its tentacles."

"""

NEWSGROUP_POST_2 = """
From -3486961859003562007
X-Google-Language: ENGLISH,ASCII-7-bit
X-Google-Thread: fb739,8045e9dc5c8c6696
X-Google-Attributes: gidfb739,public
X-Google-ArrivalTime: 2004-03-18 08:08:34 PST
Path: archiver1.google.com!news2.google.com!news1.google.com!sn-xit-02!sn-xit-01!sn-post-02!sn-post-01!supernews.com!corp.supernews.com!not-for-mail
From: Annie Benson-Lennaman <anniebenlen@stopthevoices.yahoo.com>
Newsgroups: alt.games.everquest
Subject: Re: Is this the youngest EQ player or what?
Date: Thu, 18 Mar 2004 10:08:38 -0600
Organization: Posted via Supernews, http://www.supernews.com
Message-ID: <4059C986.9EE848D0@stopthevoices.yahoo.com>
X-Mailer: Mozilla 4.61 [en] (WinNT; U)
X-Accept-Language: en
MIME-Version: 1.0
References: <gk9e50thv677gts280t3ekrgo2ob2ht0k1@4ax.com> <KcI5c.6358$GQ3.616@newsread3.news.atl.earthlink.net> <pkpe509vj5o4fqj47uk1gog158dk08jioe@4ax.com> <KHW5c.7155$GQ3.3027@newsread3.news.atl.earthlink.net>
	        <Zelgadis.13beqa@erollisimarr-dot-com-forum.com> <4059BEA2.1011B68@stopthevoices.yahoo.com> <Zelgadis.13blfy@erollisimarr-dot-com-forum.com>
Content-Type: text/plain; charset=us-ascii
Content-Transfer-Encoding: 7bit
X-Complaints-To: abuse@supernews.com
Lines: 47
Xref: archiver1.google.com alt.games.everquest:23991



Zelgadis wrote:

 > *sigh*  Alright.  Let me clarify what I was saying.
 >
 > From what I remember in the news as EQ was growing up with Verant,
 > your EQ User Information Registration age must say at least 13 years
 > on it or else they will reject your account.

   You seem like a reasonable enough sort, Zel (unlike Mr. Lurkerr or
whatever).  I kinda hope you stick around.  And I wasn't trying to be a smartass
or cut you down.  But you should be aware that in this place, a.g.e., we do
check facts.  We will google while we post, we will google and research other
people's assertions if we feel they are in error.  And we do ask for and expect
cites.

   I can see how this might be viewed as good and as bad.  Bad, because we can
come across as niggling, harsh, and overly intolerant to dubious statements. 
But also good, because if you read a fact here about some aspect of EQ, you can
be fairly certain that if it isn't correct at the start, it will almost
certainly be corrected quickly.  Of course, is many instances the "facts" aren't
really available, and it might even be questionable if anyone in SOE even knows
the answer.  Then you will get long, drawn out discussion, usually with flaming,
where people will discuss their opinions on the matter at hand.  

  This newsgroup certainly isn't for everyone.  I've seen several  interesting,
fairly intelligent people give up on us because of not having a thick enough
skin.  But once you get used to the place, and learn the standards we hold
ourselves and others to, you might find that it is well worth the effort. 

--
Annie

To join the alt.games.everquest chat channel type /join serverwide.age:age 
If you want to stayed joined, then after that type /autojoin serverwide.age:age

Currently playing:

Teapray-- 51 High Elf Cleric on Firiona Vie

Lentea-- 36 Ogre Beastlord On Firiona Vie

Teajust-- 9 Froglok Shaman on Morden Rasp

--
If you can't figure out my email address, you're not supposed to write me.

"""

# Dummy implementations for dependencies
class DummyArchiveHandler:
    def __init__(self):
        self._mailing_lists_path = "mailing_lists"
        self._newsgroups_path = "newsgroups"
        self._websites_path = "websites"

    def _convert_to_archive_url(self, relative_path: str) -> str:
        return "http://dummy.archive/" + relative_path.replace(os.sep, "/")

    def _strip_index_html_from_url(self, url: str) -> str:
        return url.replace("index.html", "")

    def _resolve_thumbnail_url(self, url: str, file_type: str) -> str:
        return "http://dummy.thumbnail/" + file_type

class DummyOpenAIManager:
    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        return {
            "llm_summary": "dummy summary",
            "llm_summary_vector": [0.1, 0.2],
            "llm_guessed_date": "2023-01-01T00:00:00Z",
            "llm_extracted_dates": [{"date": "2023-01-01"}, {"date": "2023-01-02"}],
            "llm_model_name": "dummy-model",
            "llm_content_flavour": "dummy-flavour",
            "llm_tags": ["tag1", "tag2"]
        }

    def get_openai_embedding_client(self):
        # Return dummy embedder (unused in tests as we override chunking)
        class DummyEmbedder:
            pass
        return DummyEmbedder()

    def embed_text(self, text: str):
        return [0.1, 0.2, 0.3]

    def get_chunks_and_embeddings(self, document):
        # Simply return a single chunk using the document's page content
        return [{
            "text_chunk": document.page_content,
            "vector": self.embed_text(text=document.page_content)
        }]

# Monkeypatch the SemanticChunker.split_documents to simply return the input documents
import indexer.text_handler as text_handler_module
original_split_documents = None
@pytest.fixture(autouse=True)
def patch_semantic_chunker(monkeypatch):

    def dummy_split_documents(self, docs):
        # Return the same documents as a list; simulates one chunk per document.
        return docs
    monkeypatch.setattr(SemanticChunker, "split_documents", dummy_split_documents)

@pytest.fixture
def dummy_dependencies():
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManager()
    return archive_handler, openai_manager

def test_preprocess_mailing_list_file(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Prepare the mailing list JSON with required "date" field added.
    mailing_list_data = json.loads(__import__("textwrap").dedent(MAILING_LIST_JSON_1).strip())
    # Add a "date" field to ygData since _preprocess_mailing_list_file expects it.
    mailing_list_data["ygData"]["date"] = 923071557
    mailing_list_json = json.dumps(mailing_list_data)
    
    # Write the mailing list JSON file into a "mailing_lists" folder
    mailing_list_dir = tmp_path / "mailing_lists" / "test_list"
    mailing_list_dir.mkdir(parents=True, exist_ok=True)
    mailing_list_file = mailing_list_dir / "mailing_list.json"
    mailing_list_file.write_text(mailing_list_json, encoding="utf-8")
    
    full_path = str(mailing_list_file)
    relative_path = os.path.join("mailing_lists", "test_list", "mailing_list.json")
    
    # Get the document
    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    # The preprocessed content should be produced via markdownify from the generated HTML.
    page_content = doc.page_content
    # Check that the mailing list name (directory name) appears in the content
    assert "Mailing-list:" in page_content
    # Check that the subject from the JSON is present
    assert "Hello and welcome" in page_content
    # Check that the From header from JSON is present
    assert "From:" in page_content
    # Check that a Date header was generated (ISO format date string)
    assert "Date:" in page_content

def test_process_text_file(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    # Create a temporary file with simple HTML text (will trigger fallback case)
    file_content = "<h1>Hello World</h1><p>This is a test.</p>"
    temp_file = tmp_path / "test_file.html"
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.write_text(file_content, encoding="utf-8")
    file_path = str(temp_file)  # relative path
    full_path = str(temp_file)
    mime_type = "text/html"
    domain_name = "dummy.domain"

    docs = handler.process_text_file(relative_path=file_path, full_path=full_path, mime_type=mime_type,
                                     domain_name=domain_name)
    # Expect one call inside process_text_file that returns a single root document with nested chunks.
    assert isinstance(docs, list)
    assert len(docs) == 1

    # Validate keys in the returned root document
    root_doc = docs[0]
    expected_keys = {"id", "last_indexed", "title", "file_type", "mime_type", "text",
                     "llm_summary", "llm_summary_vector", "llm_guessed_date", "llm_extracted_dates",
                     "llm_model_name", "llm_content_flavour", "llm_tags", "domain_name",
                     "mailing_list_name", "url", "alternate_url", "thumbnail"}
    assert expected_keys.issubset(root_doc.keys())
    assert len(root_doc["text"]) == 1
    assert "Hello World" in root_doc["text"][0]["text_chunk"] # test text preservation in markdown conversion
    assert "===========" in root_doc["text"][0]["text_chunk"] # test markdown conversion
    assert "This is a test." in root_doc["text"][0]["text_chunk"] # test text preservation in markdown conversion
    assert root_doc["text"][0]["vector"] == [0.1, 0.2, 0.3] # test embedding
    
    # Check that the file url comes from our dummy converter
    assert root_doc["url"].startswith("http://dummy.archive/")

def test_resolve_title_from_file_for_website(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    # Create a temporary HTML file with a <title> tag.
    file_content = "<html><head><title>Test Page Title</title></head><body>Content</body></html>"
    # Add "websites" folder to the path to trigger the website-specific logic.
    temp_file = tmp_path / "websites" / "website_test.html"
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.write_text(file_content, encoding="utf-8")
    fake_full_path = str(temp_file)
    
    title = handler._resolve_title_from_file(relative_path="websites/website_test.html", full_path=fake_full_path)
    assert title == "Test Page Title"

def test_get_documents_from_file_default(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    # Create a generic text file that does not match any special paths.
    file_content = "# Sample Markdown Content\nThis is a test."
    temp_file = tmp_path / "generic_file.md"
    temp_file.write_text(file_content, encoding="utf-8")
    full_path = str(temp_file)
    relative_path = "generic_file.md"

    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    # Should return a list with one Document
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    # Since markdownify converts markdown to markdown, the content will be processed.
    assert isinstance(doc.page_content, str)
    assert "Sample Markdown Content" in doc.page_content
    
def test_chunk_and_embed_newsgroup_post_1(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    # Create a temporary newsgroup file for NEWSGROUP_POST_1
    newsgroup_dir = tmp_path / "newsgroups"
    newsgroup_dir.mkdir(parents=True, exist_ok=True)
    temp_file = newsgroup_dir / "post1.txt"
    temp_file.write_text(NEWSGROUP_POST_1, encoding="utf-8")
    file_path = f"newsgroups{os.sep}post1.txt"
    full_path = str(temp_file)
    # Process the newsgroup post file
    docs = handler.process_text_file(relative_path=file_path, full_path=full_path,
                                     mime_type="text/plain", domain_name="groups.google.com")
    # In our dummy chunker, we expect one chunk and one root document (total 2 docs)
    root_doc = docs[-1]
    assert isinstance(root_doc["text"], list)
    # Our dummy split returns the file as one document so expect one chunk
    chunk = root_doc["text"][0]
    # Verify that the chunk text contains the entirety of the post body beyond the headers.
    # NEWSGROUP_POST_1's text section starts with "Strangely enough, Jekke, Just Jekke wrote:"
    # and includes "Not for long now." at the end.
    assert "Strangely enough, Jekke" in chunk["text_chunk"]
    assert "Not for long now." in chunk["text_chunk"]

def test_chunk_and_embed_newsgroup_post_2(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    # Create a temporary newsgroup file for NEWSGROUP_POST_2
    newsgroup_dir = tmp_path / "newsgroups"
    newsgroup_dir.mkdir(parents=True, exist_ok=True)
    temp_file = newsgroup_dir / "post2.txt"
    temp_file.write_text(NEWSGROUP_POST_2, encoding="utf-8")
    file_path = f"newsgroups{os.sep}post2.txt"
    full_path = str(temp_file)
    # Process the file
    docs = handler.process_text_file(relative_path=file_path, full_path=full_path,
                                     mime_type="text/plain", domain_name="groups.google.com")
    root_doc = docs[-1]
    assert isinstance(root_doc["text"], list)
    chunk = root_doc["text"][0]
    # Verify that the chunk's text includes all text beyond the headers.
    # For NEWSGROUP_POST_2, the body starts with "Zelgadis wrote:" and includes discussion lines.
    assert "Zelgadis wrote:" in chunk["text_chunk"]
    assert "Currently playing:" in chunk["text_chunk"]