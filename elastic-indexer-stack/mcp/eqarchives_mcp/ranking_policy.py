"""Server-owned retrieval defaults; callers cannot supply ranking policies."""

HYBRID_K = 50
HYBRID_CANDIDATES = 250
HYBRID_BOOST = 30
BROWSER_VECTOR_BOOST = 10
RERANK_WINDOW = 60
PASSAGE_MODE = "semantic"


def hybrid_branches(vector, model):
    return [{
        "field": field, "query_vector": vector, "k": HYBRID_K,
        "num_candidates": HYBRID_CANDIDATES, "boost": HYBRID_BOOST,
        **({"filter": {"term": {"embedding_model": model}}}
           if model == "embeddinggemma-2-768-v1" else {}),
    } for field in ("text.vector", "llm_summary_vector", "llm_image_text_vector", "image_vector")]
