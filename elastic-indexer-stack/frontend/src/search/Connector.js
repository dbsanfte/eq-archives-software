// This file is responsible for creating the connector and search config objects
// that are used by the Search component to interact with the Elasticsearch API.
//
// The createConnector function creates a new ElasticsearchAPIConnector object
// with the specified host, index, and connection options. 
//
// The getSearchConfig function creates a new Config object with the specified connector.

import ElasticsearchAPIConnector from "@elastic/search-ui-elasticsearch-connector";
import { getConfig } from "../config/config-helper";
import { createConfig } from "../config/Config";
import { resolveQuery } from "./Query";

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
    // Make sure any requested sorting is applied, the default is just by _score:
    if (requestState.sortField && requestState.sortField !== "") {
        requestBody.sort[0] = {
            [requestState.sortField]: requestState.sortDirection
        };
    }

    // If no search term is provided, return the request body as is
    if (!requestState.searchTerm) return requestBody;
    
    // Otherwise, resolve the query based on the search term
    resolveQuery(
        requestState, 
        requestBody, 
        searchFields, 
        paramsRef, 
        vectorFields,
        nestedVectorFields, 
        embeddingModel)
    ;

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

