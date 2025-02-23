import React from "react";
import PropTypes from "prop-types";
import { Box, Typography, FormControlLabel, Checkbox } from "@mui/material";

function SearchParameters({ values, onChange }) {
  const handleCheckboxChange = (e) => {
    onChange("enableSemanticSearch", e.target.checked);
  };

  return (
    <Box
      p={2}
      sx={{ border: "1px solid #ccc", borderRadius: "4px", marginBottom: "1rem" }}
    >
      <Typography variant="subtitle2" gutterBottom>
        Search Parameters
      </Typography>
      <FormControlLabel
        control={
          <Checkbox
            checked={values.enableSemanticSearch}
            onChange={handleCheckboxChange}
            name="enableSemanticSearch"
            color="primary"
          />
        }
        label="Enable semantic search"
      />
    </Box>
  );
}

SearchParameters.propTypes = {
  values: PropTypes.shape({
    enableSemanticSearch: PropTypes.bool.isRequired,
  }).isRequired,
  onChange: PropTypes.func.isRequired,
};

export default SearchParameters;