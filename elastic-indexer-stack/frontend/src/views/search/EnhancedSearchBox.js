import React, { useCallback, useContext, useEffect, useMemo, useRef } from "react";
import { SearchBox, withSearch } from "@elastic/react-search-ui";
import { embeddingService } from "../../search/EmbeddingService";
import { getConfig } from "../../config/config-helper";
import debounce from "lodash/debounce";
import { shouldUseSemanticSearch } from "../../search/QueryPolicy";
import PropTypes from "prop-types";
import { SearchActivityContext, useSearchActivity } from '../../search/SearchActivity';

// Keep the busy indication in the input row as well as beneath it: mobile
// keyboards can obscure everything below a focused search field.
function SearchInput({ getInputProps, getButtonProps, getAutocomplete }) {
  const { phase } = useSearchActivity();
  const busy = ['waiting', 'preparing', 'searching', 'reranking'].includes(phase);
  return <>
    <div className="sui-search-box__wrapper">
      <input {...getInputProps({ 'aria-busy': busy })} />
      {getAutocomplete()}
    </div>
    <input {...getButtonProps({ value: busy ? 'Searching…' : 'Search', 'aria-label': 'Search', 'aria-busy': busy })} />
  </>;
}

const EnhancedSearchBox = ({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions,
  enableSemanticSearch = true,
  ...props
}) => {
  const activity = useContext(SearchActivityContext);
  const semanticEnabled = useRef(enableSemanticSearch);
  semanticEnabled.current = enableSemanticSearch;
  const revision = useRef(0);
  const activeEmbedding = useRef(null);
  const latestValue = useRef(searchTerm);

  const executeSearchWithEmbedding = useCallback(async (value, requestRevision) => {
    const { embeddingModel, vectorFields, nestedVectorFields } = getConfig();
    const controller = new AbortController();
    activeEmbedding.current = controller;
    try {
      if (shouldUseSemanticSearch(value, semanticEnabled.current, vectorFields, nestedVectorFields) &&
          embeddingService.isEmbeddingServiceAvailable() && !embeddingService.getEmbedding(value, embeddingModel)) {
        activity?.preparing(value);
        await embeddingService.fetchEmbedding(value, embeddingModel, { signal: controller.signal });
      }
    } catch (error) {
      console.error("Error fetching embedding:", error);
      // The current query can still fall back to text search.
    } finally {
      if (activeEmbedding.current === controller) activeEmbedding.current = null;
    }

    // Typing, submitting, navigating, or unmounting invalidates older work.
    if (requestRevision === revision.current) {
      setSearchTerm(value, { shouldClearFilters: false });
    }
  }, [setSearchTerm, activity]);

  const debouncedSearch = useMemo(
    () => debounce(executeSearchWithEmbedding, 400),
    [executeSearchWithEmbedding]
  );

  useEffect(() => () => {
    debouncedSearch.cancel();
    activeEmbedding.current?.abort();
    revision.current += 1;
  }, [debouncedSearch]);

  useEffect(() => {
    // Honor changes made outside the input, such as browser history navigation.
    if (searchTerm !== latestValue.current) {
      latestValue.current = searchTerm;
      revision.current += 1;
      debouncedSearch.cancel();
      activeEmbedding.current?.abort();
    }
  }, [searchTerm, debouncedSearch]);

  const updateInput = (value) => {
    debouncedSearch.cancel();
    activeEmbedding.current?.abort();
    const requestRevision = ++revision.current;
    latestValue.current = value;
    activity?.draft(value);
    // Search UI owns the input value. Update it immediately, but defer the
    // network search until the debounce and embedding preparation complete.
    setSearchTerm(value, { refresh: false });
    return requestRevision;
  };

  return (
    <SearchBox
      inputView={SearchInput}
      searchTerm={searchTerm || ""}
      onChange={(value) => {
        const requestRevision = updateInput(value);
        if (searchAsYouType) debouncedSearch(value, requestRevision);
      }}
      onSubmit={(value) => {
        const requestRevision = updateInput(value);
        return executeSearchWithEmbedding(value, requestRevision);
      }}
      autocompleteSuggestions={autocompleteSuggestions}
      {...props}
    />
  );
};

EnhancedSearchBox.propTypes = {
  searchTerm: PropTypes.string,
  setSearchTerm: PropTypes.func.isRequired,
  searchAsYouType: PropTypes.bool,
  enableSemanticSearch: PropTypes.bool,
  autocompleteSuggestions: PropTypes.oneOfType([
    PropTypes.bool,
    PropTypes.object
  ])
};

export default withSearch(({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions
}) => ({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions
}))(EnhancedSearchBox);
