import {api} from './api.js';
import {state, modelById, clearError, showError, toast} from './state.js';
import {t} from './i18n.js';

let lastJobId = null;
function fillSelect(select, values, selected, label = value => value) {
  select.replaceChildren();
  for (const value of values) {
    const option = document.createElement('option'); option.value = value; option.textContent = label(value); option.selected = value === selected; select.append(option);
  }
}
export function populateGenerate() {
  if (!state.config) return;
  const config = state.config.document;
  const model = document.querySelector('#model');
  const previous = model.value || config.defaults.model;
  fillSelect(model, state.models.map(item => item.id), previous, id => modelById(id)?.name || id);
  const resolution = document.querySelector('#resolution');
  fillSelect(resolution, Object.keys(config.resolutions), resolution.value || config.defaults.resolution, id => `${t(id)} · ${config.resolutions[id].width}×${config.resolutions[id].height}${id === 'small' ? ` (${t('experimental')})` : ''}`);
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
function previews() {
  const root = document.querySelector('#reference-preview'); root.replaceChildren();
  for (const file of document.querySelector('#references').files) {
    const image = document.createElement('img'); image.src = URL.createObjectURL(file); image.alt = file.name; image.onload = () => URL.revokeObjectURL(image.src); root.append(image);
  }
  if (state.generatedReference) {
    const image = document.createElement('img'); image.src = `/api/v1/images/${state.generatedReference}/thumbnail`; image.alt = t('generatedImage'); root.append(image);
  }
}
async function submit(event) {
  event.preventDefault();
  const button = document.querySelector('#generate-submit'); const error = document.querySelector('#generate-error');
  clearError(error); button.disabled = true;
  try {
    const references = state.generatedReference ? [state.generatedReference] : [];
    for (const file of document.querySelector('#references').files) references.push((await api.upload(file)).id);
    const overrides = {};
    for (const [id, key] of [['steps', 'steps'], ['cfg', 'cfg_scale'], ['seed', 'seed']]) {
      const value = document.querySelector(`#${id}`).value;
      if (value !== '') overrides[key] = Number(value);
    }
    const job = await api.generate({prompt: document.querySelector('#prompt').value, model: document.querySelector('#model').value, resolution: document.querySelector('#resolution').value, quality: document.querySelector('#quality').value, reference_ids: references, overrides});
    lastJobId = job.id; toast(t('jobQueued')); document.dispatchEvent(new CustomEvent('sdwb:jobs-refresh'));
  } catch (exception) { showError(error, exception); }
  finally { button.disabled = false; }
}
export function showImage(image) {
  document.querySelector('#result-empty').classList.add('hidden');
  const content = document.querySelector('#result-content'); content.classList.remove('hidden');
  document.querySelector('#result-image').src = `/api/v1/images/${image.id}/file`;
  document.querySelector('#result-name').textContent = image.filename;
  const download = document.querySelector('#result-download'); download.href = `/api/v1/images/${image.id}/file`; download.download = image.filename;
}
export function jobChanged(job) {
  if (job.id === lastJobId && job.status === 'completed' && job.output_id) showImage({id: job.output_id, filename: job.resolved.output_name});
}
export function referenceReady() {
  document.querySelector('#model').value = 'qwen'; updateModel(); previews(); toast(t('referenceSelected'));
}
export function initGenerate() {
  document.querySelector('#generate-form').addEventListener('submit', submit);
  document.querySelector('#model').addEventListener('change', updateModel);
  document.querySelector('#quality').addEventListener('change', updateAdvanced);
  document.querySelector('#references').addEventListener('change', previews);
}
