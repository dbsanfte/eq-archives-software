import React, { useCallback, useState, useEffect, useRef } from "react";
import { SearchBox, withSearch } from "@elastic/react-search-ui";
import { embeddingService } from "../../search/EmbeddingService";
import { getConfig } from "../../config/config-helper";
import debounce from "lodash/debounce";
import PropTypes from "prop-types";

const EnhancedSearchBox = ({ 
  searchTerm, 
  setSearchTerm,
  searchAsYouType,
  executeSearch,
  autocompleteSuggestions,
  ...props 
}) => {
  const { embeddingModel } = getConfig();
  const [isEmbeddingLoading, setIsEmbeddingLoading] = useState(false);
  const [internalSearchTerm, setInternalSearchTerm] = useState(searchTerm || "");
  const isUserTyping = useRef(false);
  
  // Update internal state when external searchTerm changes, but only if user is not typing
  useEffect(() => {
    if (!isUserTyping.current) {
      setInternalSearchTerm(searchTerm || "");
    }
  }, [searchTerm]);

  // Execute search only after embedding is ready
  const executeSearchWithEmbedding = useCallback(async (value) => {
    if (isEmbeddingLoading) return; // Prevent concurrent embedding fetches
    
    if (!value?.trim()) {
      setSearchTerm(value);
      return;
    }

    setIsEmbeddingLoading(true);
    
    try {
      // Fetch embedding if not cached
      if (!embeddingService.getEmbedding(value)) {
        await embeddingService.fetchEmbedding(value, embeddingModel);
      }
      
      // Only now update the actual search term in the SearchKit state, 
      // and trigger the search:
      setSearchTerm(value);
    } catch (error) {
      console.error("Error fetching embedding:", error);
      // Still try to execute search even if embedding fails
      setSearchTerm(value);
    } finally {
      setIsEmbeddingLoading(false);
      isUserTyping.current = false;
    }
  }, [embeddingModel, executeSearch, setSearchTerm, isEmbeddingLoading]);

  // Handle typing with debounce
  const debouncedSearch = useCallback(
    debounce(async (value) => {
      if (searchAsYouType) {
        await executeSearchWithEmbedding(value);
      }
    }, 400),
    [executeSearchWithEmbedding, searchAsYouType]
  );

  const handleInputChange = useCallback((value) => {
    // Mark that user is currently typing
    isUserTyping.current = true;
    
    // Only update the internal value, not the SearchKit state yet
    setInternalSearchTerm(value);
    
    // Trigger debounced search for search-as-you-type
    if (value !== searchTerm) {
      debouncedSearch(value);
    }
  }, [debouncedSearch, searchTerm]);

  return (
    <SearchBox
      // Use our internal state for rendering
      searchTerm={internalSearchTerm}
      onSubmit={async (value) => {
        // When user explicitly submits, ensure we execute with embedding
        debouncedSearch.cancel();
        isUserTyping.current = false;
        await executeSearchWithEmbedding(value);
      }}
      onChange={handleInputChange}
      onBlur={() => {
        // Reset typing flag when the input loses focus
        isUserTyping.current = false;
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
  executeSearch: PropTypes.func.isRequired,
  autocompleteSuggestions: PropTypes.oneOfType([
    PropTypes.bool,
    PropTypes.object
  ])
};

export default withSearch(({ 
  searchTerm, 
  setSearchTerm,
  searchAsYouType,
  executeSearch,
  autocompleteSuggestions
}) => ({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  executeSearch,
  autocompleteSuggestions
}))(EnhancedSearchBox);
