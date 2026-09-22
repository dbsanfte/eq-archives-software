import React, { useEffect, useState } from 'react';
import { Button, Dialog, DialogTitle, DialogContent, DialogActions } from '@mui/material';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { DOCUMENT_UNAVAILABLE, fetchDocumentText } from '../../search/DocumentService';

export default function DocumentPreview({ open, onClose, result }) {
  const id = result._meta?.id || result.id?.raw;
  const inlineText = typeof result.text_full?.raw === 'string' ? result.text_full.raw : undefined;
  const [loaded, setLoaded] = useState(null);
  const [failure, setFailure] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const loadedId = loaded?.id;

  useEffect(() => {
    if (!open || inlineText !== undefined || (loaded && loadedId === id)) return;
    const controller = new AbortController();
    setFailure(null);
    fetchDocumentText(id, controller.signal).then(text => {
      if (!controller.signal.aborted) setLoaded({ id, text });
    }).catch(error => {
      if (!controller.signal.aborted) setFailure({
        id,
        message: error.message === DOCUMENT_UNAVAILABLE
          ? DOCUMENT_UNAVAILABLE : 'Could not load the full text. Please try again.'
      });
    });
    return () => controller.abort();
  }, [open, id, inlineText, loaded, loadedId, attempt]);

  const text = inlineText !== undefined ? inlineText : (loadedId === id ? loaded?.text : undefined);
  const error = failure?.id === id ? failure?.message : null;
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Preview Text</DialogTitle>
      <DialogContent dividers className="archive-preview-content">
        {text !== undefined ? (
          text ? <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
            : <p>No full text is available for this record.</p>
        ) : error ? (
          <div role="alert">
            <p>{error}</p>
            <Button onClick={() => setAttempt(value => value + 1)}>Try again</Button>
          </div>
        ) : <p role="status">Loading full text…</p>}
      </DialogContent>
      <DialogActions><Button onClick={onClose} color="primary">Close</Button></DialogActions>
    </Dialog>
  );
}
