import { useEffect, useState } from 'react';
import { DOCUMENT_UNAVAILABLE, fetchDocumentRecord } from '../../search/DocumentService';

export default function useDocument(id) {
  const [state, setState] = useState({});
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    setState({ id });
    fetchDocumentRecord(id, controller.signal).then(record => {
      if (!controller.signal.aborted) setState({ id, record });
    }).catch(error => {
      if (!controller.signal.aborted) setState({ id, error: error.message === DOCUMENT_UNAVAILABLE
        ? DOCUMENT_UNAVAILABLE : 'Could not load this document. Please try again.' });
    });
    return () => controller.abort();
  }, [id, attempt]);
  return { ...(state.id === id ? state : {}), retry: () => setAttempt(n => n + 1) };
}
