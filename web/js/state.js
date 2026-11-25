export const state = {models: [], config: null, jobs: [], images: [], page: 'generate', selectedReferences: [], generatedReference: null, auth: {role: 'admin', auth_required: false}};
export function modelById(id) { return state.models.find(model => model.id === id); }
export function element(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key.startsWith('data-')) node.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}
export function showError(target, error) { target.textContent = error.message || String(error); target.classList.remove('hidden'); }
export function clearError(target) { target.textContent = ''; target.classList.add('hidden'); }
let toastTimer;
export function toast(message) {
  const node = document.querySelector('#toast');
  node.textContent = message; node.classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.add('hidden'), 3500);
}
