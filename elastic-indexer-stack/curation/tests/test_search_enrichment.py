import pytest

from indexer.search_enrichment import VIEW_BYTES, evidence_parts, verified_metadata


@pytest.mark.parametrize('role', ['navigation', 'directory', 'off-topic'])
def test_roles_without_authored_subjects_ignore_even_literal_labels(role):
    result = {'content_role': role, 'topics': [{'name': 'tailoring', 'excerpt': 'See tailoring guides.'}]}
    metadata, receipts = verified_metadata(result, ('See tailoring guides.',))
    assert metadata['llm_content_role'] == role
    assert metadata['llm_topics'] == metadata['llm_entities'] == receipts == []


def test_verbatim_evidence_preserves_japanese_and_rejects_substrings_and_invalid_rows():
    result = {'topics': [None, {'name': '青の洞窟', 'excerpt': '青の洞窟に入る'},
                         {'name': 'ration', 'excerpt': 'The rationed food supplies.'},
                         {'name': 'stuff\x00', 'excerpt': 'There was stuff.'}],
              'entities': [{'kind': 'invalid', 'name': 'Kunark', 'excerpt': 'We traveled to Kunark.'}]}
    parts = ('私たちは青の洞窟に入るので静かに。 The rationed food supplies. There was stuff. We traveled to Kunark.',)
    metadata, receipts = verified_metadata(result, parts)
    assert metadata['llm_content_role'] == 'unknown'
    assert metadata['llm_topics'] == ['青の洞窟']
    assert metadata['llm_entities'] == []
    assert receipts == [{'field': 'llm_topics', 'label': '青の洞窟', 'excerpt': '青の洞窟に入る'}]


def test_bounded_projection_excludes_synthetic_destinations_and_cannot_join_disjoint_excerpts():
    assert not evidence_parts('**Page URL:** http://example.org/\n')
    view = '\n'.join(('**Alternate Page URL:** http://example.org/', '[Meet](https://example.org/ads)',
                      'End of first block.', '競争\n' * VIEW_BYTES, 'Start of last block.', 'Departure'))
    parts = evidence_parts(view)
    assert len(parts) == 2
    assert parts[0].startswith('[Meet]\n')
    assert not any('https://' in part or 'Page URL' in part or '�' in part for part in parts)
    assert sum(len(part.encode()) for part in parts) <= VIEW_BYTES
    result = {'content_role': 'roster', 'topics': [{'name': 'first block', 'excerpt': 'End of first block. Start of last block.'}],
              'entities': 'not-an-array'}
    metadata, receipts = verified_metadata(result, parts)
    assert metadata['llm_topics'] == metadata['llm_entities'] == receipts == []
    assert metadata['llm_content_role'] == 'roster'
