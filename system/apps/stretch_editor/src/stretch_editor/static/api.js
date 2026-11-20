// Calls to the editor's server. The server owns documents, versions and the
// background writer; the page polls it to pick up newly written sizes.
import { keyFor } from './textkit.js';

async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// The server sends specs with a mode field; the page works with plain size fields.
const clean = (spec) => Object.fromEntries(Object.entries(spec).filter(([k, v]) => v !== null && v !== undefined));
const withMode = (mode, spec) => ({ ...clean(spec), mode });

export const api = {
  versionKey: (mode, spec) => mode + ':' + keyFor(spec, mode),
  async listDocs() {
    return (await call('GET', '/api/docs')).docs;
  },
  getDoc: (id) => call('GET', `/api/docs/${id}`),
  createDoc: (title) => call('POST', '/api/docs', { title }),
  deleteDoc: (id) => call('DELETE', `/api/docs/${id}`),
  updateDoc: (id, fields) => call('PATCH', `/api/docs/${id}`, fields),
  saveVersion: (id, mode, spec, text, basedOnModel = null) =>
    call('PUT', `/api/docs/${id}/versions`, { spec: spec ? withMode(mode, spec) : null, text, based_on_model: basedOnModel }),
  lock: (id, mode, spec, locked, model) => call('POST', `/api/docs/${id}/lock`, { spec: withMode(mode, spec), locked, model }),
  write: (id, mode, spec, fresh = false, model = null) =>
    call('POST', `/api/docs/${id}/write`, { spec: withMode(mode, spec), fresh, model }),
  log: async (id, logId) => (await call('GET', `/api/docs/${id}/log/${logId}`)).entry,
  withMode
};
