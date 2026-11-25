import {api} from './api.js';
import {state, element, modelById} from './state.js';
import {t, formatDate} from './i18n.js';
import {jobChanged} from './generate.js';

function jobCard(job) {
  const card = element('article', {class: 'job-card', 'data-job-id': job.id});
  const top = element('div', {class: 'job-top'});
  const title = element('div'); title.append(element('div', {class: 'job-id', text: job.id}), element('strong', {text: `${modelById(job.model)?.name || job.model || job.kind} · ${job.kind === 'generation' ? `${job.resolved.width}×${job.resolved.height}` : job.kind}`}));
  top.append(title, element('span', {class: `job-status ${job.status}`, text: t(job.status)})); card.append(top);
  if (job.request.prompt) card.append(element('p', {class: 'job-prompt', text: job.request.prompt}));
  card.append(element('small', {text: `${formatDate(job.created_at)} · ${t(job.stage.split(':')[0])}`}));
  if (['running', 'queued'].includes(job.status)) { const progress = element('progress', {max: '1', value: String(job.progress)}); card.append(progress); }
  if (job.error) card.append(element('p', {class: 'job-error', text: job.error}));
  const actions = element('div', {class: 'job-actions'});
  if (['queued', 'running'].includes(job.status)) { const button = element('button', {class: 'secondary', text: t('cancel')}); button.addEventListener('click', () => api.cancel(job.id).then(loadJobs)); actions.append(button); }
  if (['failed', 'cancelled', 'interrupted', 'completed'].includes(job.status)) { const button = element('button', {class: 'secondary', text: t('retry')}); button.addEventListener('click', () => api.retry(job.id).then(loadJobs)); actions.append(button); }
  if (job.log_path) {
    const details = element('details'); details.append(element('summary', {text: t('logs')}));
    details.addEventListener('toggle', async () => { if (details.open && !details.dataset.loaded) { const result = await api.logs(job.id); details.append(element('pre', {class: 'job-log', text: result.log || '—'})); details.dataset.loaded = '1'; } });
    actions.append(details);
  }
  card.append(actions); return card;
}
export async function loadJobs() {
  state.jobs = await api.jobs();
  const list = document.querySelector('#jobs-list'); list.replaceChildren(...state.jobs.map(jobCard));
  document.querySelector('#jobs-empty').classList.toggle('hidden', state.jobs.length > 0);
  const active = state.jobs.filter(job => ['queued', 'running'].includes(job.status)).length;
  const badge = document.querySelector('#jobs-badge'); badge.textContent = String(active); badge.classList.toggle('hidden', !active);
  state.jobs.forEach(jobChanged);
}
export function initJobs() { document.querySelector('#jobs-refresh').addEventListener('click', () => loadJobs()); }
