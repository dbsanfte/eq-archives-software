import { resolveQuery } from './Query';

// Mock the embedding service
jest.mock('./EmbeddingService', () => ({
    embeddingService: {
        isEmbeddingServiceAvailable: jest.fn(),
        getEmbedding: jest.fn()
    }
}));

import { embeddingService } from './EmbeddingService';

describe('Query.js', () => {
    let requestState;
    let requestBody;
    let searchFields;
    let paramsRef;
    let vectorFields;
    let nestedVectorFields;
    let embeddingModel;

    beforeEach(() => {
        // Reset all mocks
        jest.clearAllMocks();

        // Setup default test data
        requestState = { searchTerm: 'test query' };
        requestBody = {};
        searchFields = ['title', 'content', 'description'];
        paramsRef = {
            current: {
                enableSemanticSearch: true,
                k: 10,
                num_candidates: 100,
                boost: 1.0
            }
        };
        vectorFields = ['title_vector', 'content_vector'];
        nestedVectorFields = ['chunks'];
        embeddingModel = 'text-embedding-model';
    });

    describe('resolveQuery', () => {
        test.each(['"cleric"', 'cleric'])('requires the text match and retains date filters for %s', query => {
            requestState.searchTerm = query;
            paramsRef.current.enableSemanticSearch = false;
            const filter = [{ range: { capture_date: { gte: '2000-01-01', lte: '2000-12-31' } } }];
            requestBody.query = { bool: { should: [{ match: { text_full: query } }], filter } };
            resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);
            expect(requestBody.query.bool.filter).toEqual(filter);
            expect(requestBody.query.bool.minimum_should_match).toBe(1);
        });

        test('applies date and facet restrictions to every semantic branch as well as keyword results', () => {
            embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
            embeddingService.getEmbedding.mockReturnValue([0.1, 0.2]);
            const dates = [{ range: { capture_date: { gte: '2000-01-01', lte: '2000-12-31' } } },
                { range: { llm_guessed_date: { gte: '1999-01-01', lte: '1999-12-31' } } }];
            const facet = { term: { domain_name: 'example.org' } };
            requestBody.query = { bool: { should: [{ match: { text_full: 'cleric' } }], filter: dates } };
            requestBody.post_filter = facet;
            resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);
            expect(requestBody.query.bool.filter).toEqual(dates);
            expect(requestBody.knn).toHaveLength(3);
            for (const branch of requestBody.knn) expect(branch.filter).toEqual([...dates, facet]);
        });

        describe('queries with reserved characters', () => {
            const reservedCharTests = [
                { char: '"', query: '"exact phrase"', description: 'double quotes' },
                { char: '*', query: 'wild*card', description: 'asterisk wildcard' },
                { char: '?', query: 'test?', description: 'question mark wildcard' },
                { char: 'AND', query: 'term1 AND term2', description: 'AND operator' },
                { char: 'OR', query: 'term1 OR term2', description: 'OR operator' },
                { char: 'NOT', query: 'term1 NOT term2', description: 'NOT operator' },
                { char: '(', query: '(term1 OR term2)', description: 'parentheses grouping' },
                { char: ':', query: 'field:value', description: 'field search' },
                { char: '[', query: 'range:[1 TO 10]', description: 'range query' },
                { char: '^', query: 'boost^2', description: 'boost operator' },
                { char: '~', query: 'fuzzy~', description: 'fuzzy search' },
                { char: '+', query: '+required', description: 'required term' },
                { char: '-', query: '-excluded', description: 'excluded term' },
                { char: '&&', query: 'term1 && term2', description: 'logical AND' },
                { char: '||', query: 'term1 || term2', description: 'logical OR' },
                { char: '\\', query: 'escaped\\term', description: 'escaped character' },
                { char: '/', query: 'path/to/file', description: 'forward slash' }
            ];

            reservedCharTests.forEach(({ char, query, description }) => {
                test(`should build exact match query for ${description}`, () => {
                    requestState.searchTerm = query;

                    resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                    expect(requestBody.query).toEqual({
                        bool: {
                            minimum_should_match: 1,
                            should: [{
                                query_string: {
                                    query: query,
                                    fields: searchFields,
                                    default_operator: "AND"
                                }
                            }]
                        }
                    });
                    expect(requestBody.knn).toBeUndefined();
                });
            });
        });

        describe('semantic search queries', () => {
            beforeEach(() => {
                embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
                embeddingService.getEmbedding.mockReturnValue([0.1, 0.2, 0.3, 0.4, 0.5]);
            });

            test('should build KNN query when semantic search is enabled and service is available', () => {
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(embeddingService.getEmbedding).toHaveBeenCalledWith('simple query');
                expect(requestBody.knn).toHaveLength(3); // 2 vector fields + 1 nested field
                expect(requestBody.query).toBeUndefined();
            });

            test('should include vector field queries with correct parameters', () => {
                requestState.searchTerm = 'vector search';
                const expectedVector = [0.1, 0.2, 0.3];
                embeddingService.getEmbedding.mockReturnValue(expectedVector);

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.knn).toEqual(expect.arrayContaining([
                    {
                        field: 'title_vector',
                        query_vector: expectedVector,
                        k: 10,
                        num_candidates: 100,
                        boost: 1.0
                    },
                    {
                        field: 'content_vector',
                        query_vector: expectedVector,
                        k: 10,
                        num_candidates: 100,
                        boost: 1.0
                    }
                ]));
            });

            test('keeps nested vector matching without returning unused full text chunks', () => {
                requestState.searchTerm = 'nested search';
                const expectedVector = [0.1, 0.2, 0.3];
                embeddingService.getEmbedding.mockReturnValue(expectedVector);

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.knn).toEqual(expect.arrayContaining([
                    {
                        field: 'chunks.vector',
                        query_vector: expectedVector,
                        k: 10,
                        num_candidates: 100,
                        boost: 1.0
                    }
                ]));
            });

            test('should not build KNN query when semantic search is disabled', () => {
                paramsRef.current.enableSemanticSearch = false;
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
                expect(requestBody.knn).toBeUndefined();
                expect(requestBody.query).toBeUndefined();
            });

            test('should not build KNN query when embedding service is unavailable', () => {
                embeddingService.isEmbeddingServiceAvailable.mockReturnValue(false);
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
                expect(requestBody.knn).toBeUndefined();
                expect(requestBody.query).toBeUndefined();
            });

            test('should not build KNN query when no vector fields are provided', () => {
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, [], [], embeddingModel);

                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
                expect(requestBody.knn).toBeUndefined();
                expect(requestBody.query).toBeUndefined();
            });

            test('should not build KNN query when search term is empty', () => {
                requestState.searchTerm = '';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
                expect(requestBody.knn).toBeUndefined();
                expect(requestBody.query).toBeUndefined();
            });

            test('should handle null/undefined vector fields gracefully', () => {
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, null, undefined, embeddingModel);

                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
                expect(requestBody.knn).toBeUndefined();
            });

            test('should handle missing embedding gracefully', () => {
                embeddingService.getEmbedding.mockReturnValue(null);
                requestState.searchTerm = 'simple query';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.knn).toEqual([]);
            });

            test('should handle embedding service errors gracefully', () => {
                embeddingService.getEmbedding.mockImplementation(() => {
                    throw new Error('Embedding service error');
                });
                requestState.searchTerm = 'simple query';
                const consoleSpy = jest.spyOn(console, 'error').mockImplementation();

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(consoleSpy).toHaveBeenCalledWith('Error during embedding fetch:', expect.any(Error));
                expect(requestBody.knn).toBeUndefined();

                consoleSpy.mockRestore();
            });

            test('should use custom search parameters from paramsRef', () => {
                paramsRef.current = {
                    enableSemanticSearch: true,
                    k: 5,
                    num_candidates: 50,
                    boost: 2.0
                };
                requestState.searchTerm = 'custom params';
                embeddingService.getEmbedding.mockReturnValue([0.1, 0.2]);

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.knn[0]).toMatchObject({
                    k: 5,
                    num_candidates: 50,
                    boost: 2.0
                });
            });
        });

        describe('edge cases', () => {
            test('should handle whitespace-only search terms', () => {
                requestState.searchTerm = '   ';

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.query).toBeUndefined();
                expect(requestBody.knn).toBeUndefined();
            });

            test('should handle null search term', () => {
                requestState.searchTerm = null;

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.query).toBeUndefined();
                expect(requestBody.knn).toBeUndefined();
            });

            test('should handle undefined search term', () => {
                requestState.searchTerm = undefined;

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.query).toBeUndefined();
                expect(requestBody.knn).toBeUndefined();
            });

            test('should prioritize exact match over semantic search for queries with reserved chars', () => {
                requestState.searchTerm = 'test AND query';
                embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
                embeddingService.getEmbedding.mockReturnValue([0.1, 0.2, 0.3]);

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.query).toBeDefined();
                expect(requestBody.knn).toBeUndefined();
                expect(embeddingService.getEmbedding).not.toHaveBeenCalled();
            });

            test('should handle multiple nested vector fields', () => {
                nestedVectorFields = ['chunks', 'sections', 'paragraphs'];
                requestState.searchTerm = 'multi nested';
                embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
                embeddingService.getEmbedding.mockReturnValue([0.1, 0.2]);

                resolveQuery(requestState, requestBody, searchFields, paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.knn).toHaveLength(5); // 2 vector + 3 nested
                expect(requestBody.knn.filter(q => q.field.endsWith('.vector'))).toHaveLength(3);
            });

            test('should handle empty search fields array', () => {
                requestState.searchTerm = '"exact phrase"';
                searchFields = [];

                resolveQuery(requestState, requestBody, [], paramsRef, vectorFields, nestedVectorFields, embeddingModel);

                expect(requestBody.query.bool.should[0].query_string.fields).toEqual([]);
            });
        });
    });
});
