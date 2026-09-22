import os
import datetime
import json
import tempfile
import chardet
import random
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

MAILING_LIST_JSON_UNICODE = r'''
{
    "ygPerms": {
        "resourceCapabilityList": [
            {
                "resourceType": "GROUP",
                "capabilities": [
                    {
                        "name": "READ"
                    },
                    {
                        "name": "JOIN"
                    }
                ]
            },
            {
                "resourceType": "PHOTO",
                "capabilities": []
            },
            {
                "resourceType": "FILE",
                "capabilities": []
            },
            {
                "resourceType": "MEMBER",
                "capabilities": []
            },
            {
                "resourceType": "LINK",
                "capabilities": []
            },
            {
                "resourceType": "CALENDAR",
                "capabilities": []
            },
            {
                "resourceType": "DATABASE",
                "capabilities": []
            },
            {
                "resourceType": "POLL",
                "capabilities": []
            },
            {
                "resourceType": "MESSAGE",
                "capabilities": [
                    {
                        "name": "READ"
                    }
                ]
            },
            {
                "resourceType": "PENDING_MESSAGE",
                "capabilities": []
            },
            {
                "resourceType": "ATTACHMENTS",
                "capabilities": [
                    {
                        "name": "READ"
                    }
                ]
            },
            {
                "resourceType": "PHOTOMATIC_ALBUMS",
                "capabilities": []
            },
            {
                "resourceType": "MEMBERSHIP_TYPE",
                "capabilities": []
            },
            {
                "resourceType": "POST",
                "capabilities": [
                    {
                        "name": "READ"
                    }
                ]
            },
            {
                "resourceType": "PIN",
                "capabilities": []
            }
        ],
        "groupUrl": "groups.yahoo.com",
        "intlCode": "us"
    },
    "comscore": "pageview_candidate",
    "ygData": {
        "userId": 0,
        "authorName": "LKW",
        "from": "LKW &lt;leekw13@...",
        "replyTo": "LIST",
        "senderId": "CUJ_EbhtKCsQuDWK2kHjYfl_IrKfG7gR-rrhCj4BNtRHc1cWTWE4yJ5w_cN8pmi7SRcgH2QEH8VO6KWZB1KcX7pVXA",
        "spamInfo": {
            "isSpam": false,
            "reason": "0"
        },
        "subject": "Re: Maps and WebSites",
        "postDate": "927248608",
        "msgId": 173,
        "canDelete": false,
        "contentTrasformed": false,
        "systemMessage": false,
        "headers": {
            "messageIdInHeader": "PDE2NzI3OS4yMi4xNzMuOTU5Mjc4OTIyQGVHcm91cHMuY29tPg=="
        },
        "prevInTopic": 167,
        "nextInTopic": 174,
        "prevInTime": 172,
        "nextInTime": 174,
        "topicId": 165,
        "numMessagesInTopic": 5,
        "msgSnippet": "Simi  is LD? Btw, anyone know Innoruuk daily server down time?  Is it like UO where the last save is 30-45 min b4 the server really goes down, OR it s the time",
        "messageBody": "\u003Cdiv id=\"ygrps-yiv-61458222\"\u003ESimi  is LD? Btw, anyone know Innoruuk daily server down time?  Is it like \u003Cbr/\u003E\nUO where the last save is 30-45 min b4 the server really goes down, OR it&#39;s \u003Cbr/\u003E\nthe time we last see the msg &quot;XXX saved&quot;??\u003Cbr/\u003E\n\u003Cbr/\u003E\nWas hunting happily last night with a lvl 10 druid when the operator \u003Cbr/\u003E\nannounce abnormal server down at 2am...and I&#39;m (&^*(($@! only 20% of a bar \u003Cbr/\u003E\nof XP to lvl 8. ;(  I initially thought I can get lvl 8 last night, sigh.\u003Cbr/\u003E\n\u003Cbr/\u003E\nI heard from my druid friend that once wiz hit lvl 8, they can get new \u003Cbr/\u003E\nspells that hit up to 45 dam, is it true?  I find it hard to believe, coz \u003Cbr/\u003E\nmy spell now hit only a max of 14 dam.\u003Cbr/\u003E\n\u003Cbr/\u003E\nOn Thursday, May 20, 1999 10:30 PM, Gary Qui Hong Loong \u003Cbr/\u003E\n[SMTP:\u003Ca rel=\"nofollow\" target=\"_blank\" href=\"mailto:kanglun@...\"\u003Ekanglun@...\u003C/a\u003E] wrote:\u003Cbr/\u003E\n\u003Cblockquote\u003E\u003Cspan title=\"ireply\"\u003E &gt; From: Gary Qui Hong Loong &lt;\u003Ca rel=\"nofollow\" target=\"_blank\" href=\"mailto:kanglun@...\"\u003Ekanglun@...\u003C/a\u003E&gt;\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt;     Aye. I kana LD. that is when alot of ppl login to play. When I played \u003Cbr/\u003E\n \u003C/span\u003E\u003C/blockquote\u003Ein the\u003Cbr/\u003E\n\u003Cblockquote\u003E\u003Cspan title=\"ireply\"\u003E &gt; wee hours, ie 10pm till 7am, no problem at all. Smooth as silk.\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt; Alex wrote:\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt; &gt; From: &quot;Alex&quot; &lt;\u003Ca rel=\"nofollow\" target=\"_blank\" href=\"mailto:leealex@...\"\u003Eleealex@...\u003C/a\u003E&gt;\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Hiya,\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Just spoke  to Yutaka on Innoruke mins ago. the server is very laggy \u003Cbr/\u003E\n \u003C/span\u003E\u003C/blockquote\u003Eand was\u003Cbr/\u003E\n\u003Cblockquote\u003E\u003Cspan title=\"ireply\"\u003E &gt; &gt; LD 3 times :P\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Here&#39;s the maps i was telling Yutaka bout on Qeynos aquaduct to hunt\u003Cbr/\u003E\n&gt; &gt; Froglocks for Netted armors.\u003Cbr/\u003E\n&gt; &gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://members.door.net/enigma/eqmaps.htm\"\u003Ehttp://members.door.net/enigma/eqmaps.htm\u003C/a\u003E\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; There&#39;s the Monks only web sites\u003Cbr/\u003E\n&gt; &gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://kadanit.com/eqmonks/\"\u003Ehttp://kadanit.com/eqmonks/\u003C/a\u003E\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Join a Monk Guild..not working yet , it seems.\u003Cbr/\u003E\n&gt; &gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://eq.internet8.net/\"\u003Ehttp://eq.internet8.net/\u003C/a\u003E\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Another EQ site\u003Cbr/\u003E\n&gt; &gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://gameznet.com/eq/\"\u003Ehttp://gameznet.com/eq/\u003C/a\u003E\u003Cbr/\u003E\n&gt; &gt;\u003Cbr/\u003E\n&gt; &gt; Yiyang\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n \u003C/span\u003E\u003C/blockquote\u003E------------------------------------------------------------------------\u003Cbr/\u003E\n\u003Cblockquote\u003E\u003Cspan title=\"ireply\"\u003E &gt; &gt; Campaign 2000 is here!\u003Cbr/\u003E\n&gt; &gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://www.onelist.com\"\u003Ehttp://www.onelist.com\u003C/a\u003E\u003Cbr/\u003E\n&gt; &gt; Discuss your thoughts; get informed at ONElist.  See our homepage.\u003Cbr/\u003E\n&gt; &gt; \u003Cbr/\u003E\n \u003C/span\u003E\u003C/blockquote\u003E------------------------------------------------------------------------\u003Cbr/\u003E\n\u003Cblockquote\u003E\u003Cspan title=\"qreply\"\u003E &gt; &gt; Singapore Everquest Players List\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt; --\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt; Gary Qui Hong Loong\u003Cbr/\u003E\n&gt; Technical Support Executive\u003Cbr/\u003E\n&gt; Pacific Internet Technical Support Department .\u003Cbr/\u003E\n&gt; Pacific Internet Limited.\u003Cbr/\u003E\n&gt; ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\u003Cbr/\u003E\n&gt; Pacific Surf : \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://www.pacific.net.sg\"\u003Ehttp://www.pacific.net.sg\u003C/a\u003E\u003Cbr/\u003E\n&gt; Technical Support Helpline : 1800-8723330\u003Cbr/\u003E\n&gt; Customer Support Helpline : 1800-8725055\u003Cbr/\u003E\n&gt; 89 Science Park Drive\u003Cbr/\u003E\n&gt; #04-09/12 The Rutherford\u003Cbr/\u003E\n&gt; Singapore 118261\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt;\u003Cbr/\u003E\n&gt; ------------------------------------------------------------------------\u003Cbr/\u003E\n&gt; Are you hogging all the fun?\u003Cbr/\u003E\n&gt; \u003Ca rel=\"nofollow\" target=\"_blank\" href=\"http://www.onelist.com\"\u003Ehttp://www.onelist.com\u003C/a\u003E\u003Cbr/\u003E\n&gt; Friends tell friends about ONElist!\u003Cbr/\u003E\n&gt; ------------------------------------------------------------------------\u003Cbr/\u003E\n&gt; Singapore Everquest Players List \u003C/span\u003E\u003C/blockquote\u003E\u003C/div\u003E",
        "specialLinks": []
    }
}
'''

