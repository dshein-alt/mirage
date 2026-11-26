import {api} from './api.js';
import {state, element, modelById} from './state.js';
import {t, formatDate} from './i18n.js';
import {jobChanged} from './generate.js';

const cards = new Map();
let refreshVersion = 0;

function jobCard(job) {
  const card = element('article', {class: 'job-card', 'data-job-id': job.id});
  const top = element('div', {class: 'job-top'});
  const title = element('strong');
  const status = element('span');
  top.append(element('div', {}, [element('div', {class: 'job-id', text: job.id}), title]), status);
  card.append(top);
  if (job.request.prompt) card.append(element('p', {class: 'job-prompt', text: job.request.prompt}));
  const stage = element('small');
  const progress = element('progress', {max: '1'});
  const error = element('p', {class: 'job-error'});
  const cancel = element('button', {class: 'secondary'});
  const retry = element('button', {class: 'secondary'});
  cancel.addEventListener('click', () => api.cancel(job.id).then(loadJobs));
  retry.addEventListener('click', () => api.retry(job.id).then(loadJobs));
  const actions = element('div', {class: 'job-actions'}, [cancel, retry]);
  let details, summary, log, logRequest, previousStatus;

  function refreshLog() {
    if (!details?.open || logRequest) return;
    logRequest = api.logs(job.id).then(result => {
      const text = result.log || '—';
      if (text !== log.textContent) {
        const scroll = log.scrollTop;
        log.textContent = text;
        log.scrollTop = scroll;
      }
    }).catch(error => {
      if (!log.textContent) log.textContent = error.message;
    }).finally(() => { logRequest = null; });
  }

  if (job.log_path) {
    summary = element('summary');
    log = element('pre', {class: 'job-log'});
    details = element('details', {}, [summary, log]);
    details.addEventListener('toggle', refreshLog);
    actions.append(details);
  }
  card.append(stage, progress, error, actions);

  function update(job) {
    const active = ['queued', 'running'].includes(job.status);
    const [phase, detail] = job.stage.split(':', 2);
    const phaseText = phase === 'sampling' && detail ? `${t(phase)} ${detail}` : t(phase);
    title.textContent = `${modelById(job.model)?.name || job.model || job.kind} · ${job.kind === 'generation' ? `${job.resolved.width}×${job.resolved.height}` : job.kind}`;
    status.className = `job-status ${job.status}`;
    status.textContent = t(job.status);
    stage.textContent = `${formatDate(job.created_at)} · ${phaseText}`;
    progress.classList.toggle('hidden', !active);
    progress.value = job.progress;
    progress.setAttribute('aria-label', phaseText);
    error.classList.toggle('hidden', !job.error);
    error.textContent = job.error || '';
    cancel.classList.toggle('hidden', !active);
    cancel.textContent = t('cancel');
    retry.classList.toggle('hidden', !['failed', 'cancelled', 'interrupted', 'completed'].includes(job.status));
    retry.textContent = t('retry');
    if (summary) summary.textContent = t('logs');
    if (state.page === 'jobs' && (job.status === 'running' || previousStatus === 'running')) {
      if (logRequest && job.status !== 'running') logRequest.then(refreshLog);
      else refreshLog();
    }
    previousStatus = job.status;
  }
  return {card, update};
}

export async function loadJobs() {
  const version = ++refreshVersion;
  const jobs = await api.jobs();
  if (version !== refreshVersion) return;
  state.jobs = jobs;
  const list = document.querySelector('#jobs-list');
  const ids = new Set(jobs.map(job => job.id));
  for (const [id, item] of cards) {
    if (!ids.has(id)) {
      item.card.remove();
      cards.delete(id);
    }
  }
  jobs.forEach((job, index) => {
    if (!cards.has(job.id)) cards.set(job.id, jobCard(job));
    const item = cards.get(job.id);
    item.update(job);
    if (list.children[index] !== item.card) list.insertBefore(item.card, list.children[index] || null);
  });
  document.querySelector('#jobs-empty').classList.toggle('hidden', state.jobs.length > 0);
  const active = state.jobs.filter(job => ['queued', 'running'].includes(job.status)).length;
  const badge = document.querySelector('#jobs-badge'); badge.textContent = String(active); badge.classList.toggle('hidden', !active);
  state.jobs.forEach(jobChanged);
}
export function initJobs() { document.querySelector('#jobs-refresh').addEventListener('click', () => loadJobs()); }
