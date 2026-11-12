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

  // Shimmer only placeholders that have not received their first real value.
  const placeholders = [...document.querySelectorAll('[data-skeleton]')];
  for (const item of placeholders) item.dataset.skeletonInitial = item.textContent.trim();
  const clearLoadedPlaceholder = (records) => {
    for (const record of records) {
      const target = record.target.nodeType === Node.ELEMENT_NODE
        ? record.target
        : record.target.parentElement;
      const item = target?.closest?.('[data-skeleton]');
      if (item && item.textContent.trim() !== item.dataset.skeletonInitial) {
        item.removeAttribute('data-skeleton');
        item.removeAttribute('aria-busy');
      }
    }
  };
  if (placeholders.length && document.body) {
    const observer = new MutationObserver(clearLoadedPlaceholder);
    observer.observe(document.body, { childList: true, characterData: true, subtree: true });
  }
})();
