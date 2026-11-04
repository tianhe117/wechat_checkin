// Keep manual check-in confirmation on the current page.
(() => {
  const dialog = document.getElementById('run-confirmation');
  const form = document.getElementById('run-confirmation-form');
  const sequence = dialog.querySelector('.confirmation-sequence');
  const keypad = dialog.querySelector('.confirmation-keypad');
  const status = dialog.querySelector('.confirmation-status');
  const cancel = dialog.querySelector('[data-confirmation-cancel]');
  const confirm = form.querySelector('button[type="submit"]');
  let challenge = null;
  let sourceForm = null;
  let picked = '';
  let submitting = false;
  let generation = 0;
  let pending = Promise.resolve();
  let cancelling = Promise.resolve();

  // Pause periodic page refresh while users are interacting with the dialog.
  const refreshSeconds = Number(document.querySelector('meta[name="page-refresh"]')?.content);
  if (refreshSeconds > 0) {
    const refresh = () => {
      if (dialog.open || document.hidden) setTimeout(refresh, 1000);
      else window.location.reload();
    };
    setTimeout(refresh, refreshSeconds * 1000);
  }

  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle('is-error', error);
  }

  async function post(data) {
    const response = await fetch(form.action, {
      method: 'POST', body: data, credentials: 'same-origin', headers: { Accept: 'application/json' }
    });
    if (response.redirected) return { redirect: response.url };
    if (!response.headers.get('Content-Type')?.includes('application/json')) {
      throw new Error('请求未完成，请刷新页面后重试。');
    }
    return response.json();
  }

  function update() {
    sequence.querySelectorAll('li').forEach((step, index) => {
      step.classList.toggle('is-complete', index < picked.length);
      step.setAttribute('aria-label', `第 ${index + 1} 个数字 ${step.textContent}${index < picked.length ? '，已完成' : ''}`);
    });
    keypad.querySelectorAll('button').forEach(button => {
      const complete = picked.includes(button.dataset.digit);
      button.classList.toggle('is-complete', complete);
      button.disabled = complete || picked.length === 6 || submitting;
    });
    form.elements.answer.value = picked;
    confirm.disabled = !challenge || picked.length !== 6 || submitting;
  }

  function showChallenge(payload) {
    challenge = payload.confirmation;
    if (!challenge) throw new Error('确认信息已失效，请重新打开。');
    picked = '';
    sequence.replaceChildren();
    keypad.replaceChildren();
    for (const digit of challenge.sequence) {
      const item = document.createElement('li');
      item.textContent = digit;
      sequence.append(item);
    }
    for (const digit of challenge.grid) {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.digit = digit;
      button.textContent = digit;
      button.setAttribute('aria-label', `数字 ${digit}`);
      keypad.append(button);
    }
    message(payload.error || '完成点选后可确认打卡', Boolean(payload.error));
    update();
  }

  async function open(source, initial = null) {
    if (dialog.open) return;
    await cancelling;
    if (dialog.open) return;
    sourceForm = source;
    const current = ++generation;
    challenge = null;
    picked = '';
    submitting = false;
    form.elements.csrf_token.value = source.elements.csrf_token.value;
    form.elements.request_id.value = source.elements.request_id.value;
    form.elements.answer.value = '';
    sequence.replaceChildren();
    keypad.replaceChildren();
    confirm.disabled = true;
    cancel.disabled = false;
    confirm.textContent = '确认打卡';
    message('正在准备确认…');
    dialog.showModal();
    try {
      const data = new FormData(source);
      pending = initial ? Promise.resolve(initial) : post(data);
      const payload = await pending;
      if (current !== generation) return;
      if (payload.redirect) { window.location.assign(payload.redirect); return; }
      showChallenge(payload);
    } catch (error) {
      if (current === generation) message(error.message || '加载失败，请取消后重试。', true);
    }
  }

  document.addEventListener('submit', event => {
    const source = event.target;
    if (source === form || !(source instanceof HTMLFormElement)) return;
    if (new URL(source.action, window.location.href).pathname !== new URL(form.action).pathname) return;
    if (source.elements.kind?.value !== 'full') return;
    event.preventDefault();
    open(source);
  });

  keypad.addEventListener('click', event => {
    const button = event.target.closest('button[data-digit]');
    if (!button || button.disabled || !challenge || submitting) return;
    if (button.dataset.digit === challenge.sequence[picked.length]) {
      picked += button.dataset.digit;
      message(picked.length === 6 ? '点选完成，请确认打卡' : `已完成 ${picked.length}/6`);
    } else {
      picked = '';
      message('顺序不正确，请从第一个数字重新点选。', true);
    }
    update();
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!challenge || picked.length !== 6 || submitting) return;
    submitting = true;
    cancel.disabled = true;
    confirm.textContent = '正在提交…';
    update();
    try {
      pending = post(new FormData(form));
      const payload = await pending;
      if (payload.redirect) { window.location.assign(payload.redirect); return; }
      submitting = false;
      cancel.disabled = false;
      confirm.textContent = '确认打卡';
      showChallenge(payload);
    } catch (error) {
      submitting = false;
      cancel.disabled = false;
      confirm.textContent = '确认打卡';
      message(error.message || '提交失败，请重试。', true);
      update();
    }
  });

  function close() {
    if (!dialog.open || submitting) return;
    ++generation;
    challenge = null;
    picked = '';
    if (sourceForm) {
      const data = new FormData(sourceForm);
      data.set('action', 'cancel');
      data.delete('answer');
      // Clear the old challenge before a rapid reopen can create the next one.
      cancelling = pending.catch(() => {}).then(() => post(data)).catch(() => {});
    }
    dialog.close();
  }

  cancel.addEventListener('click', close);
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog || submitting) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
  });

  const initial = document.getElementById('initial-run-confirmation');
  if (initial) {
    const source = document.querySelector('.confirmation-fallback form');
    open(source, JSON.parse(initial.textContent));
  }
})();