MAILING_LIST_JSON_UNICODE_2 = r'''
{"ygPerms":{"resourceCapabilityList":[{"resourceType":"GROUP","capabilities":[{"name":"READ"},{"name":"JOIN"}]},{"resourceType":"PHOTO","capabilities":[]},{"resourceType":"FILE","capabilities":[]},{"resourceType":"MEMBER","capabilities":[]},{"resourceType":"LINK","capabilities":[]},{"resourceType":"CALENDAR","capabilities":[]},{"resourceType":"DATABASE","capabilities":[]},{"resourceType":"POLL","capabilities":[]},{"resourceType":"MESSAGE","capabilities":[{"name":"READ"}]},{"resourceType":"PENDING_MESSAGE","capabilities":[]},{"resourceType":"ATTACHMENTS","capabilities":[{"name":"READ"}]},{"resourceType":"PHOTOMATIC_ALBUMS","capabilities":[]},{"resourceType":"MEMBERSHIP_TYPE","capabilities":[]},{"resourceType":"POST","capabilities":[{"name":"READ"}]},{"resourceType":"PIN","capabilities":[]}],"groupUrl":"groups.yahoo.com","intlCode":"us"},"comscore":"pageview_candidate","ygData":{"userId":0,"authorName":"LKW","from":"LKW &lt;leekw13@...","replyTo":"LIST","senderId":"idDE9bqCyRk03OP7T7kPFEr-ybAbthyLDxtIub2nsgfI6rlBdzEUXT2dZQcuUWelTDVxecTqpj0_631SiodXPwdSWQ","spamInfo":{"isSpam":false,"reason":"0"},"subject":"RE: Good things must shared la","postDate":"936610893","msgId":2314,"canDelete":false,"contentTrasformed":false,"systemMessage":false,"headers":{"messageIdInHeader":"PDE2NzI3OS4xNjcuMjMxNC45NTkyNzg5MjZAZUdyb3Vwcy5jb20+"},"prevInTopic":0,"nextInTopic":0,"prevInTime":2313,"nextInTime":2315,"topicId":2314,"numMessagesInTopic":1,"msgSnippet":"Any more kang-tou of similar nature?","messageBody":"<div id=\"ygrps-yiv-737031480\">Any more kang-tou of similar nature?  <br/>\n<br/>\n<blockquote><span title=\"qreply\"> On Monday, 06 September, 1999 5:11 PM, Sereph [SMTP:<a rel=\"nofollow\" target=\"_blank\" href=\"mailto:garford@...\">garford@...</a>] wrote:<br/>\n&gt; From: Sereph &lt;<a rel=\"nofollow\" target=\"_blank\" href=\"mailto:garford@...\">garford@...</a>&gt;<br/>\n&gt; <br/>\n&gt; Hmmm, about GMs, I got twinkered by one today with a FSS(Fanged Skull Stiletto) and my<br/>\n&gt; cleric friend got a Blued Two-Handed Hammer from a GM runned event. (Diablo, the event<br/>\n&gt; happen in Lesser Faydark when it was near empty at 2 this afternoon, keep a look out<br/>\n&gt; for that place)<br/>\n&gt; </span></blockquote></div>","specialLinks":[]}}
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

def test_llm_summary_placeholder(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Override the OpenAI manager to return a response without llm_summary
    def missing_summary_response(*args, **kwargs):
        return {
            # No llm_summary key
            "llm_summary_vector": [0.1, 0.2],
            "llm_guessed_date": "2023-01-01T00:00:00Z",
            "llm_extracted_dates": [{"date": "2023-01-01"}],
            "llm_model_name": "dummy-model",
            "llm_content_flavour": "dummy-flavour",
            "llm_tags": ["tag1", "tag2"]
        }
    
    # Replace the original method with our modified version
    original_method = openai_manager.call_openai_api_text
    openai_manager.call_openai_api_text = missing_summary_response
    
    # Create a simple text file
    file_content = "This is a test file."
    temp_file = tmp_path / "test_placeholder.txt"
    temp_file.write_text(file_content, encoding="utf-8")
    file_path = str(temp_file.relative_to(tmp_path))
    full_path = str(temp_file)
    
    try:
        # Process the file
        docs = handler.process_text_file(
            relative_path=file_path,
            full_path=full_path,
            mime_type="text/plain",
            domain_name="example.com"
        )
        
        # Verify that the placeholder is used for llm_summary
        assert isinstance(docs, list)
        assert len(docs) > 0
        root_doc = docs[0]
        assert root_doc["llm_summary"] == "[ Still awaiting LLM Enrichment... ]"
    finally:
        # Restore the original method
        openai_manager.call_openai_api_text = original_method

def test_preprocess_mailing_list_file(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Setup: Mock archive_handler.get_mailing_list_date to return a fixed date
    archive_handler.get_mailing_list_date = lambda full_path: ("2023-01-15")
    
    # Create a temporary mailing list JSON file
    ml_dir = tmp_path / "mailing_lists" / "eq_chat"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "message.json"
    temp_file.write_text(MAILING_LIST_JSON_1, encoding="utf-8")
    full_path = str(temp_file)
    
    # Call the method under test
    result = handler._preprocess_mailing_list_file(full_path=full_path)
    
    # Verify the output contains expected content converted to markdown
    assert "Subject: Hello and welcome" in result  # Subject 
    assert "From: Virginia Renaut" in result  # From header preserved
    assert "Mailing-list: eq\\_chat" in result  # Group name extracted from path
    assert "Date: 2023-01-15" in result  # Date from archive_handler
    assert "I'm Phoeble, Monk of the Ashen Order" in result  # Message body preserved

def test_get_documents_from_file_mailing_list(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Setup: Mock archive_handler.get_mailing_list_date to return a fixed date
    archive_handler.get_mailing_list_date = lambda full_path: ("2023-01-15")
    
    # Create a temporary mailing list JSON file
    ml_dir = tmp_path / "mailing_lists" / "eq_chat"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "message.json"
    temp_file.write_text(MAILING_LIST_JSON_1, encoding="utf-8")
    
    # Set paths for test
    full_path = str(temp_file)
    relative_path = f"mailing_lists{os.sep}eq_chat{os.sep}message.json"
    
    # Tell the dummy archive handler where to find mailing lists
    archive_handler._mailing_lists_path = "mailing_lists"
    
    # Call the method under test
    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    
    # Verify results
    assert isinstance(docs, list)
    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert "Hello and welcome" in docs[0].page_content
    assert "Virginia Renaut" in docs[0].page_content
    assert "I'm Phoeble, Monk of the Ashen Order" in docs[0].page_content
    assert docs[0].metadata["source"] == full_path

def test_get_documents_from_file_website(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary website HTML file
    website_dir = tmp_path / "websites" / "site1"
    website_dir.mkdir(parents=True, exist_ok=True)
    temp_file = website_dir / "index.html"
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Test Website</title>
    </head>
    <body>
        <h1>Welcome to Test Website</h1>
        <p>This is a sample website for testing.</p>
        <script>alert('This should be stripped');</script>
        <style>body { color: red; }</style>
    </body>
    </html>
    """
    temp_file.write_text(html_content, encoding="utf-8")
    
    # Set paths for test
    full_path = str(temp_file)
    relative_path = f"websites{os.sep}site1{os.sep}index.html"
    
    # Tell the dummy archive handler where to find websites
    archive_handler._websites_path = "websites"
    archive_handler._convert_to_archive_url = lambda relative_path: f"http://test.archive/{relative_path.replace(os.sep, '/')}"
    
    # Call the method under test
    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    
    # Verify results
    assert isinstance(docs, list)
    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert "Page URL:" in docs[0].page_content
    assert "Welcome to Test Website" in docs[0].page_content
    assert "sample website for testing" in docs[0].page_content
    assert "alert('This should be stripped')" not in docs[0].page_content  # Script tag should be stripped
    assert "body { color: red; }" not in docs[0].page_content  # Style tag should be stripped
    assert docs[0].metadata["source"] == full_path

def test_edge_case_non_json_mailing_list(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary mailing list file with wrong extension
    ml_dir = tmp_path / "mailing_lists" / "eq_chat"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "message.txt"  # Not a JSON file
    temp_file.write_text("This is not JSON", encoding="utf-8")
    
    # Set paths for test
    full_path = str(temp_file)
    
    # Tell the dummy archive handler where to find mailing lists
    archive_handler._mailing_lists_path = "mailing_lists"
    
    # Test should raise ValueError because mailing list files must be JSON
    with pytest.raises(ValueError, match="Mailing list file must be a JSON file"):
        handler._preprocess_mailing_list_file(full_path=full_path)

def test_unicode_mailing_list_json(tmp_path, dummy_dependencies):
    """Test that mailing list JSON with Unicode characters is processed correctly."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Setup: Mock archive_handler.get_mailing_list_date to return a fixed date
    archive_handler.get_mailing_list_date = lambda full_path: ("1999-05-21")
    
    # Create a temporary mailing list JSON file with Unicode content
    ml_dir = tmp_path / "mailing_lists" / "sg_everquest"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "unicode_message.json"
    temp_file.write_text(MAILING_LIST_JSON_UNICODE, encoding="utf-8")
    full_path = str(temp_file)
    
    # Call the preprocessing method
    result = handler._preprocess_mailing_list_file(full_path=full_path)
    
    # Verify the output contains expected content with Unicode characters properly converted
    assert "Subject: Re: Maps and WebSites" in result  # Subject preserved
    assert "From: LKW" in result  # From header preserved
    assert "Mailing-list: sg\\_everquest" in result  # Group name extracted from path
    assert "Date: 1999-05-21" in result  # Date from archive_handler
    
    # Check Unicode content is preserved
    assert "Simi is LD?" in result  # First line of message body
    assert "UO where the last save is 30-45 min b4 the server really goes down" in result
    assert "kanglun@..." in result  # Email preserved
    assert "Singapore Everquest Players List" in result  # Footer preserved
    
    # Now test the document creation process
    relative_path = f"mailing_lists{os.sep}sg_everquest{os.sep}unicode_message.json"
    archive_handler._mailing_lists_path = "mailing_lists"
    
    # Call the document creation method
    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    
    # Verify the resulting document contains the expected unicode content
    assert isinstance(docs, list)
    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert "Re: Maps and WebSites" in docs[0].page_content
    assert "LKW" in docs[0].page_content
    assert "Simi is LD?" in docs[0].page_content
    assert "Technical Support Executive" in docs[0].page_content
    assert docs[0].metadata["source"] == full_path

def test_unicode_mailing_list_json_2(tmp_path, dummy_dependencies):
    """Test that a second mailing list JSON with different Unicode characters is processed correctly."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Setup: Mock archive_handler.get_mailing_list_date to return a fixed date
    archive_handler.get_mailing_list_date = lambda full_path: ("1999-09-06")
    
    # Create a temporary mailing list JSON file with Unicode content
    ml_dir = tmp_path / "mailing_lists" / "sg_everquest"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "unicode_message_2.json"
    temp_file.write_text(MAILING_LIST_JSON_UNICODE_2, encoding="utf-8")
    full_path = str(temp_file)
    
    # Call the preprocessing method
    result = handler._preprocess_mailing_list_file(full_path=full_path)
    
    # Verify the output contains expected content with Unicode characters properly converted
    assert "Subject: RE: Good things must shared la" in result  # Subject preserved
    assert "From: LKW" in result  # From header preserved
    assert "Mailing-list: sg\\_everquest" in result  # Group name extracted from path
    assert "Date: 1999-09-06" in result  # Date from archive_handler
    
    # Check Unicode content is preserved
    assert "Any more kang-tou of similar nature?" in result  # First line of message body
    assert "Sereph" in result  # Name in quoted part
    assert "FSS(Fanged Skull Stiletto)" in result  # Item name preserved
    assert "Blued Two-Handed Hammer" in result  # Another item name preserved
    assert "Lesser Faydark" in result  # Location name preserved
    
    # Now test the document creation process
    relative_path = f"mailing_lists{os.sep}sg_everquest{os.sep}unicode_message_2.json"
    archive_handler._mailing_lists_path = "mailing_lists"
    
    # Call the document creation method
    docs = handler._get_documents_from_file(relative_path=relative_path, full_path=full_path)
    
    # Verify the resulting document contains the expected unicode content
    assert isinstance(docs, list)
    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert "RE: Good things must shared la" in docs[0].page_content
    assert "LKW" in docs[0].page_content
    assert "Any more kang-tou of similar nature?" in docs[0].page_content
    assert "FSS(Fanged Skull Stiletto)" in docs[0].page_content
    assert "Lesser Faydark" in docs[0].page_content
    assert docs[0].metadata["source"] == full_path

def test_process_text_file_with_unicode_mailing_list(tmp_path, dummy_dependencies):
    """
    Test that process_text_file correctly processes mailing list files with Unicode characters
    and preserves them in the final Elasticsearch document.
    """
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager,
                          llm_enrichment_enabled=False)  # Disable LLM enrichment for test
    
    # Setup: Mock archive_handler methods needed by process_text_file
    archive_handler.get_mailing_list_date = lambda full_path: "1999-05-21"
    archive_handler._convert_to_archive_url = lambda relative_path: f"http://test.archive/{relative_path.replace(os.sep, '/')}"
    archive_handler._strip_index_html_from_url = lambda url: url
    archive_handler._resolve_thumbnail_url = lambda url, file_type: None
    
    # Create a temporary mailing list JSON file with Unicode content
    ml_dir = tmp_path / "mailing_lists" / "sg_everquest"
    ml_dir.mkdir(parents=True, exist_ok=True)
    temp_file = ml_dir / "unicode_message.json"
    temp_file.write_text(MAILING_LIST_JSON_UNICODE, encoding="utf-8")
    full_path = str(temp_file)
    relative_path = f"mailing_lists{os.sep}sg_everquest{os.sep}unicode_message.json"
    
    # Tell the dummy archive handler where to find mailing lists
    archive_handler._mailing_lists_path = "mailing_lists"
    
    # Mock OpenAI manager to return predefined chunks and embeddings
    def mock_get_chunks_and_embeddings(document):
        chunks = [
            {
                "text": "Subject: Re: Maps and WebSites\nFrom: LKW",
                "chunk_id": 0,
                "vector": [0.1, 0.2, 0.3]
            },
            {
                "text": "Simi is LD? Btw, anyone know Innoruuk daily server down time?",
                "chunk_id": 1,
                "vector": [0.4, 0.5, 0.6]
            },
            {
                "text": "Technical Support Executive\nPacific Internet Limited.",
                "chunk_id": 2,
                "vector": [0.7, 0.8, 0.9]
            }
        ]
        return chunks
    openai_manager.get_chunks_and_embeddings = mock_get_chunks_and_embeddings
    
    # Call the method under test
    docs = handler.process_text_file(
        relative_path=relative_path,
        full_path=full_path,
        mime_type="application/json",
        domain_name="sg_everquest"
    )
    
    # Verify results
    assert isinstance(docs, list)
    assert len(docs) == 1
    
    doc = docs[0]
    
    # Check document structure and content
    assert doc["id"] == f"mailing_lists/sg_everquest/unicode_message.json"
    assert doc["title"] == "Re: Maps and WebSites"
    assert doc["file_type"] == "text"
    assert doc["mime_type"] == "application/json"
    assert doc["domain_name"] == "sg_everquest"
    assert doc["mailing_list_name"] == "sg_everquest"
    assert doc["url"] == f"http://test.archive/mailing_lists/sg_everquest/unicode_message.json"
    
    # Check that Unicode content was preserved in the full text
    assert "Simi is LD? Btw, anyone know Innoruuk daily server down time?" in doc["text_full"]
    assert "UO where the last save is 30-45 min b4 the server really goes down" in doc["text_full"]
    assert "(&^\\*(($@! only 20% of a bar" in doc["text_full"]  # Special characters preserved
    assert "Gary Qui Hong Loong" in doc["text_full"]  # Chinese name preserved
    assert "Technical Support Executive" in doc["text_full"]
    
    # Check that chunks contain Unicode content
    chunks = doc["text"]
    assert len(chunks) == 3
    
    # Find the chunk containing "Simi is LD?"
    unicode_chunk = next((chunk for chunk in chunks if "Simi is LD?" in chunk["text"]), None)
    assert unicode_chunk is not None
    assert "Innoruuk daily server down time" in unicode_chunk["text"]
    
    # Check metadata
    assert "Still awaiting LLM Enrichment" in doc["llm_summary"] # Since we disabled LLM enrichment

def test_detect_and_read_file_utf8(tmp_path, dummy_dependencies):
    """Test reading a UTF-8 encoded file."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a UTF-8 file
    content = "Hello, world! UTF-8 test with unicode: 你好，世界！"
    temp_file = tmp_path / "utf8_test.txt"
    temp_file.write_text(content, encoding="utf-8")
    
    # Test the method
    result_content, result_encoding = handler._detect_and_read_file(str(temp_file))
    
    # Verify results
    assert result_content is not None
    assert result_encoding is not None
    assert result_encoding.lower() in ["utf-8", "utf8", "utf_8"]  # chardet might return slightly different format
    assert result_content == content
    assert "你好，世界" in result_content

def test_detect_and_read_file_cp1252(tmp_path, dummy_dependencies):
    """Test reading a Windows-1252 encoded file."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a CP-1252 file (Windows Western European)
    cp1252_bytes = b"Hello, world! CP-1252 test with special chars: \xA3\xA9\xAE"  # £©®
    temp_file = tmp_path / "cp1252_test.txt"
    with open(temp_file, 'wb') as f:
        f.write(cp1252_bytes)
    
    # Test the method
    result_content, result_encoding = handler._detect_and_read_file(str(temp_file))
    
    # Verify results
    assert result_content is not None
    assert result_encoding is not None
    assert "Hello, world!" in result_content
    assert "£©®" in result_content

def test_detect_and_read_file_with_fallback(tmp_path, dummy_dependencies, monkeypatch):
    """Test reading a file where the first encoding detection fails but a fallback works."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Mock chardet to return an incorrect encoding first
    original_detect = chardet.detect
    def mock_detect(data):
        return {'encoding': 'utf-16', 'confidence': 0.99}
    
    monkeypatch.setattr(chardet, 'detect', mock_detect)
    
    # Create a utf-8 file that will need fallback
    content = "Hello, this is a fallback test"
    temp_file = tmp_path / "fallback_test.txt"
    temp_file.write_text(content, encoding="utf-8")
    
    # Test the method
    result_content, result_encoding = handler._detect_and_read_file(str(temp_file))
    
    # Verify results - should use one of the fallbacks
    assert result_content is not None
    assert result_encoding is not None
    assert result_encoding in ['utf-8', 'cp1252', 'latin-1']
    assert result_content == content
    
    # Restore original function
    monkeypatch.setattr(chardet, 'detect', original_detect)

def test_detect_and_read_file_nonexistent(tmp_path, dummy_dependencies):
    """Test handling a nonexistent file."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a path to a nonexistent file
    nonexistent_file = tmp_path / "does_not_exist.txt"
    
    # Test the method
    result_content, result_encoding = handler._detect_and_read_file(str(nonexistent_file))
    
    # Verify results
    assert result_content is None
    assert result_encoding is None

def test_detect_and_read_file_exception_handling(tmp_path, dummy_dependencies, monkeypatch):
    """Test various exception handling scenarios in the _detect_and_read_file method."""
    archive_handler, openai_manager = dummy_dependencies
    handler = TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a test file
    temp_file = tmp_path / "exception_test.txt"
    temp_file.write_text("Some content", encoding="utf-8")
    
    # Test case 1: chardet raises an exception
    def mock_detect_error(data):
        raise Exception("Simulated chardet error")
    
    monkeypatch.setattr(chardet, 'detect', mock_detect_error)
    
    # Test the method with chardet error
    content1, encoding1 = handler._detect_and_read_file(str(temp_file))
    assert content1 is None
    assert encoding1 is None
    
    # Restore original function
    monkeypatch.undo()
    
    # Test case 2: File permission error
    # Create a temporary file and make it read-only after writing
    temp_file2 = tmp_path / "permission_test.txt"
    temp_file2.write_text("Permission test", encoding="utf-8")
    
    # Mock open to raise permission error
    original_open = open
    def mock_open_error(file, *args, **kwargs):
        if str(file) == str(temp_file2) and 'rb' in args:
            raise PermissionError("Permission denied")
        return original_open(file, *args, **kwargs)
    
    builtin_name = 'builtins.open'
    monkeypatch.setattr(builtin_name, mock_open_error)
    
    # Test the method with permission error
    content2, encoding2 = handler._detect_and_read_file(str(temp_file2))
    assert content2 is None
    assert encoding2 is None
    
    # Restore original function
    monkeypatch.undo()
    
    # Test case 3: Internal ValueError when all encodings fail
    temp_file3 = tmp_path / "encoding_fail_test.txt" 
    
    # Create file with raw binary data that won't decode with any standard encoding
    with open(temp_file3, 'wb') as f:
        f.write(bytes([0xFF, 0xFE, 0x00, 0xFD] + [random.randint(0, 255) for _ in range(20)]))
    
    # Mock the encoding attempts to all fail but let the detection work
    def mock_open_encoding_fails(file, *args, **kwargs):
        if str(file) == str(temp_file3) and 'encoding' in kwargs:
            raise UnicodeDecodeError('charmap', b'test', 0, 1, 'Test decoding error')
        return original_open(file, *args, **kwargs)
    
    monkeypatch.setattr(builtin_name, mock_open_encoding_fails)
    
    # Test the method when all encodings fail
    content3, encoding3 = handler._detect_and_read_file(str(temp_file3))
    assert content3 is None
    assert encoding3 is None
    
    # Restore original open function
    monkeypatch.undo()