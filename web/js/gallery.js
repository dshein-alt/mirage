import {api} from './api.js';
import {state, element, modelById, toast} from './state.js';
import {t, formatDate} from './i18n.js';
import {referenceReady} from './generate.js';

let selected = null;
export async function loadGallery() {
  const query = new URLSearchParams();
  for (const [id, key] of [['gallery-search', 'search'], ['gallery-model', 'model'], ['gallery-resolution', 'resolution'], ['gallery-quality', 'quality']]) { const value = document.querySelector(`#${id}`).value; if (value) query.set(key, value); }
  state.images = await api.images(`?${query}`);
  const grid = document.querySelector('#gallery-grid'); grid.replaceChildren();
  document.querySelector('#gallery-empty').classList.toggle('hidden', state.images.length > 0);
  for (const image of state.images) {
    const card = element('button', {class: 'image-card', type: 'button', 'aria-label': image.prompt});
    card.append(element('img', {src: `/api/v1/images/${image.id}/thumbnail`, alt: image.prompt, loading: 'lazy'}));
    const info = element('div', {class: 'image-card-info'});
    info.append(element('div', {class: 'image-card-prompt', text: image.prompt}), element('div', {class: 'image-card-meta', text: `${modelById(image.model)?.name || image.model} · ${image.width}×${image.height} · ${t(image.quality)}`}));
    card.append(info); card.addEventListener('click', () => openImage(image)); grid.append(card);
  }
}
function openImage(image) {
  selected = image;
  document.querySelector('#dialog-image').src = `/api/v1/images/${image.id}/file`;
  document.querySelector('#dialog-image').alt = image.prompt;
  document.querySelector('#dialog-title').textContent = image.filename;
  document.querySelector('#dialog-prompt').textContent = image.prompt;
  document.querySelector('#dialog-metadata').textContent = JSON.stringify(image.metadata, null, 2);
  const download = document.querySelector('#dialog-download'); download.href = `/api/v1/images/${image.id}/file`; download.download = image.filename;
  document.querySelector('#image-dialog').showModal();
}
export function populateGalleryFilters() {
  const model = document.querySelector('#gallery-model'); const current = model.value;
  model.replaceChildren(element('option', {value: '', 'data-i18n': 'allModels', text: t('allModels')}));
  for (const item of state.models) model.append(element('option', {value: item.id, text: item.name}));
  model.value = current;
  for (const [id, values, allKey] of [['gallery-resolution', Object.keys(state.config?.document.resolutions || {}), 'allSizes'], ['gallery-quality', ['fast', 'medium', 'high'], 'allQualities']]) {
    const select = document.querySelector(`#${id}`); const value = select.value; select.replaceChildren(element('option', {value: '', text: t(allKey)}));
    for (const item of values) select.append(element('option', {value: item, text: t(item)})); select.value = value;
  }
}
export function initGallery() {
  for (const id of ['gallery-search', 'gallery-model', 'gallery-resolution', 'gallery-quality']) document.querySelector(`#${id}`).addEventListener(id === 'gallery-search' ? 'input' : 'change', () => loadGallery().catch(toast));
  document.querySelector('#gallery-refresh').addEventListener('click', () => loadGallery().catch(toast));
  document.querySelector('#dialog-close').addEventListener('click', () => document.querySelector('#image-dialog').close());
  document.querySelector('#image-dialog').addEventListener('click', event => { if (event.target.id === 'image-dialog') event.target.close(); });
  document.querySelector('#dialog-reuse').addEventListener('click', () => {
    if (!selected) return;
    document.querySelector('#prompt').value = selected.prompt;
    document.querySelector('#model').value = selected.model;
    document.querySelector('#model').dispatchEvent(new Event('change'));
    document.querySelector('#resolution').value = selected.resolution;
    document.querySelector('#quality').value = selected.quality;
    document.querySelector('#quality').dispatchEvent(new Event('change'));
    document.querySelector('#image-dialog').close();
    document.querySelector('[data-page="generate"]').click();
  });
  document.querySelector('#dialog-reference').addEventListener('click', async () => {
    if (!selected) return;
    const blob = await fetch(`/api/v1/images/${selected.id}/file`).then(response => response.blob());
    const uploaded = await api.upload(new File([blob], selected.filename, {type: 'image/png'}));
    state.generatedReference = uploaded.id; document.querySelector('#image-dialog').close();
    document.querySelector('[data-page="generate"]').click(); referenceReady();
  });
  document.querySelector('#dialog-delete').addEventListener('click', async () => {
    if (!selected || !confirm(t('deleteConfirm'))) return;
    await api.trash(selected.id); document.querySelector('#image-dialog').close(); await loadGallery();
  });
}
