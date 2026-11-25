import {api} from './api.js';
import {state, element, modelById, toast} from './state.js';
import {t} from './i18n.js';
import {referenceReady, referenceAvailable} from './generate.js';

let selected = null;
let deferredImages = null;
let lastFingerprint = '';
let loadSequence = 0;
const dialogTriggers = new Map();

function galleryQuery() {
  const query = new URLSearchParams();
  for (const [id, key] of [['gallery-search', 'search'], ['gallery-model', 'model'], ['gallery-resolution', 'resolution'], ['gallery-quality', 'quality'], ['gallery-width', 'width'], ['gallery-height', 'height']]) {
    const control = document.querySelector(`#${id}`);
    const value = control?.value;
    if (value) query.set(key, value);
  }
  return query;
}

function openDialog(dialog, trigger) {
  dialogTriggers.set(dialog, trigger);
  if (!dialog.open) dialog.showModal();
}

function openViewer(image, trigger) {
  const viewer = document.querySelector('#viewer-dialog');
  const full = document.querySelector('#viewer-image');
  full.src = `/api/v1/images/${image.id}/file`;
  full.alt = image.prompt;
  openDialog(viewer, trigger);
}

function openImage(image, trigger) {
  selected = image;
  document.querySelector('#dialog-image').src = `/api/v1/images/${image.id}/file`;
  document.querySelector('#dialog-image').alt = image.prompt;
  document.querySelector('#dialog-title').textContent = image.filename;
  document.querySelector('#dialog-prompt').textContent = image.prompt;
  document.querySelector('#dialog-metadata').textContent = JSON.stringify(image.metadata, null, 2);
  const download = document.querySelector('#dialog-download');
  download.href = `/api/v1/images/${image.id}/file`;
  download.download = image.filename;
  openDialog(document.querySelector('#image-dialog'), trigger);
}

function renderGallery(images) {
  const active = document.activeElement;
  const focus = active?.dataset.imageId ? {id: active.dataset.imageId, action: active.dataset.action} : null;
  state.images = images;
  lastFingerprint = JSON.stringify(images);
  const grid = document.querySelector('#gallery-grid');
  grid.replaceChildren();
  document.querySelector('#gallery-empty').classList.toggle('hidden', images.length > 0);
  for (const image of images) {
    const card = element('article', {class: 'image-card'});
    const preview = element('div', {class: 'image-card-preview'});
    preview.append(element('img', {src: `/api/v1/images/${image.id}/thumbnail`, alt: image.prompt, loading: 'lazy'}));
    const fullscreen = element('button', {
      class: 'card-action fullscreen-action', type: 'button', text: '⛶',
      'aria-label': t('fullscreenImage'), title: t('fullscreenImage'),
      'data-image-id': image.id, 'data-action': 'fullscreen',
    });
    fullscreen.addEventListener('click', () => openViewer(image, fullscreen));
    preview.append(fullscreen);
    const metadata = element('button', {
      class: 'image-card-info image-metadata-button', type: 'button',
      'aria-label': `${t('imageMetadata')}: ${image.prompt}`,
      'data-image-id': image.id, 'data-action': 'metadata',
    });
    metadata.append(
      element('div', {class: 'image-card-prompt', text: image.prompt}),
      element('div', {class: 'image-card-meta', text: `${modelById(image.model)?.name || image.model} · ${image.width}×${image.height} · ${t(image.quality)}`}),
    );
    metadata.addEventListener('click', () => openImage(image, metadata));
    card.append(preview, metadata);
    grid.append(card);
  }
  if (focus) {
    const replacement = grid.querySelector(`[data-image-id="${CSS.escape(focus.id)}"][data-action="${CSS.escape(focus.action)}"]`);
    (replacement || document.querySelector('#gallery-refresh')).focus();
  }
}

export async function loadGallery() {
  const sequence = ++loadSequence;
  const [images, dimensions] = await Promise.all([api.images(`?${galleryQuery()}`), api.imageDimensions()]);
  if (sequence !== loadSequence) return state.images;
  if (JSON.stringify(dimensions) !== JSON.stringify(state.dimensions)) {
    state.dimensions = dimensions;
    populateGalleryFilters();
  }
  const fingerprint = JSON.stringify(images);
  if (fingerprint === lastFingerprint) return state.images;
  if (document.querySelector('#viewer-dialog').open || document.querySelector('#image-dialog').open) {
    deferredImages = images;
    return state.images;
  }
  renderGallery(images);
  return images;
}

function originalDimensions(image) {
  return new Promise((resolve, reject) => {
    const probe = new Image();
    probe.onload = () => resolve({width: probe.naturalWidth, height: probe.naturalHeight});
    probe.onerror = reject;
    probe.src = `/api/v1/images/${image.id}/file`;
  });
}

