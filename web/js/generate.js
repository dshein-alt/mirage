import {api} from './api.js';
import {state, modelById, clearError, showError, toast} from './state.js';
import {t} from './i18n.js';

let lastJobId = null;
const MAX_REFERENCES = 8;

function fillSelect(select, values, selected, label = value => value) {
  select.replaceChildren();
  for (const value of values) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label(value);
    option.selected = value === selected;
    select.append(option);
  }
}

export function populateGenerate() {
  if (!state.config) return;
  const config = state.config.document;
  const model = document.querySelector('#model');
  const previous = model.value || config.defaults.model;
  fillSelect(model, state.models.map(item => item.id), previous, id => modelById(id)?.name || id);
  const resolution = document.querySelector('#resolution');
  fillSelect(resolution, Object.keys(config.resolutions), resolution.value || config.defaults.resolution, id => `${t(id)} · ${config.resolutions[id].width}×${config.resolutions[id].height}`);
  updateModel();
}

export function updateModel() {
  const id = document.querySelector('#model').value;
  const config = state.config?.document.models[id];
  if (!config) return;
  const quality = document.querySelector('#quality');
  const selectedQuality = Object.hasOwn(config.quality, quality.value) ? quality.value : config.default_quality;
  fillSelect(quality, Object.keys(config.quality), selectedQuality, value => t(value));
  document.querySelector('#reference-section').classList.toggle('hidden', !modelById(id)?.tasks.includes('edit'));
  updateAdvanced();
}

function updateAdvanced() {
  const id = document.querySelector('#model').value;
  const config = state.config?.document.models[id];
  if (!config) return;
  document.querySelector('#steps').placeholder = `${t('presetDefault')}: ${config.quality[document.querySelector('#quality').value]?.steps || ''}`;
  document.querySelector('#cfg').placeholder = `${t('presetDefault')}: ${config.cfg_scale}`;
}

function referenceCount() {
  return state.selectedReferences.length + (state.generatedReference ? 1 : 0);
}

export function referenceAvailable() {
  return referenceCount() < MAX_REFERENCES;
}

function removeButton(label, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'reference-remove';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  button.addEventListener('click', action);
  return button;
}

function previewItem(image, label, action) {
  const item = document.createElement('div');
  item.className = 'reference-item';
  item.append(image, removeButton(t('removeReference', {name: label}), action));
  return item;
}

function previews() {
  const root = document.querySelector('#reference-preview');
  root.replaceChildren();
  state.selectedReferences.forEach((file, index) => {
    const image = document.createElement('img');
    image.src = URL.createObjectURL(file);
    image.alt = file.name;
    image.onload = () => URL.revokeObjectURL(image.src);
    root.append(previewItem(image, file.name, () => {
      state.selectedReferences.splice(index, 1);
      previews();
    }));
  });
  if (state.generatedReference) {
    const image = document.createElement('img');
    image.src = `/api/v1/images/${state.generatedReference}/thumbnail`;
    image.alt = t('generatedImage');
    root.append(previewItem(image, t('generatedImage'), async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api.deleteInput(state.generatedReference);
        state.generatedReference = null;
        previews();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    }));
  }
  document.querySelector('#reference-count').textContent = t('referenceCount', {count: referenceCount(), maximum: MAX_REFERENCES});
}

function addReferences(event) {
  const input = event.currentTarget;
  const files = [...input.files];
  input.value = '';
  const available = MAX_REFERENCES - referenceCount();
  if (files.length > available) toast(t('referenceLimit', {maximum: MAX_REFERENCES}));
  const existing = new Set(state.selectedReferences.map(file => `${file.name}:${file.size}:${file.lastModified}`));
  for (const file of files.slice(0, Math.max(0, available))) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!existing.has(key)) {
      state.selectedReferences.push(file);
      existing.add(key);
    }
  }
  previews();
}

async function submit(event) {
  event.preventDefault();
  const button = document.querySelector('#generate-submit');
  const error = document.querySelector('#generate-error');
  clearError(error);
  button.disabled = true;
  const uploaded = [];
  try {
    const references = state.generatedReference ? [state.generatedReference] : [];
    for (const file of state.selectedReferences) {
      const record = await api.upload(file);
      uploaded.push(record.id);
      references.push(record.id);
    }
    if (references.length > MAX_REFERENCES) throw new Error(t('referenceLimit', {maximum: MAX_REFERENCES}));
    const overrides = {};
    for (const [id, key] of [['steps', 'steps'], ['cfg', 'cfg_scale'], ['seed', 'seed']]) {
      const value = document.querySelector(`#${id}`).value;
      if (value !== '') overrides[key] = Number(value);
    }
    const job = await api.generate({prompt: document.querySelector('#prompt').value, model: document.querySelector('#model').value, resolution: document.querySelector('#resolution').value, quality: document.querySelector('#quality').value, reference_ids: references, overrides});
    lastJobId = job.id;
    state.selectedReferences = [];
    state.generatedReference = null;
    previews();
    toast(t('jobQueued'));
    document.dispatchEvent(new CustomEvent('mirage:jobs-refresh'));
  } catch (exception) {
    await Promise.all(uploaded.map(id => api.deleteInput(id).catch(() => {})));
    showError(error, exception);
  } finally {
    button.disabled = false;
  }
}

export function showImage(image) {
  document.querySelector('#result-empty').classList.add('hidden');
  const content = document.querySelector('#result-content');
  content.classList.remove('hidden');
  document.querySelector('#result-image').src = `/api/v1/images/${image.id}/file`;
  document.querySelector('#result-name').textContent = image.filename;
  const download = document.querySelector('#result-download');
  download.href = `/api/v1/images/${image.id}/file`;
  download.download = image.filename;
}

export function jobChanged(job) {
  if (job.id === lastJobId && job.status === 'completed' && job.output_id) showImage({id: job.output_id, filename: job.resolved.output_name});
}

export function referenceReady() {
  document.querySelector('#model').value = 'qwen';
  updateModel();
  previews();
  toast(t('referenceSelected'));
}

export function initGenerate() {
  document.querySelector('#generate-form').addEventListener('submit', submit);
  document.querySelector('#model').addEventListener('change', updateModel);
  document.querySelector('#quality').addEventListener('change', updateAdvanced);
  document.querySelector('#references').addEventListener('change', addReferences);
  previews();
}
