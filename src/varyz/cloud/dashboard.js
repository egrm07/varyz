/* Load a project only when opened; keep each request and DOM page bounded. */
(() => {
  const panels = [...document.querySelectorAll('[data-cloud-panel]')];
  const links = [...document.querySelectorAll('[data-cloud-view]')];
  const select = section => {
    const heading = document.querySelector('.cloud-dashboard-heading h1');
    if (heading) heading.textContent = {projects:'Your Cloud',connections:'Connections',account:'Account'}[section] || 'Your Cloud';
    for (const panel of panels) panel.hidden = panel.dataset.cloudPanel !== section;
    for (const link of links) {
      if (link.dataset.cloudView === section) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  };
  for (const link of links) link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    select(link.dataset.cloudView);
    history.pushState(null, '', link.href);
  });
  const dialog = document.querySelector('.cloud-project-dialog');
  let activeTrigger;
  const closeMenus = () => {
    for (const trigger of document.querySelectorAll('.cloud-project-menu-trigger')) {
      trigger.setAttribute('aria-expanded', 'false');
      trigger.closest('[data-project]').querySelector('.cloud-project-menu').hidden = true;
    }
  };
  for (const trigger of document.querySelectorAll('.cloud-project-menu-trigger')) {
    const group = trigger.closest('[data-project]');
    const menu = group.querySelector('.cloud-project-menu');
    trigger.addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation();
      const opening = menu.hidden;
      closeMenus();
      if (opening) {
        menu.hidden = false; trigger.setAttribute('aria-expanded', 'true');
        menu.querySelector('a,button').focus();
      }
    });
    menu.addEventListener('click', event => event.stopPropagation());
    for (const action of menu.querySelectorAll('[data-project-action]')) {
      action.addEventListener('click', event => {
        event.preventDefault(); closeMenus(); trigger.focus();
        const form = document.querySelector('.cloud-project-action-form');
        form.querySelector('[name=project]').value = group.dataset.project;
        form.querySelector('[name=action]').value = action.dataset.projectAction;
        form.requestSubmit();
      });
    }
    menu.querySelector('.cloud-project-delete').addEventListener('click', event => {
      event.preventDefault(); closeMenus(); activeTrigger = trigger;
      dialog.querySelector('[name=project]').value = group.dataset.project;
      dialog.querySelector('h2').textContent = 'Delete ' + (group.dataset.project === '*' ? 'General' : group.dataset.project) + '?';
      dialog.showModal(); dialog.querySelector('.cloud-delete-cancel').focus();
    });
  }
  document.addEventListener('click', closeMenus);
  document.addEventListener('focusin', event => {
    if (!event.target.closest('.cloud-project-menu,.cloud-project-menu-trigger')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const trigger = document.querySelector('.cloud-project-menu-trigger[aria-expanded=true]');
    closeMenus(); if (trigger) { event.preventDefault(); trigger.focus(); }
  });
  dialog?.querySelector('.cloud-delete-cancel').addEventListener('click', () => dialog.close());
  dialog?.addEventListener('close', () => activeTrigger?.focus());
  dialog?.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
  window.addEventListener('popstate', () => select(new URLSearchParams(location.search).get('section') || 'projects'));
  for (const group of document.querySelectorAll('[data-project][data-url]')) {
    const status = group.querySelector('.cloud-load-status');
    const content = group.querySelector('.cloud-preference-page');
    const more = group.querySelector('.cloud-load-more');
    let loaded = false, loading = false, after = 0, snapshot;
    const load = async () => {
      if (loading) return;
      loading = true; more.disabled = true;
      status.textContent = 'Loading preferences…';
      group.setAttribute('aria-busy', 'true');
      try {
        const url = new URL(group.dataset.url, location.origin);
        url.searchParams.set('after', after);
        if (snapshot !== undefined) url.searchParams.set('snapshot', snapshot);
        const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store',
          headers: {Accept: 'application/json'}, signal: AbortSignal.timeout(20000)});
        if (response.status === 401 || response.status === 403) {
          status.textContent = 'Sign in again to see this project.';
          const link = document.createElement('a'); link.href = '/login'; link.textContent = 'Sign in';
          status.append(' ', link); more.hidden = true; return;
        }
        if (!response.ok) throw new Error('unavailable');
        const value = await response.json();
        content.insertAdjacentHTML('beforeend', value.html);
        snapshot = value.snapshot;
        loaded = true;
        more.hidden = value.next_after === null;
        if (value.next_after !== null) after = value.next_after;
        status.textContent = content.children.length ? '' : 'No confirmed preferences yet.';
        more.textContent = 'Load more';
      } catch (_) {
        status.textContent = 'Could not load preferences. Your project is still saved.';
        more.textContent = 'Try again'; more.hidden = false;
      } finally {
        loading = false; more.disabled = false; group.removeAttribute('aria-busy');
      }
    };
    group.addEventListener('toggle', () => {
      if (!group.open) return;
      // Tiles: one project open at a time, shown full width below its row.
      if (group.classList.contains('cloud-project-tile')) {
        for (const other of document.querySelectorAll('.cloud-project-tile[open]')) if (other !== group) other.open = false;
        group.scrollIntoView({block: 'nearest'});
      }
      if (!loaded) load();
    });
    more.addEventListener('click', load);
  }
})();