async function reuseSettings() {
  if (!selected) return;
  const image = selected;
  let dimensions;
  try {
    dimensions = await originalDimensions(image);
    if (!dimensions.width || !dimensions.height) throw new Error('invalid dimensions');
  } catch {
    toast(t('reuseImageError'));
    return;
  }
  const resolutions = state.config?.document.resolutions || {};
  const match = Object.entries(resolutions).find(([, size]) => size.width === dimensions.width && size.height === dimensions.height);
  const resolution = match?.[0] || (Object.hasOwn(resolutions, 'hd') ? 'hd' : state.config?.document.defaults.resolution);
  if (!resolution) {
    toast(t('reuseImageError'));
    return;
  }
  document.querySelector('#prompt').value = image.prompt;
  document.querySelector('#model').value = image.model;
  document.querySelector('#model').dispatchEvent(new Event('change'));
  document.querySelector('#resolution').value = resolution;
  document.querySelector('#quality').value = image.quality;
  document.querySelector('#quality').dispatchEvent(new Event('change'));
  if (!match) toast(t('unsupportedReuseSize', {size: `${dimensions.width}×${dimensions.height}`, preset: resolutions[resolution].width + '×' + resolutions[resolution].height}));
  document.querySelector('#image-dialog').close();
  document.querySelector('[data-page="generate"]').click();
}

function dialogClosed(dialog) {
  const trigger = dialogTriggers.get(dialog);
  dialogTriggers.delete(dialog);
  if (trigger?.isConnected) trigger.focus();
  else document.querySelector('#gallery-refresh').focus();
  if (dialog.id === 'image-dialog') selected = null;
  if (deferredImages) {
    const pending = deferredImages;
    deferredImages = null;
    renderGallery(pending);
  }
}

export function populateGalleryFilters() {
  const model = document.querySelector('#gallery-model');
  const current = model.value;
  model.replaceChildren(element('option', {value: '', 'data-i18n': 'allModels', text: t('allModels')}));
  for (const item of state.models) model.append(element('option', {value: item.id, text: item.name}));
  model.value = current;
  const quality = document.querySelector('#gallery-quality');
  const currentQuality = quality.value;
  quality.replaceChildren(element('option', {value: '', text: t('allQualities')}));
  for (const item of ['fast', 'medium', 'high']) quality.append(element('option', {value: item, text: t(item)}));
  quality.value = currentQuality;
  for (const [id, dimension, allKey] of [['gallery-width', 'width', 'allWidths'], ['gallery-height', 'height', 'allHeights']]) {
    const select = document.querySelector(`#${id}`);
    const value = select.value;
    const values = [...new Set(state.dimensions.map(size => size[dimension]))];
    select.replaceChildren(element('option', {value: '', text: t(allKey)}));
    for (const item of values) select.append(element('option', {value: String(item), text: String(item)}));
    select.value = value;
  }
}

export function initGallery() {
  for (const id of ['gallery-search', 'gallery-model', 'gallery-resolution', 'gallery-quality', 'gallery-width', 'gallery-height']) {
    const control = document.querySelector(`#${id}`);
    control?.addEventListener(id === 'gallery-search' ? 'input' : 'change', () => loadGallery().catch(toast));
  }
  document.querySelector('#gallery-refresh').addEventListener('click', () => loadGallery().catch(toast));
  for (const id of ['viewer-dialog', 'image-dialog']) {
    const dialog = document.querySelector(`#${id}`);
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => dialogClosed(dialog));
  }
  document.querySelector('#viewer-close').addEventListener('click', () => document.querySelector('#viewer-dialog').close());
  document.querySelector('#dialog-close').addEventListener('click', () => document.querySelector('#image-dialog').close());
  document.querySelector('#dialog-reuse').addEventListener('click', () => reuseSettings().catch(error => toast(error.message)));
  document.querySelector('#dialog-reference').addEventListener('click', async () => {
    if (!selected) return;
    if (!referenceAvailable()) {
      toast(t('referenceLimit', {maximum: 8}));
      return;
    }
    const image = selected;
    const response = await fetch(`/api/v1/images/${image.id}/file`);
    if (!response.ok) throw new Error(t('reuseImageError'));
    const blob = await response.blob();
    const uploaded = await api.upload(new File([blob], image.filename, {type: 'image/png'}));
    state.generatedReference = uploaded.id;
    document.querySelector('#image-dialog').close();
    document.querySelector('[data-page="generate"]').click();
    referenceReady();
  });
  document.querySelector('#dialog-delete').addEventListener('click', async () => {
    if (!selected || !confirm(t('deleteConfirm'))) return;
    await api.trash(selected.id);
    document.querySelector('#image-dialog').close();
    await loadGallery();
  });
}
