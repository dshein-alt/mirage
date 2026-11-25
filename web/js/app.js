import {api} from './api.js';
import {state, toast, showError, clearError} from './state.js';
import {initI18n, t, setLocale} from './i18n.js';
import {initTheme, cycleTheme, themeMode} from './theme.js';
import {initGenerate, populateGenerate} from './generate.js';
import {initGallery, loadGallery, populateGalleryFilters} from './gallery.js';
import {initJobs, loadJobs} from './jobs.js';
import {initModels, loadModels} from './models.js';
import {initSettings, loadSettings} from './settings.js';

let started = false;
function navigate(page) {
  if (!['generate', 'gallery', 'jobs', 'models', 'settings'].includes(page)) page = 'generate';
  state.page = page; history.replaceState(null, '', `#${page}`);
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('active', section.id === `page-${page}`));
  document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.page === page));
  document.title = `${t(page)} · SD Workbench`;
  if (page === 'gallery') loadGallery().catch(error => toast(error.message));
  if (page === 'jobs') loadJobs().catch(error => toast(error.message));
  if (page === 'models') loadModels().catch(error => toast(error.message));
  if (page === 'settings') loadSettings().then(populateGenerate).catch(error => toast(error.message));
}
function applyAuth(auth) {
  state.auth = auth;
  document.querySelector('#logout').classList.toggle('hidden', !auth.auth_required);
  document.querySelectorAll('[data-admin]').forEach(element => { element.hidden = auth.role !== 'admin'; });
}
function showLogin() {
  const dialog = document.querySelector('#auth-dialog');
  if (!dialog.open) dialog.showModal();
  document.querySelector('#auth-token').focus();
}
async function startApplication() {
  if (started) return;
  started = true;
  try {
    const [models, config] = await Promise.all([api.models(), api.config()]); state.models = models; state.config = config;
    populateGenerate(); populateGalleryFilters(); await loadJobs();
    document.querySelector('#service-status').textContent = t('ready'); document.querySelector('#service-status').classList.add('ready');
  } catch (error) { document.querySelector('#service-status').textContent = t('offline'); toast(error.message); }
  navigate(location.hash.slice(1) || 'generate');
  const events = new EventSource('/api/v1/events');
  events.addEventListener('job', () => { loadJobs().catch(() => {}); if (state.page === 'models') loadModels().catch(() => {}); });
  events.onerror = () => { events.close(); setInterval(() => { loadJobs().catch(() => {}); if (state.page === 'models') loadModels().catch(() => {}); }, 5000); };
  setInterval(() => { if (state.page === 'gallery' && state.jobs.some(job => job.status === 'completed')) loadGallery().catch(() => {}); }, 5000);
}
async function initialize() {
  initTheme(); await initI18n();
  document.querySelector('#theme-quick').value = themeMode();
  initGenerate(); initGallery(); initJobs(); initModels(); initSettings();
  document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => navigate(button.dataset.page)));
  document.querySelector('#theme-quick').addEventListener('click', cycleTheme);
  document.querySelector('#language-quick').addEventListener('change', event => setLocale(event.target.value));
  document.addEventListener('sdwb:jobs-refresh', () => loadJobs().catch(() => {}));
  document.addEventListener('sdwb:config', () => { populateGenerate(); populateGalleryFilters(); });
  document.addEventListener('sdwb:locale', () => { populateGenerate(); populateGalleryFilters(); if (state.page === 'gallery') loadGallery(); if (state.page === 'jobs') loadJobs(); if (state.page === 'models') loadModels(); navigate(state.page); });
  document.addEventListener('sdwb:unauthorized', showLogin);
  document.querySelector('#auth-dialog').addEventListener('cancel', event => event.preventDefault());
  document.querySelector('#auth-form').addEventListener('submit', async event => {
    event.preventDefault();
    const error = document.querySelector('#auth-error'); clearError(error);
    try {
      const auth = await api.login(document.querySelector('#auth-token').value);
      applyAuth({...auth, auth_required: true});
      document.querySelector('#auth-token').value = '';
      document.querySelector('#auth-dialog').close();
      await startApplication();
    } catch (exception) { showError(error, exception); }
  });
  document.querySelector('#logout').addEventListener('click', async () => { await api.logout(); location.reload(); });
  try {
    const auth = await api.session(); applyAuth(auth);
    await startApplication();
  } catch {
    applyAuth({role: 'user', auth_required: true});
    showLogin();
  }
}
initialize();
