/* Form behavior only; scheduling and validation remain on the server. */
(() => {
  document.querySelectorAll('.weekday-window').forEach(block => {
    const toggle = block.querySelector('[data-weekday-custom]');
    const update = () => {
      block.querySelectorAll('input[type="time"]').forEach(input => {
        input.disabled = !toggle.checked;
        input.required = toggle.checked;
        if (!toggle.checked) {
          input.value = document.querySelector(`[name="${block.dataset.slot}_${input.dataset.bound}"]`).value;
        }
      });
    };
    toggle.addEventListener('change', update);
    ['start', 'end'].forEach(bound => {
      document.querySelector(`[name="${block.dataset.slot}_${bound}"]`).addEventListener('input', update);
    });
    update();
  });
  const form = document.querySelector('[data-day-editor="single"]');
  if (!form) return;
  const update = () => {
    const custom = form.querySelector('[name="mode"]:checked').value === 'work';
    form.querySelectorAll('.day-custom-fields input').forEach(input => {
      input.disabled = !custom;
      input.required = custom && input.type === 'time';
    });
  };
  form.querySelectorAll('[name="mode"]').forEach(input => input.addEventListener('change', update));
  const loadDate = () => {
    const input = form.querySelector('[name="day"]');
    if (!input.reportValidity() || !input.value) return;
    const url = new URL(window.location.href);
    url.search = '';
    url.searchParams.set('day', input.value);
    window.location.assign(url.href);
  };
  form.querySelector('[name="day"]').addEventListener('change', loadDate);
  form.querySelector('[data-load-date]').addEventListener('click', loadDate);
  update();
})();
