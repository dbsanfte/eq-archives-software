import {
  buildAutocompleteQueryConfig,
  buildStandardFacetConfigFromConfig,
  buildSearchOptionsFromConfig
} from "./config-helper";

export function createConfig(apiConnector) {
  return {
    searchQuery: {
      facets: buildStandardFacetConfigFromConfig(),
      ...buildSearchOptionsFromConfig()
    },
    autocompleteQuery: buildAutocompleteQueryConfig(),
    apiConnector,
    alwaysSearchOnInitialLoad: true
  };
}