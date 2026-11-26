import {api} from './api.js';
import {state, clearError, showError, toast} from './state.js';
import {t, setLocale} from './i18n.js';
import {setTheme} from './theme.js';

export async function loadSettings() {
  const [config, runtime, settings, health] = await Promise.all([api.config(), api.runtime(), api.settings(), api.health()]);
  state.config = config; document.querySelector('#config-editor').value = JSON.stringify(config.document, null, 2);
  document.querySelector('#settings-editor').value = JSON.stringify(settings, null, 2);
  document.querySelector('#config-revision').textContent = config.revision.slice(0, 12);
  const details = document.querySelector('#runtime-details'); details.replaceChildren();
  for (const [key, value] of [['mirageVersion', health.version], ['active', runtime.active ? t('yes') : t('no')], ['revision', runtime.sd_cpp_commit || runtime.revision || t('unknown')], ['buildId', runtime.build_id || t('unknown')], ['runtimeImage', runtime.runtime_image?.id || t('unknown')], ['hostData', runtime.host_data_dir], ['releases', runtime.releases.join(', ')]]) { details.append(document.createElement('dt')); details.lastChild.textContent = t(key); details.append(document.createElement('dd')); details.lastChild.textContent = value; }
}
export function initSettings() {
  document.querySelector('#theme-setting').addEventListener('change', event => setTheme(event.target.value));
  document.querySelector('#language-setting').addEventListener('change', event => setLocale(event.target.value));
  document.querySelector('#settings-save').addEventListener('click', async () => {
    const error = document.querySelector('#settings-error'); clearError(error);
    try { await api.saveSettings(JSON.parse(document.querySelector('#settings-editor').value)); toast(t('settingsSaved')); } catch (exception) { showError(error, exception); }
  });
  document.querySelector('#config-save').addEventListener('click', async () => {
    const error = document.querySelector('#config-error'); clearError(error);
    try { state.config = await api.saveConfig(JSON.parse(document.querySelector('#config-editor').value)); document.querySelector('#config-revision').textContent = state.config.revision.slice(0, 12); toast(t('configSaved')); document.dispatchEvent(new CustomEvent('mirage:config')); } catch (exception) { showError(error, exception); }
  });
  document.querySelector('#config-reload').addEventListener('click', async () => { try { state.config = await api.reloadConfig(); document.querySelector('#config-editor').value = JSON.stringify(state.config.document, null, 2); toast(t('configReloaded')); document.dispatchEvent(new CustomEvent('mirage:config')); } catch (exception) { showError(document.querySelector('#config-error'), exception); } });
  document.querySelector('#config-restore').addEventListener('click', async () => { if (!confirm(t('restoreConfirm'))) return; state.config = await api.restoreConfig(); document.querySelector('#config-editor').value = JSON.stringify(state.config.document, null, 2); toast(t('configRestored')); document.dispatchEvent(new CustomEvent('mirage:config')); });
  document.querySelector('#runtime-verify').addEventListener('click', async event => { event.target.disabled = true; try { await api.verifyRuntime(); toast(t('runtimeVerified')); } catch (error) { toast(error.message); } finally { event.target.disabled = false; } });
}
