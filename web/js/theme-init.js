(() => {
  const saved = localStorage.getItem('mirage.theme') || 'system';
  const dark = saved === 'dark' || (saved === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeMode = saved;
})();
