import React from "react";
import PropTypes from "prop-types";
import { Chip, Box } from "@mui/material";

const TagRow = ({ tags }) => {
  const safeTags = Array.isArray(tags) ? [...tags].sort((a, b) => a.localeCompare(b)) : [];

  return (
    <Box className="archive-result-tags">
      {safeTags.map(tag => (
        <Chip key={tag} label={tag} title="Suggested topic" variant="outlined" />
      ))}
    </Box>
  );
};

TagRow.propTypes = {
  tags: PropTypes.arrayOf(PropTypes.string)
};

export default TagRow;