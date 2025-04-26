// This file is responsible for creating the connector and search config objects
// that are used by the Search component to interact with the Elasticsearch API.

import ElasticsearchAPIConnector from "@elastic/search-ui-elasticsearch-connector";
import { getConfig } from "../config/config-helper";
import { createConfig } from "../config/Config";
import { resolveQuery } from "./Query";
import filterRegistry from './FilterRegistry';

// Debug: Log filter count at module load time
console.log(`[Connector] Module loaded - Filter count: ${filterRegistry.getFilterCount()}`);
filterRegistry.listFilters();

export const createConnector = (paramsRef) => {
  const {
    elasticsearch_username,
    elasticsearch_password,
    indexName,
    vectorFields,
    nestedVectorFields,
    embeddingModel,
    searchFields
  } = getConfig();
  const host = `${window.location.protocol}//${window.location.host}/elasticsearch`;

  const knnPostProcess = (requestBody, requestState) => {
    // Debug: Log filter count at post-process time
    console.log(`[Connector] Post-processing - Filter count: ${filterRegistry.getFilterCount()}`);
    filterRegistry.listFilters();
    
    // Make sure any requested sorting is applied, the default is just by _score:
    if (requestState.sortField && requestState.sortField !== "") {
        requestBody.sort[0] = {
            [requestState.sortField]: requestState.sortDirection
        };
    }

    // If no search term is provided, return the request body as is
    if (!requestState.searchTerm) {
      // Apply any registered filters from our FilterRegistry service
      requestBody = filterRegistry.applyFilters(requestBody);
      return requestBody;
    }
    
    // Otherwise, resolve the query based on the search term
    resolveQuery(
        requestState, 
        requestBody, 
        searchFields, 
        paramsRef, 
        vectorFields,
        nestedVectorFields, 
        embeddingModel
    );
    
    // Apply any registered filters from our FilterRegistry service
    requestBody = filterRegistry.applyFilters(requestBody);

    return requestBody;
  };

  const connector = new ElasticsearchAPIConnector(
    {
      host,
      index: indexName,
      connectionOptions: {
        headers: {
          Authorization: "Basic " + btoa(elasticsearch_username + ":" + elasticsearch_password)
        }
      }
    },
    knnPostProcess
  );

  return connector;
};

export const getSearchConfig = (paramsRef) => {
  const connector = createConnector(paramsRef);
  return createConfig(connector);
};

