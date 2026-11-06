import { captureIdentity } from './CaptureIdentity';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkRehype from 'remark-rehype';

export const DISCUSSION_VERSION = 'forum-discussions-v1';

// Discussion grouping never changes the exact-page identity used by readers,
// dated history and comparisons. Only source-verified metadata can opt in.
export function discussionIdentity(result) {
  const proof = result.discussion?.raw;
  const capture = captureIdentity(result);
  if (!capture || !proof || proof.version !== DISCUSSION_VERSION ||
      !['lithium', 'phpbb', 'vbulletin', 'snitz', 'ezboard'].includes(proof.platform) ||
      !/^[a-f0-9]{64}$/.test(proof.key || '') || !/^[a-f0-9]{64}$/.test(proof.source_sha256 || '') ||
      !/^[a-f0-9]{64}$/.test(proof.evidence_sha256 || '') ||
      !/^[1-9][0-9]{0,19}$/.test(proof.topic || '') ||
      typeof proof.forum !== 'string' || !proof.forum || typeof proof.title !== 'string' || !proof.title.trim()) return null;
  try {
    if (new URL(capture.originalUrl).origin !== proof.origin) return null;
    return { key: `discussion:${proof.key}`, title: proof.title, forum: proof.forum, platform: proof.platform };
  } catch (_) { return null; }
}

export function snippetHtml(result) {
  const snippet = result.text_full?.snippet;
  return Array.isArray(snippet) ? snippet.find(value => typeof value === 'string' && value.trim()) || ''
    : typeof snippet === 'string' ? snippet : '';
}

export function excerptText(result) {
  const snippet = snippetHtml(result);
  return decodeSnippet(snippet);
}

function decodeSnippet(snippet) {
  if (!snippet) return '';
  // Search snippets are HTML-escaped by Elasticsearch; em is its only markup.
  // Decode to text without mounting source HTML or executing any content.
  const node = document.createElement('textarea');
  node.innerHTML = snippet.replace(/<\/?em>/gi, '');
  return node.value;
}

export function discussionMatchKey(result) {
  const excerpt = excerptText(result).replace(/\s+/g, ' ').trim();
  return excerpt || result._meta?.id || result.id?.raw;
}

const excerptParser = unified().use(remarkParse).use(remarkGfm).use(remarkRehype);
const excerptHints = new WeakMap();
export function excerptFind(result) {
  if (excerptHints.has(result)) return excerptHints.get(result);
  // Use the reader's Markdown pipeline: destinations, markup and image alt
  // attributes are not searchable text nodes. Partial table/line fragments
  // remain separate so a snippet cannot join otherwise distinct reader nodes.
  const tree = excerptParser.runSync(excerptParser.parse(excerptText(result)));
  const terms = [...snippetHtml(result).matchAll(/<em>([\s\S]*?)<\/em>/giu)]
    .map(match => decodeSnippet(match[1]).toLowerCase()).filter(Boolean);
  let longest = '';
  let mostTerms = -1;
  function visit(node) {
    if (node.type === 'text') {
      for (const part of node.value.split(/[\r\n|]/u).map(value => value.trim())) {
        const matches = terms.filter(term => part.toLowerCase().includes(term)).length;
        if (matches > mostTerms || (matches === mostTerms && part.length > longest.length)) {
          longest = part; mostTerms = matches;
        }
      }
    }
    node.children?.forEach(visit);
  }
  visit(tree);
  const hint = Array.from(longest).slice(0, 240).join('');
  excerptHints.set(result, hint);
  return hint;
}
