import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@mui/material/Chip';
import Box from '@mui/material/Box';

const TagRow = ({ tags }) => {
  const colors = ['#e57373', '#81c784', '#64b5f6', '#ffb74d', '#ba68c8'];

  // Make sure tags is an array and sort it
  const safeTags = Array.isArray(tags) ? [...tags].sort((a, b) => a.localeCompare(b)) : [];

  return (
    <Box p={2} >
      <Box sx={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', paddingLeft: "8px" }}>
        {safeTags.map((tag, index) => (
          <Chip
            key={tag}
            label={tag}
            tooltip={"Content Tag from LLM"}
            variant="outlined"
            size="small"
            style={{ color: colors[index % colors.length] }}
          />
        ))}
      </Box>
    </Box>
  );
};

TagRow.propTypes = {
  tags: PropTypes.arrayOf(PropTypes.string).isRequired,
};

export default TagRow;