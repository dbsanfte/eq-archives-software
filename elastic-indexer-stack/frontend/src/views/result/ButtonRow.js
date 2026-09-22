import React, { useContext, useState } from "react";
import PropTypes from "prop-types";
import { Button, Snackbar } from "@mui/material";
import { readerUrl, recordId, ReaderSearchContext } from "../reader/reader-utils";

const ButtonRow = ({ result }) => {
  const find = useContext(ReaderSearchContext);
  const href = readerUrl(recordId(result), { find });
  const [snackbarOpen, setSnackbarOpen] = useState(false);

  const handlePermalink = () => {
    const searchUrl = window.location.origin + href;
    Promise.resolve().then(() => navigator.clipboard.writeText(searchUrl))
      .then(() => {
        setSnackbarOpen(true);
      })
      .catch(err => {
        console.error("Failed to copy URL: ", err);
        alert("Failed to copy permalink to clipboard");
      });
  };

  const handleSnackbarClose = () => {
    setSnackbarOpen(false);
  };

  return (
    <>
      <div className="archive-result-actions">
        {recordId(result) && <Button component="a" href={href} variant="contained" className="archive-result-reader">Read document</Button>}
        <Button
          variant="text"
          onClick={() => {
            if (result.alternate_url?.raw) {
              window.open(result.alternate_url.raw, "_blank", "noopener,noreferrer");
            }
          }}
        >
          Alternate Link
        </Button>
        <Button variant="text" onClick={handlePermalink} className="archive-result-permalink">
          Copy Permalink
        </Button>
      </div>

      {/* Notification for successful copy */}
      <Snackbar
        open={snackbarOpen}
        autoHideDuration={3000}
        onClose={handleSnackbarClose}
        message="Permalink copied to clipboard"
      />
    </>
  );
};

ButtonRow.propTypes = {
  result: PropTypes.shape({
    id: PropTypes.shape({
      raw: PropTypes.string,
    }),
    alternate_url: PropTypes.shape({
      raw: PropTypes.string,
    }),
  }).isRequired,
};

export default ButtonRow;
