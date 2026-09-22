import { embeddingService } from './EmbeddingService';
import { usesQuerySyntax, shouldUseSemanticSearch } from './QueryPolicy';

// Logic for building Elasticsearch queries based on user input
//
// @param {string} requestState - The current request state, before postprocessing
// @param {object} requestBody - The request body to be sent to Elasticsearch
// @param {array} searchFields - The fields to search for the query
// @param {object} paramsRef - The reference to the search parameters
// @param {array} vectorFields - The fields containing vector embeddings
// @param {string} embeddingModel - The name of the embedding model
// @returns {void}
export function resolveQuery(requestState,
                             requestBody,
                             searchFields,
                             paramsRef,
                             vectorFields,
                             nestedVectorFields,
                             embeddingModel) {

    const queryText = requestState.searchTerm;

    // Early return if query is empty or whitespace-only
    if (!queryText || !queryText.trim()) {
        return;
    }

    if (usesQuerySyntax(queryText)) {
        requestBody.query = {
            bool: {
                should: buildExactMatchQuery(queryText, searchFields)
            }
        };
    }
    // Only perform semantic search if enabled AND service is available
    else if (shouldUseSemanticSearch(queryText, paramsRef.current.enableSemanticSearch, vectorFields, nestedVectorFields) &&
             embeddingService.isEmbeddingServiceAvailable()) {
        try {
            requestBody.knn = buildKnnQuery(queryText, embeddingModel, paramsRef, vectorFields, nestedVectorFields);
        }
        catch (error) {
            console.error("Error during embedding fetch:", error);
        }
    }
}

function buildKnnQuery(queryText, embeddingModel, paramsRef, vectorFields, nestedVectorFields) {
    // Get the embedding from cache instead of synchronous XHR
    const vector = embeddingService.getEmbedding(queryText);
    let knnQuery = [];

    if (vector) {
        const { k, num_candidates, boost } = paramsRef.current;
        // First query the top-level fields
        if (vectorFields?.length) {
            const vectorQuery = vectorFields.map(field => ({
                field,
                query_vector: vector,
                k,
                num_candidates,
                boost
            }));
            knnQuery = knnQuery.concat(vectorQuery);
        }

        // Then query the nested fields
        if (nestedVectorFields?.length) {
            const nestedQuery = nestedVectorFields.map(field => ({
                "field": field+".vector",
                query_vector: vector,
                k,
                num_candidates,
                boost
            }));
            knnQuery = knnQuery.concat(nestedQuery);
        }
    }
    return knnQuery;
}

function buildExactMatchQuery(queryText, searchFields) {
    /*
        We now handle:
        - Double-quoted phrases
        - Logical operators: AND, OR, NOT
        - Parentheses grouping

        We use Elasticsearch's 'query_string' to let ES parse and handle
        all operators, parentheses, and phrases correctly.
    */
    return [{
        query_string: {
            query: queryText,
            fields: searchFields,
            default_operator: "AND"
        }
    }];
}
