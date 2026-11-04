// Keep whole records within the available space above pagination.
(() => {
  const card = document.querySelector('.records-card');
  const list = card.querySelector('.records-items');
  const entry = list.querySelector('.record-entry');
  if (!entry) return;

  let timer;
  const fit = () => {
    const style = getComputedStyle(list);
    const columns = style.display === 'grid' ? style.gridTemplateColumns.split(' ').length : 1;
    const gap = parseFloat(style.rowGap) || 0;
    const rows = Math.max(1, Math.floor((list.clientHeight + gap) / (entry.getBoundingClientRect().height + gap)));
    const perPage = Math.min(50, rows * columns);
    const previousSize = Number(card.dataset.perPage);
    if (perPage === previousSize) return;

    const firstIndex = (Number(card.dataset.page) - 1) * previousSize;
    const url = new URL(window.location.href);
    url.searchParams.set('per_page', perPage);
    url.searchParams.set('page', Math.floor(firstIndex / perPage) + 1);
    window.location.replace(url);
  };

  new ResizeObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(fit, 150);
  }).observe(list);
})();
