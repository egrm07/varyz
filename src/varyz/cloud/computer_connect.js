/* Local Cloud setup: connect, preview choices, explicitly approve and verify. */
(() => {
  const button = document.getElementById('connect-computer');
  const status = document.getElementById('computer-status');
  const fallback = document.getElementById('computer-fallback');
  if (!button || !status || !fallback) return;
  const endpoint = 'http://127.0.0.1:1207/cloud/computer';
  let ticket = '', approval = '', switching = false, autoResume = false, reconnectTimer;
  let availableHistory = [], availableReports = [], projectNames = [];
  let signInWindow = null, signInUrl = '';
  const closeSignIn = () => {if (signInWindow && !signInWindow.closed) signInWindow.close(); signInWindow = null; signInUrl = '';};
  const draftKey = 'varyz-project-draft:' + (button.dataset || {}).cloudAccount;
  const storageKey = 'varyz-computer:' + (button.dataset || {}).cloudAccount;
  const remember = () => {
    try {sessionStorage.setItem(storageKey, ticket);} catch (_) {}
  };
  let heartbeat;
  const keepAlive = () => {
    if (heartbeat || typeof setInterval === 'undefined') return;
    heartbeat = setInterval(() => {
      if (ticket && !switching) request('GET', ticket).catch(() => {});
    }, 60000);
  };
  const el = id => document.getElementById(id);
  const projectStep = el('computer-project-step');
  const reviewStep = el('computer-review-step');
  const automaticEntry = el('cloud-setup-content')?.dataset?.cloudEntry === 'auto';
  const showStep = (step) => {
    if (!projectStep) return;
    el('cloud-dashboard-content').hidden = true;
    el('cloud-setup-content').hidden = false;
    el('computer-progress').textContent = step === 4 ? 'Setup finished' : `Step ${step} of 3`;
    el('computer-progress').hidden = false;
    button.hidden = false;
    el('computer-intro').textContent = [
      '', 'Start by connecting Varyz on this computer.',
      'Your computer is connected. Choose what to include next.',
      'Check the preview, then approve to finish setup.',
      'Your computer is ready to keep your approved data in sync.',
    ][step];
    fallback.hidden = step !== 1;
    el('computer-more-help').hidden = true;
    el('computer-connected-label').hidden = true;
    el('connect-title').hidden = step === 4;
    el('connect-title').textContent = ['', 'Connect your computer', 'Choose projects', 'Ready to sync', ''][step];
    el('computer-intro').hidden = step === 4;
  };
  const complete = result => {
    if (heartbeat) {clearInterval(heartbeat); heartbeat = null;}
    showStep(4);
    el('computer-connect-step').hidden = true;
    projectStep.hidden = true; reviewStep.hidden = true;
    el('computer-done-step').hidden = false;
    el('computer-done-summary').textContent = result.state === 'complete_limited'
      ? 'Your computer is connected. Cloud storage is full, so remaining uploads are waiting safely on this computer. Remove unneeded projects from Cloud to make room; syncing will resume automatically.'
      : result.state === 'complete'
      ? 'Your approved data is synced, and the Cloud connection check passed. New eligible context in your selected projects will sync automatically.'
      : 'Your computer is connected and automatic sync is on. There are no preferences to check yet. They will appear here as you confirm them in your AI.';
    try {sessionStorage.removeItem(draftKey);} catch (_) {}
    el('computer-done-title').focus();
    el('computer-done-step').scrollIntoView({block: 'nearest'});
  };
  const request = async (method = 'GET', capability = '', route = '', payload) => {
    // Reads and preview generation are safe to retry. Finish is deduplicated by
    // its approval capability locally; sign-in POST is never blindly repeated.
    const attempts = method === 'GET' || route === '/finish' || route === '/preview' ? 3 : 1;
    for (let attempt = 0; attempt < attempts; attempt++) {
    let response, value;
    try {response = await fetch(endpoint + route, {
      method, credentials: 'omit', cache: 'no-store',
      targetAddressSpace: 'loopback',
      headers: capability ? {
        'X-Varyz-Ticket': capability,
        ...(method === 'POST' && !route ? {'X-Varyz-Account': button.dataset.cloudAccount} : {}),
        ...(method === 'POST' && !route && switching ? {'X-Varyz-Switch': '1'} : {}),
        ...(method === 'POST' && !route && autoResume ? {'X-Varyz-Resume': '1'} : {}),
        ...(method === 'POST' && !route && signInWindow ? {'X-Varyz-Browser': '1'} : {}),
        ...(payload ? {'Content-Type': 'application/json'} : {}),
      } : {},
      ...(payload ? {body: JSON.stringify(payload)} : {}),
      signal: AbortSignal.timeout(30000),
    }); value = await response.json();} catch (error) {
      if (attempt + 1 === attempts) {
        throw new Error('Varyz on this computer is unavailable. Your setup choices are kept. Reconnect when it is running again.');
      }
      await new Promise(resolve => setTimeout(resolve, 2000 * (attempt + 1)));
      continue;
    }
    if (!response.ok) {
      const error = new Error(value.error || 'Local connection unavailable');
      error.needsReconnect = /expired|Reconnect this computer|Finish connecting/.test(error.message);
      throw error;
    }
    return value;
    }
  };
  const chooseProjects = async (allowLegacyCompletion = false) => {
    if (!projectStep) return;
    const choices = await request('GET', ticket, '/choices');
    // Only a validated link to this browser's account can report completion.
    // Explicit project editing stays available; entering after sign-in skips it.
    const setupComplete = choices.setup_complete === true || (
      allowLegacyCompletion && choices.setup_complete === undefined && (
        choices.all_projects === true ||
        [choices.sync_selected, choices.selected, choices.history_selected, choices.report_selected]
          .some(value => Array.isArray(value) && value.length > 0)
      )
    );
    if (automaticEntry && setupComplete) {
      window.location.replace('/?view=dashboard');
      return;
    }
    const list = el('computer-projects'), all = el('computer-all-projects');
    const selectAll = el('computer-select-all');
    list.replaceChildren();
    availableHistory = (choices.history_projects || []).map(p => p.name);
    availableReports = (choices.report_projects || []).map(p => p.name);
    const projects = new Map();
    const unified = choices.sync_projects;
    const candidates = unified || [...(choices.projects || []), ...(choices.history_projects || []), ...(choices.report_projects || [])];
    for (const p of candidates) {
      projects.set(p.name, p);
    }
    if (availableHistory.includes('*') || availableReports.includes('*')) projects.set('*', {name: '*'});
    projectNames = [...projects.keys()].sort((a,b) => a === '*' ? -1 : b === '*' ? 1 :
      Number(!!projects.get(a).needs_review) - Number(!!projects.get(b).needs_review) || a.localeCompare(b));
    let selected = choices.sync_selected || [...(choices.selected || []), ...(choices.history_selected || []), ...(choices.report_selected || [])];
    if (choices.all_projects) selected = projectNames;
    try {const draft = JSON.parse(sessionStorage.getItem(draftKey)); if (Array.isArray(draft)) selected = draft;} catch (_) {}
    const changed = () => {
      approval = ''; reviewStep.hidden = true;
      const checked = [...list.querySelectorAll('input:checked')].map(i => i.value);
      const recommended = projectNames.filter(name => !projects.get(name).needs_review);
      all.checked = recommended.length > 0 && recommended.every(name => checked.includes(name));
      all.indeterminate = checked.length > 0 && !all.checked;
      if (selectAll) {
        selectAll.disabled = projectNames.length === 0;
        selectAll.textContent = projectNames.length > 0 && projectNames.every(name => checked.includes(name))
          ? 'Deselect all' : 'Select all';
      }
      try {sessionStorage.setItem(draftKey, JSON.stringify(checked));} catch (_) {}
      el('computer-selection-status').textContent = checked.length ? `${checked.length} ${checked.length === 1 ? 'project selected' : 'projects selected'}` : 'Only general preferences will sync.';
    };
    for (const name of projectNames) {
      const label = document.createElement('label'); label.className = 'cloud-project';
      const input = document.createElement('input'); input.type = 'checkbox'; input.value = name;
      input.checked = selected.includes(name);
      const text = document.createElement('span'); text.textContent = name === '*' ? 'General context' : name;
      if (name === '*') {const note = document.createElement('small'); note.textContent = 'Conversations and work outside a project'; text.append(note);}
      else {
        const info = projects.get(name);
        const note = document.createElement('small');
        note.style.overflowWrap = 'anywhere';
        note.textContent = [info.needs_review ? 'Needs review' : '', info.root || info.repository || '',
          info.reason || '', info.sessions ? `${info.sessions} conversations` : ''].filter(Boolean).join(' · ');
        if (note.textContent) text.append(note);
      }
      label.append(input, text); list.append(label);
    }
    all.disabled = !projectNames.some(name => !projects.get(name).needs_review);
    all.onchange = () => {
      for (const input of list.querySelectorAll('input')) {
        input.checked = all.checked && !projects.get(input.value).needs_review;
      }
      changed();
    };
    if (selectAll) selectAll.onclick = () => {
      const inputs = [...list.querySelectorAll('input')];
      const select = inputs.some(input => !input.checked);
      for (const input of inputs) input.checked = select;
      changed();
    };
    list.onchange = changed;
    const search = el('computer-project-search');
    if (search) {
      el('computer-search-label').hidden = projectNames.length <= 8;
      search.value = '';
      search.oninput = () => {for (const row of list.children) row.hidden = !row.textContent.toLowerCase().includes(search.value.trim().toLowerCase());};
    }
    el('computer-general').textContent = 'General preferences apply across projects and are always included.';
    changed();
    projectStep.hidden = false; reviewStep.hidden = true;
    fallback.open = false;
    el('computer-connect-step').hidden = true;
    showStep(2);
    el('computer-preview-button').disabled = false;
    el('computer-preview-button').focus({preventScroll: true});
    projectStep.scrollIntoView({block: 'start'});
  };
  if (projectStep) {
    el('computer-preview-button').addEventListener('click', async () => {
      const previewButton = el('computer-preview-button');
      previewButton.disabled = true; reviewStep.hidden = true; approval = '';
      el('computer-selection-status').textContent = 'Preparing your preview…';
      try {
        const projects = [...el('computer-projects').querySelectorAll('input:checked')].map(i => i.value);
        const preview = await request('POST', ticket, '/preview', {selection: 'projects', projects,
          all_projects: false, history_projects: projects.filter(p => availableHistory.includes(p)),
          report_projects: projects.filter(p => availableReports.includes(p))});
        approval = preview.approval;
        const uploads = preview.uploads + (preview.history_uploads || 0) + (preview.report_uploads || 0);
        const deletions = preview.deletions + (preview.history_deletions || 0) + (preview.report_deletions || 0);
        el('computer-upload-summary').textContent = uploads
          ? `${uploads} ${uploads === 1 ? 'item will' : 'items will'} sync. This includes general preferences and context from your selected projects.`
          : 'Everything is up to date. Finishing will save your project choices.';
        el('computer-deletion-summary').textContent = deletions
          ? `${deletions} ${deletions === 1 ? 'item will' : 'items will'} be removed from Cloud. Your local records will stay on this computer.`
          : 'Nothing will be removed.';
        const details = el('computer-preview-details');
        if (details) {
          const parts = [`${preview.uploads} confirmed preferences`, `${preview.history_uploads || 0} history records`, `${preview.report_uploads || 0} work reports`];
          const blocked = (preview.history_blocked || 0) + (preview.report_blocked || 0);
          details.textContent = parts.join(' · ') + (blocked ? `. ${blocked} records will stay local because of privacy or size checks.` : '.');
        }
        el('computer-selection-status').textContent = 'Your preview is ready. Approve below to sync these choices.';
        el('computer-finish-status').textContent = '';
        el('computer-finish-button').disabled = false;
        reviewStep.hidden = false; projectStep.hidden = true;
        showStep(3);
        el('computer-finish-button').focus({preventScroll: true});
        reviewStep.scrollIntoView({block: 'start'});
      } catch (error) {
        if (error.needsReconnect) retry(error.message);
        el('computer-selection-status').textContent = error.message;
      } finally {previewButton.disabled = false;}
    });
    el('computer-back-button').addEventListener('click', () => {
      approval = ''; reviewStep.hidden = true; projectStep.hidden = false;
      showStep(2);
      el('computer-preview-button').focus({preventScroll: true});
      projectStep.scrollIntoView({block: 'start'});
    });
    el('computer-finish-button').addEventListener('click', async () => {
      if (!approval) return;
      const finishButton = el('computer-finish-button'), back = el('computer-back-button');
      finishButton.disabled = true; back.disabled = true;
      const approved = approval; approval = '';
      el('computer-finish-status').textContent = 'Syncing your preferences and checking the connection…';
      try {
        await request('POST', ticket, '/finish', {approve: true, approval: approved});
        const deadline = Date.now() + 660000;
        while (Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          const result = await request('GET', ticket);
          if (['complete', 'complete_empty', 'complete_limited'].includes(result.state)) {
            complete(result);
            return;
          }
          if (result.state !== 'syncing') {
            if (result.state === 'sync_failed' && result.reason === 'storage_limit') {
              throw new Error('Cloud storage is full. Review your selected projects before trying again. Your local records are safe.');
            }
            const messages = {
              stale: 'Your preferences changed. Review an updated preview before approving.',
              conflict: 'A preference changed on another device. Ask your local AI to review the conflict; no version was chosen for you.',
              verification_failed: 'Your preferences synced, but the connection check did not finish. Review and try again.',
              interrupted: 'Varyz restarted during setup. Your computer is still connected. Review a fresh preview before continuing.',
            };
            throw new Error(messages[result.state] || 'Sync did not finish. Review the preview and try again.');
          }
        }
        throw new Error('Sync is taking longer than expected. Review again to check progress.');
      } catch (error) {
        if (error.needsReconnect) retry(error.message);
        el('computer-finish-status').textContent = error.message;
        back.textContent = 'Review again';
      } finally {back.disabled = false;}
    });
  }
  const retry = (message, {help = true, action = 'Try connecting again', switchAccount = false} = {}) => {
    closeSignIn();
    switching = switchAccount;
    if (projectStep) {
      projectStep.hidden = true; reviewStep.hidden = true;
      el('computer-connect-step').hidden = false;
      el('computer-connected-label').hidden = true;
      el('computer-done-step').hidden = true;
    }
    status.textContent = message;
    showStep(1);
    button.disabled = false;
    button.textContent = action;
    fallback.open = help;
    if (!switchAccount && action === 'Try connecting again' && /unavailable|unreachable|Could not reach|Could not complete|expired/.test(message)) scheduleReconnect();
  };
  // The local bridge reports a fixed reason code, never the underlying error text.
  const failed = reason => {
    if (reason === 'other_account') {
      retry('This computer is linked to a different Varyz Cloud account. Switch it to this account to continue. Your preferences stay on this computer, and the other account keeps its Cloud data.',
        {help: false, action: 'Switch this computer to this account', switchAccount: true});
      return;
    }
    const messages = {
      unavailable: ['Cloud is temporarily unavailable. Your connection is saved; retrying automatically…', false],
      sign_in: ['Connect this computer once. After that, Varyz reconnects automatically.', false],
      interrupted: ['Restoring your connection…', false],
      wrong_sign_in: ['You signed in to a different Varyz Cloud account than this page. Sign in with this account and try again.', false],
      inactive: ['Cloud access is not active on this account yet. Try again once your payment is confirmed.', false],
      busy: ['Varyz on this computer is still finishing an earlier sign-in or sync. Wait a moment, then try again.', false],
      declined: ['Sign-in was cancelled. Try again when you are ready.', false],
      timeout: ['Sign-in timed out. Try connecting again.', false],
    };
    const [message, help] = messages[reason]
      || ['Sign-in did not complete. Try again, or use the setup instructions below.', true];
    retry(message, {help, action: reason === 'sign_in' ? 'Connect this computer' : 'Try connecting again'});
    if (['unavailable', 'busy', 'interrupted'].includes(reason)) scheduleReconnect();
  };
  const waitForConnection = async capability => {
    const deadline = Date.now() + 660000;
    while (Date.now() < deadline) {
      const result = await request('GET', capability);
      if (['complete', 'complete_empty', 'complete_limited'].includes(result.state)) {
        let manage = false;
        try {manage = new URLSearchParams(window.location.search).get('view') === 'setup' && !sessionStorage.getItem(draftKey);} catch (_) {}
        if (automaticEntry && projectStep) {window.location.replace('/?view=dashboard'); return;}
        if (manage && projectStep) {keepAlive(); await chooseProjects(); return;}
        complete(result); return;
      }
      if (result.state === 'connecting' && result.authorization_url && signInWindow && !signInWindow.closed) {
        const url = new URL(result.authorization_url);
        if (url.origin === location.origin && url.pathname === '/oauth/authorize' && signInUrl !== url.href) {signInUrl = url.href; signInWindow.location.href = url.href;}
      }
      if (result.state === 'connected') {
        closeSignIn();
        keepAlive();
        button.textContent = 'Computer connected';
        status.textContent = 'This computer is connected. Choose projects; upload approval comes next.';
        await chooseProjects(true);
        return;
      }
      if (['preview_ready', 'interrupted', 'stale', 'sync_failed', 'conflict', 'verification_failed'].includes(result.state)) {
        keepAlive();
        await chooseProjects();
        el('computer-selection-status').textContent = 'Your computer is still connected. Review a fresh preview to continue.';
        return;
      }
      if (result.state === 'syncing') {
        keepAlive();
        status.textContent = 'Your approved sync is still running. Checking progress…';
        await new Promise(resolve => setTimeout(resolve, 2000));
        continue;
      }
      if (result.state !== 'connecting') {
        failed(result.reason);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    retry('Sign-in timed out. Try connecting again.');
  };
  if (typeof window !== 'undefined' && window.location.hash.startsWith('#computer=')) {
    const resumed = window.location.hash.slice('#computer='.length);
    window.history.replaceState(null, '', window.location.pathname + window.location.search + '#connect-varyz');
    if (/^[A-Za-z0-9_-]{43}$/.test(resumed)) {
      ticket = resumed;
      remember();
      showStep(1);
      button.disabled = true;
      status.textContent = 'Finishing your computer connection…';
      waitForConnection(ticket).catch(() => retry('Could not resume the connection. Try connecting again.'));
    }
  } else if (typeof window === 'undefined' || new URLSearchParams(window.location.search).get('view') !== 'dashboard') {
    try {ticket = sessionStorage.getItem(storageKey) || '';} catch (_) {}
    if (/^[A-Za-z0-9_-]{43}$/.test(ticket)) {
      button.disabled = true;
      status.textContent = 'Restoring your computer connection…';
      waitForConnection(ticket).catch(error => retry(error.message));
    }
  }
  const connect = async (automatic = false) => {
    autoResume = automatic;
    if (reconnectTimer) {clearTimeout(reconnectTimer); reconnectTimer = null;}
    button.disabled = true;
    status.textContent = 'Looking for Varyz on this computer…';
    let detected;
    try {
      detected = await request();
      if (detected.installed !== true) throw new Error('Unavailable');
    } catch (_) {
      retry('Could not reach local Varyz. Start Varyz if it is not running. This page will reconnect automatically.', {help: !automatic});
      return;
    }
    if (typeof detected.ticket === 'string') {ticket = detected.ticket; remember();}
    if (detected.busy) {
      retry('A sign-in is already in progress. Finish it in your browser, then check the connection again.', {switchAccount: switching});
      if (automatic) scheduleReconnect();
      return;
    }
    if (automatic && detected.browser_oauth !== true) {
      retry('Connect this computer once to continue. Update local Varyz for automatic reconnection.',
        {help: false, action: 'Connect this computer'});
      return;
    }
    if (detected.browser_oauth !== true) closeSignIn();
    if (typeof detected.ticket !== 'string') {
      retry('Local Varyz needs an update. Use the setup instructions below.');
      return;
    }
    try {
      const consent = await fetch('/computer/connect', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({csrf: button.dataset.csrf}),
        signal: AbortSignal.timeout(30000),
      });
      if (!consent.ok) {
        retry('Your account page expired. Refresh it and try connecting again.');
        return;
      }
      await request('POST', detected.ticket);
      button.textContent = switching ? 'Switching account' : 'Sign-in in progress';
      status.textContent = automatic ? 'Restoring your saved connection…' : switching
        ? 'Switching this computer to this account. Finish sign-in in the browser window that opened.'
        : 'Varyz is installed. Finish sign-in in the browser window that opened.';
      await waitForConnection(detected.ticket);
    } catch (_) {
      retry('Could not complete the connection. Your choices are saved. Retrying automatically…', {help: false});
    }
  };
  const scheduleReconnect = () => {
    if (reconnectTimer || switching || !projectStep || typeof window === 'undefined') return;
    reconnectTimer = setTimeout(() => {reconnectTimer = null; connect(true);}, 15000);
  };
  button.addEventListener('click', () => {
    if (typeof window !== 'undefined' && typeof window.open === 'function') {
      signInWindow = window.open('about:blank', 'varyz-cloud-sign-in');
      if (signInWindow) {signInWindow.document.title = 'Connecting Varyz'; signInWindow.document.body.textContent = 'Connecting Varyz…';}
    }
    return connect(false);
  });
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') window.addEventListener('online', () => {if (el('cloud-setup-content') && !el('cloud-setup-content').hidden && !button.disabled) connect(true);});
  if (automaticEntry && el('cloud-setup-content') && !el('cloud-setup-content').hidden) {
    el('computer-progress').hidden = true;
    el('connect-title').textContent = 'Checking this computer';
    status.textContent = 'Checking your saved Cloud connection…';
    button.hidden = true;
  }
  if (!ticket && el('cloud-setup-content') && !el('cloud-setup-content').hidden) connect(true);
})();
