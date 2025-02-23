// Logic for building Elasticsearch queries based on user input
//
// @param {string} queryText - The user's search query
// @param {object} requestBody - The request body to be sent to Elasticsearch
// @param {array} searchFields - The fields to search for the query
// @param {object} paramsRef - The reference to the search parameters
// @param {array} vectorFields - The fields containing vector embeddings
// @param {string} embeddingModel - The name of the embedding model
// @returns {void}
export function resolveQuery(queryText, 
                             requestBody, 
                             searchFields, 
                             paramsRef, 
                             vectorFields, 
                             nestedVectorFields, 
                             embeddingModel) {
    // If query contains double quotes, build a custom query:
    if (queryText.includes('"')) {
        requestBody.query = {
            bool: {
                should: buildExactMatchQuery(queryText, searchFields)
            }
        };
    }
    else if (paramsRef.current.enableSemanticSearch && vectorFields?.length && queryText) {
        // Perform semantic search if enabled:
        try {
            requestBody.knn = buildKnnQuery(queryText, embeddingModel, paramsRef, vectorFields, nestedVectorFields);
        }
        catch (error) {
            console.error("Error during embedding fetch:", error);
        }
    }
}

function buildKnnQuery(queryText, embeddingModel, paramsRef, vectorFields, nestedVectorFields) {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/openai/v1/embeddings", false);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.send(JSON.stringify({ input: [queryText], model: embeddingModel }));

    let knnQuery = [];
    if (xhr.status === 200) {
        const data = JSON.parse(xhr.responseText);
        const vector = data?.data?.[0].embedding;
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
                    boost,
                    inner_hits: {
                        _source: false,
                        "fields": [field+".text_chunk"],
                        highlight: {
                            fields: {
                                [field+".text_chunk"]: {}
                            }
                        }
                    }
                }));
                knnQuery = knnQuery.concat(nestedQuery);
            }
        }
        else {
            console.error("No embedding found for query:", queryText);
        }
    }
    return knnQuery;
}

function buildExactMatchQuery(queryText, searchFields) {
    let phrases = [];
    const remaining = queryText.replace(/"([^"]+)"/g, (match, p1) => {
        phrases.push(p1);
        return "";
    }).trim();

    let shouldQueries = [];

    if (remaining) {
        shouldQueries.push({
            multi_match: {
                query: remaining,
                fields: searchFields,
                type: "best_fields",
                operator: "and"
            }
        });
    }

    phrases.forEach(phrase => {
        searchFields.forEach(field => {
            shouldQueries.push({
                match_phrase: {
                    [field]: phrase
                }
            });
        });
    });
    return shouldQueries;
}