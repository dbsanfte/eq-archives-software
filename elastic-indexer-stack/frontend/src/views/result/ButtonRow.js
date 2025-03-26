import React, { useState } from "react";
import PropTypes from "prop-types";
import { Button, Dialog, DialogTitle, DialogContent, DialogActions } from "@mui/material";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const ButtonRow = ({ result }) => {
  const [previewOpen, setPreviewOpen] = useState(false);

  const handlePreviewOpen = () => setPreviewOpen(true);
  const handlePreviewClose = () => setPreviewOpen(false);

  return (
    <>
      <div
        style={{
          marginTop: "1rem",
          display: "flex",
          gap: "1rem",
          flexWrap: "wrap",
          paddingLeft: "24px"
        }}
      >
        <Button variant="outlined" onClick={handlePreviewOpen}>
          Preview Full Text
        </Button>
        <Button
          variant="outlined"
          onClick={() => {
            if (result.alternate_url?.raw) {
              window.open(result.alternate_url.raw, "_blank", "noopener,noreferrer");
            }
          }}
        >
          Alternate Link
        </Button>
      </div>
      {/* Markdown Text Preview Dialog */}
      <Dialog open={previewOpen} onClose={handlePreviewClose} fullWidth maxWidth="md">
        <DialogTitle>Preview Text</DialogTitle>
        <DialogContent dividers>
          <Markdown remarkPlugins={[remarkGfm]}>
            {/* Use the raw text if it's a string, otherwise use an empty string */}
            {typeof result.text_full?.raw === "string" ? result.text_full.raw : ""}
          </Markdown>
        </DialogContent>
        <DialogActions>
          <Button onClick={handlePreviewClose} color="primary">
            Close
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
};

ButtonRow.propTypes = {
  result: PropTypes.shape({
    alternate_url: PropTypes.shape({
      raw: PropTypes.string,
    }),
    text_full: PropTypes.shape({
      raw: PropTypes.string,
    }),
  }).isRequired,
};

export default ButtonRow;