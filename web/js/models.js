import {api} from './api.js';
import {state, element, toast} from './state.js';
import {t, formatBytes} from './i18n.js';

function modelCard(model) {
  const card = element('article', {class: 'model-card'});
  const top = element('div', {class: 'model-top'}); const title = element('div');
  title.append(element('div', {class: 'model-name', text: model.name}), element('div', {class: 'model-subtitle', text: model.tasks.map(task => t(task)).join(' · ')}));
  top.append(title, element('span', {class: `job-status ${model.installed ? 'completed' : ''}`, text: t(model.installed ? 'installed' : 'notInstalled')})); card.append(top);
  const components = element('ul', {class: 'model-components'});
  for (const component of model.components) {
    const item = element('li'); item.append(element('span', {text: component.local_path}), element('span', {class: component.valid ? 'component-valid' : 'component-missing', text: `${formatBytes(component.bytes)} · ${t(component.required ? 'required' : 'optional')} · ${component.valid ? '✓' : '—'}`})); components.append(item);
  }
  card.append(components, element('div', {class: 'model-size', text: `${t('downloadSize')}: ${formatBytes(model.bytes_total)} · ${model.editing_installed ? t('editingReady') : t('editingOptional')}`}));
  const actions = element('div', {class: 'model-actions'});
  if (!model.installed || !model.editing_installed) { const button = element('button', {class: 'primary', text: t(model.installed ? 'installEditing' : 'install')}); button.addEventListener('click', async () => { button.disabled = true; try { await api.install(model.id, !model.editing_installed); toast(t('jobQueued')); document.dispatchEvent(new CustomEvent('sdwb:jobs-refresh')); } catch (error) { toast(error.message); } finally { button.disabled = false; } }); actions.append(button); }
  if (model.installed) {
    const verify = element('button', {class: 'secondary', text: t('verify')}); verify.addEventListener('click', () => api.verifyModel(model.id).then(() => toast(t('verifyStarted'))).catch(error => toast(error.message)));
    const remove = element('button', {class: 'danger-secondary', text: t('remove')}); remove.hidden = state.auth.role !== 'admin'; remove.addEventListener('click', async () => { if (!confirm(t('removeConfirm'))) return; try { await api.removeModel(model.id); await loadModels(); } catch (error) { toast(error.message); } }); actions.append(verify, remove);
  }
  card.append(actions); return card;
}
export async function loadModels() { state.models = await api.models(); document.querySelector('#models-list').replaceChildren(...state.models.map(modelCard)); }
export function initModels() { document.querySelector('#models-refresh').addEventListener('click', () => loadModels()); }
