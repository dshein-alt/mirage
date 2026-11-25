export class ApiError extends Error {
  constructor(code, message, status) { super(message); this.code = code; this.status = status; }
}

function csrf() { return sessionStorage.getItem('mirage.csrf') || ''; }
function mutation(method) { return ['POST', 'PUT', 'PATCH', 'DELETE'].includes((method || 'GET').toUpperCase()); }
function headers(options, contentType = 'application/json') {
  const result = {'Content-Type': contentType, ...(options.headers || {})};
  if (mutation(options.method) && csrf()) result['X-CSRF-Token'] = csrf();
  return result;
}

async function request(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {credentials: 'same-origin', ...options, headers: headers(options)});
  if (!response.ok) {
    let detail = {code: 'http_error', message: `HTTP ${response.status}`};
    try { detail = (await response.json()).error || detail; } catch {}
    if (response.status === 401 && !path.startsWith('/auth/')) document.dispatchEvent(new CustomEvent('mirage:unauthorized'));
    throw new ApiError(detail.code, detail.message, response.status);
  }
  return response.json();
}

export const api = {
  session: () => request('/auth/session'),
  login: async token => {
    const result = await request('/auth/login', {method: 'POST', body: JSON.stringify({token})});
    sessionStorage.setItem('mirage.csrf', result.csrf);
    return result;
  },
  logout: async () => { const result = await request('/auth/logout', {method: 'POST'}); sessionStorage.removeItem('mirage.csrf'); return result; },
  health: () => request('/health'),
  system: () => request('/system'),
  models: () => request('/models'),
  config: () => request('/config/generation'),
  jobs: () => request('/jobs'),
  images: (query = '') => request(`/images${query}`),
  imageDimensions: () => request('/image-dimensions'),
  runtime: () => request('/runtime'),
  settings: () => request('/settings'),
  saveSettings: document => request('/settings', {method: 'PUT', body: JSON.stringify(document)}),
  saveConfig: document => request('/config/generation', {method: 'PUT', body: JSON.stringify(document)}),
  reloadConfig: () => request('/config/generation/reload', {method: 'POST'}),
  restoreConfig: () => request('/config/generation/restore-defaults', {method: 'POST'}),
  generate: job => request('/jobs', {method: 'POST', body: JSON.stringify(job)}),
  cancel: id => request(`/jobs/${id}/cancel`, {method: 'POST'}),
  retry: id => request(`/jobs/${id}/retry`, {method: 'POST'}),
  logs: id => request(`/jobs/${id}/logs`),
  install: (id, editing) => request(`/models/${id}/install`, {method: 'POST', body: JSON.stringify({with_editing: editing})}),
  verifyModel: id => request(`/models/${id}/verify`, {method: 'POST', body: '{}'}),
  removeModel: id => request(`/models/${id}/remove`, {method: 'POST'}),
  verifyRuntime: () => request('/runtime/verify', {method: 'POST'}),
  trash: id => request(`/images/${id}`, {method: 'DELETE'}),
  deleteInput: id => request(`/inputs/${id}`, {method: 'DELETE'}),
  upload: async file => {
    const response = await fetch(`/api/v1/uploads?filename=${encodeURIComponent(file.name)}`, {method: 'POST', credentials: 'same-origin', headers: headers({method: 'POST'}, 'application/octet-stream'), body: file});
    if (!response.ok) {
      const body = await response.json();
      if (response.status === 401) document.dispatchEvent(new CustomEvent('mirage:unauthorized'));
      throw new ApiError(body.error.code, body.error.message, response.status);
    }
    return response.json();
  }
};
