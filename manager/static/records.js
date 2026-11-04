// Reuse the fitted page size before navigation, then verify it before showing records.
(() => {
  const storageKey = 'records-page-size-v1';
  const recordsUrl = new URL(document.currentScript.dataset.recordsUrl, window.location.href);
  const currentUrl = new URL(window.location.href);
  const isRecordsPage = currentUrl.pathname === recordsUrl.pathname;
  let cachedSize;
  try {
    const cached = JSON.parse(localStorage.getItem(storageKey));
    if (Number.isInteger(cached?.perPage) && cached.perPage >= 1 && cached.perPage <= 50) {
      cachedSize = cached.perPage;
    }
  } catch (_) { /* Storage may be unavailable; fitting still works without a cache. */ }

  // This script runs in <head>, so the default records and page count never paint.
  if (isRecordsPage) {
    document.documentElement.classList.add('records-fitting');
    if (cachedSize && !currentUrl.searchParams.has('per_page')) {
      currentUrl.searchParams.set('per_page', cachedSize);
      window.location.replace(currentUrl);
      return;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    for (const link of document.querySelectorAll('a[href]')) {
      const url = new URL(link.href, window.location.href);
      if (cachedSize && url.origin === recordsUrl.origin && url.pathname === recordsUrl.pathname && !url.searchParams.has('per_page')) {
        url.searchParams.set('per_page', cachedSize);
        link.href = url.href;
      }
    }

    if (!isRecordsPage) return;
    const card = document.querySelector('.records-card');
    if (!card) {
      document.documentElement.classList.remove('records-fitting');
      return;
    }
    const list = card.querySelector('.records-items');
    const entry = list.querySelector('.record-entry');
    const reveal = () => {
      document.documentElement.classList.remove('records-fitting');
      card.removeAttribute('aria-busy');
    };
    if (!entry) { reveal(); return; }

    let timer;
    let navigating = false;
    const fit = () => {
      if (navigating) return;
      const entryHeight = entry.getBoundingClientRect().height;
      if (!list.clientHeight || !entryHeight) { reveal(); return; }
      const style = getComputedStyle(list);
      const columns = style.display === 'grid' ? style.gridTemplateColumns.split(/\s+/).length : 1;
      const gap = parseFloat(style.rowGap) || 0;
      const rows = Math.max(1, Math.floor((list.clientHeight + gap) / (entryHeight + gap)));
      const perPage = Math.min(50, rows * columns);
      try {
        localStorage.setItem(storageKey, JSON.stringify({perPage, width: list.clientWidth, height: list.clientHeight}));
      } catch (_) { /* A blocked cache must not prevent records from displaying. */ }
      const previousSize = Number(card.dataset.perPage);
      if (perPage === previousSize) { reveal(); return; }

      navigating = true;
      document.documentElement.classList.add('records-fitting');
      card.setAttribute('aria-busy', 'true');
      const firstIndex = (Number(card.dataset.page) - 1) * previousSize;
      const url = new URL(window.location.href);
      url.searchParams.set('per_page', perPage);
      url.searchParams.set('page', Math.floor(firstIndex / perPage) + 1);
      window.location.replace(url);
    };

    const scheduleFit = () => {
      clearTimeout(timer);
      timer = setTimeout(fit, 150);
    };
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(scheduleFit).observe(list);
    } else {
      window.addEventListener('resize', scheduleFit);
    }
    fit();
  });
})();
