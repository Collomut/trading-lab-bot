(() => {
  const storageKey = 'trading-lab-theme';
  const root = document.documentElement;
  const buttons = [...document.querySelectorAll('[data-theme-toggle]')];
  let savedTheme = 'light';
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === 'dark' || stored === 'light') savedTheme = stored;
  } catch (_) {}

  const applyTheme = (theme) => {
    root.dataset.theme = theme;
    const nextTheme = theme === 'dark' ? 'light' : 'dark';
    for (const button of buttons) {
      button.setAttribute('aria-label', `Switch to ${nextTheme} mode`);
      const label = button.querySelector('[data-theme-label]');
      if (label) label.textContent = `${nextTheme[0].toUpperCase()}${nextTheme.slice(1)} mode`;
    }
  };

  applyTheme(savedTheme);
  for (const button of buttons) {
    button.addEventListener('click', () => {
      const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      applyTheme(theme);
      try { localStorage.setItem(storageKey, theme); } catch (_) {}
    });
  }
})();
