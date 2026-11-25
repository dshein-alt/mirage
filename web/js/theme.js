const media = matchMedia('(prefers-color-scheme: dark)');
const modes = ['system', 'light', 'dark'];

export function themeMode() { return localStorage.getItem('sdwb.theme') || 'system'; }
function apply() {
  const mode = themeMode();
  document.documentElement.dataset.themeMode = mode;
  document.documentElement.dataset.theme = mode === 'dark' || (mode === 'system' && media.matches) ? 'dark' : 'light';
  document.querySelectorAll('#theme-setting').forEach(control => { control.value = mode; });
}
export function setTheme(mode) { if (!modes.includes(mode)) return; localStorage.setItem('sdwb.theme', mode); apply(); }
export function cycleTheme() { setTheme(modes[(modes.indexOf(themeMode()) + 1) % modes.length]); }
export function initTheme() { media.addEventListener('change', apply); apply(); }
