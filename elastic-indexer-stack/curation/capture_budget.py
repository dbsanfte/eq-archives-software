"""Bind missing-file retries to their original published site's Luna budget."""
import json
from pathlib import Path
import re

from common import CrawlError, digest


def published_manifest(root, prior):
    identifier = prior.get('review_id', '')
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise CrawlError('Invalid published predecessor identity')
    root = Path(root).resolve()
    path = (root / 'batches' / identifier / 'approved.json').resolve()
    try:
        if root not in path.parents or path.stat().st_size > 64 * 1024 * 1024:
            raise CrawlError('Invalid published predecessor path')
        saved = json.loads(path.read_text())
        manifest = saved['manifest']
        if (manifest['batch_id'] != identifier or saved['manifest_sha256'] != prior['manifest_sha256']
                or digest(manifest) != prior['manifest_sha256']
                or not re.fullmatch(r'[a-f0-9]{40}', saved['publication']['commit'])):
            raise CrawlError('Published predecessor no longer matches the saved retry')
    except (OSError, KeyError, TypeError, ValueError):
        raise CrawlError('Published predecessor is unavailable or invalid') from None
    return saved


def budget_identity(root, manifest):
    """Validate metadata only; source validation happens once on the new manifest."""
    if any(site.get('continued_from', {}).get('published') for site in manifest['sites']) and 'enrichment_budget_id' not in manifest:
        raise CrawlError('Published file retry must retain the original enrichment budget')
    budget = manifest.get('enrichment_budget_id', manifest['batch_id'])
    if not isinstance(budget, str) or not re.fullmatch(r'[a-f0-9]{32}', budget):
        raise CrawlError('Invalid site enrichment budget identity')
    seen = set()
    while 'enrichment_budget_id' in manifest:
        if manifest['batch_id'] in seen or len(seen) >= 100:
            raise CrawlError('Invalid published retry lineage')
        seen.add(manifest['batch_id'])
        sites = manifest['sites']
        site = sites[0]
        prior = site.get('continued_from', {})
        if len(sites) != 1 or prior.get('published') is not True or prior.get('mode') != 'retry_failed':
            raise CrawlError('Shared enrichment budget requires a published missing-file retry')
        previous = published_manifest(root, prior)['manifest']
        if (len(previous['sites']) != 1
                or {**previous['sites'][0], 'continued_from': prior} != site
                or previous.get('enrichment_budget_id', previous['batch_id']) != budget):
            raise CrawlError('Retry changed the original site or enrichment budget')
        manifest = previous
    if manifest['batch_id'] != budget:
        raise CrawlError('Retry does not use its original site enrichment budget')
    return budget
