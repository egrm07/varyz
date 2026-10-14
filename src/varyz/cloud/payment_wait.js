/* After checkout: poll the subscription, then recognize the existing computer setup. */
(() => {
  const status = document.getElementById('payment-wait-status');
  if (!status) return;
  const started = Date.now();
  const elapsed = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)} min ${seconds % 60}s`;
  };
  const check = async () => {
    let next = Date.now() - started < 120000 ? 3000 : 15000;
    try {
      const response = await fetch('/account/billing-status', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (response.status === 401) {
        window.location.replace('/login');
        return;
      }
      const body = await response.json();
      if (body.active) {
        status.textContent = 'Payment confirmed. Opening your Cloud…';
        window.location.replace('/');
        return;
      }
      status.textContent = Date.now() - started < 300000
        ? `Still waiting for confirmation (${elapsed()}). This page checks again by itself.`
        : `Still waiting after ${elapsed()}. Your purchase is kept: leave this page open `
          + 'or sign in again later, and your account opens once confirmation arrives.';
    } catch {
      status.textContent = 'Could not reach Varyz Cloud. Trying again shortly.';
      next = 10000;
    }
    window.setTimeout(check, next);
  };
  window.setTimeout(check, 2000);
})();
