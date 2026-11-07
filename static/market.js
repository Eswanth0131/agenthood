'use strict';
const $ = id => document.getElementById(id);
const html = value => String(value ?? '').replace(/[&<>"']/g, ch =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
const cash = cents => '$' + (cents / 100).toFixed(2);
const labels = {text_analysis:'Algorithmic text analysis', model:'Live model', endpoint:'External endpoint'};
let user, listings = [], owned = [], quote, selected, editing, lastJob, busy = false, refreshing = false;

async function api(path, method = 'GET', data) {
  const response = await fetch('/api/' + path, {
    method, headers: data === undefined ? {} : {'Content-Type':'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(typeof result.detail === 'string' ? result.detail :
      result.detail?.map(x => x.msg).join(' · ') || 'Request failed');
    error.status = response.status;
    throw error;
  }
  return result;
}

function act(fn) {
  return async event => {
    event?.preventDefault();
    if (busy) return;
    busy = true;
    const button = event?.submitter || (event?.target?.tagName === 'BUTTON' ? event.target : null);
    if (button) button.disabled = true;
    $('notice').textContent = '';
    try { await fn(event); }
    catch (error) { $('notice').textContent = error.message; }
    finally {
      busy = false;
      if (button) button.disabled = false;
    }
  };
}

async function loadCatalog() {
  listings = await api('market/listings');
  const previous = $('listing').value;
  $('catalog').innerHTML = listings.map(x => `
    <div class="tile"><strong>${html(x.name)}</strong>
    <p>${html(x.capabilities)}</p>
    <small>${labels[x.tool]} · ${cash(x.price_cents)} · ${x.available_capacity}/${x.capacity} slots available
    · ${x.rating.n ? x.rating.average.toFixed(1) + '/5 (' + x.rating.n + ')' : 'No ratings'}</small>
    ${user?.role === 'buyer' && x.available_capacity ? `<br><button data-listing="${html(x.id)}" class="secondary">Select</button>` : ''}</div>`
  ).join('');
  $('listing').innerHTML = listings.filter(x => x.available_capacity).map(x =>
    `<option value="${html(x.id)}">${html(x.name)}</option>`).join('');
  if (listings.some(x => x.id === previous && x.available_capacity)) $('listing').value = previous;
}

function resetQuote() {
  quote = null;
  $('quoteBox').hidden = true;
}

async function loadNotes() {
  const notes = await api('market/memory');
  $('notes').innerHTML = notes.map(x =>
    `<div class="tile">${html(x.note)} <button data-delete="${html(x.id)}" class="secondary">Delete</button></div>`
  ).join('');
}

function edit(id) {
  const listing = owned.find(x => x.id === id);
  editing = listing?.id;
  $('listingForm').reset();
  $('editorTitle').textContent = listing ? 'Edit ' + listing.name : 'New listing';
  if (listing) {
    for (const key of ['name','capabilities','capacity','tool','endpoint']) $(key).value = listing[key];
    $('price').value = listing.price_cents;
    $('active').checked = listing.active;
  }
  $('active').disabled = !listing?.verified_at && !listing?.dispatchable;
  $('endpointFields').hidden = $('tool').value !== 'endpoint';
  $('verifyFields').hidden = !listing;
  $('credential').value = '';
}

async function loadOwned() {
  owned = await api('market/seller/listings');
  $('owned').innerHTML = owned.map(x =>
    `<div class="tile"><strong>${html(x.name)}</strong>
    <small> · ${x.dispatchable ? 'Live' : x.active ? 'Unavailable' : 'Draft'} · ${x.verified_at ? 'Probed' : 'Probe required'}
    · ${x.available_capacity}/${x.capacity} slots</small> <button data-edit="${html(x.id)}" class="secondary">Edit</button></div>`
  ).join('');
}

async function loadJobs(force = false) {
  if (!user || refreshing) return;
  refreshing = true;
  const account = user.id;
  try {
    const [jobs, ledger] = await Promise.all([api('market/jobs'), api('market/ledger')]);
    if (user?.id !== account) return;
    $('ledger').textContent = `${ledger.simulated_settled_jobs} settled simulation(s) · ${cash(ledger.simulated_settled_cents)} simulated · verified payments disabled`;
    $('jobs').innerHTML = jobs.map(job => `<div class="tile">
      <button class="secondary" data-job="${html(job.id)}">${html(job.terms.name)}</button>
      <small>${html(job.status.replaceAll('_',' '))} · ${cash(job.terms.price_cents)}</small></div>`
    ).join('') || '<p class="muted">No jobs yet.</p>';
    if (selected) {
      const id = selected;
      const summary = jobs.find(x => x.id === id);
      const version = JSON.stringify(summary);
      if (force || version !== lastJob) {
        const detail = await api('market/jobs/' + id);
        if (selected === id && user?.id === account) {
          lastJob = version;
          renderJob(detail);
        }
      }
    }
  } finally { refreshing = false; }
}

function renderJob(job) {
  const buyer = user.role === 'buyer';
  const actions = [];
  if (buyer && job.status === 'awaiting_approval') {
    actions.push(['approve','Approve draft']);
    if (!job.revised) actions.push(['revise','Request revision']);
  }
  if (buyer && job.status === 'delivered') actions.push(['accept','Accept final delivery']);
  if (buyer && ['queued','awaiting_approval','delivered'].includes(job.status)) actions.push(['cancel','Cancel job']);
  $('detail').hidden = false;
  $('detail').innerHTML = `<h2>${html(job.terms.name)}</h2>
   <p>${html(job.status.replaceAll('_',' '))} · ${html(job.phase)} · ${html(job.payment_status.replaceAll('_',' '))}</p>
   <p class="muted">Contract v${job.terms.version} · ${cash(job.terms.price_cents)} quote / ${cash(job.terms.budget_cents)} budget
   · One draft revision included</p>
   ${buyer ? `<h3>Brief</h3><pre>${html(job.terms.brief)}</pre>` : ''}
   ${job.mode ? `<p class="accent">${html(job.mode)}</p>` : ''}
   ${job.result ? `<pre>${html(job.result)}</pre>` : ''}
   ${actions.some(x => x[0] === 'revise' || x[0] === 'approve') ?
     `<label for="feedback">${job.terms.tool === 'text_analysis' ? 'Replacement source text for a revision' : 'Feedback for the next stage'}</label><textarea id="feedback" maxlength="2000"></textarea>` : ''}
   <div class="row">${actions.map(([action, title]) => `<button data-action="${action}" data-delivery="${html(job.delivery_id)}">${title}</button>`).join('')}</div>
   ${buyer && job.status === 'completed' && !job.review ? `<form id="reviewForm">
     <label for="stars">Rating</label><select id="stars">${[5,4,3,2,1].map(n => `<option>${n}</option>`).join('')}</select>
     <label for="reviewComment">Comment shared with the seller</label><textarea id="reviewComment" maxlength="800"></textarea>
     <button>Save review</button></form>` : ''}
   ${job.review ? `<p>Rating ${job.review.stars}/5 · ${html(job.review.comment)}</p>` : ''}
   ${job.deliveries?.length > 1 ? `<h3>Earlier deliveries</h3>${job.deliveries.slice(0,-1).map(d =>
     `<details><summary>${html(d.phase)} · ${html(d.mode)}</summary><pre>${html(d.result)}</pre></details>`).join('')}` : ''}
   <h3>Contract activity</h3>
   ${job.events.map(e => `<div class="tile"><strong>${html(e.event.replaceAll('_',' '))}</strong> · ${html(e.actor)}
   <br><small>${html(new Date(e.at).toLocaleString())}</small><pre>${html(JSON.stringify(e.detail, null, 2))}</pre></div>`).join('')}`;
}

async function loadAccount() {
  try { user = await api('auth/me'); }
  catch (error) { if (error.status !== 401) throw error; user = null; }
  $('authForm').hidden = !!user;
  $('signedIn').hidden = !user;
  $('buyerPanel').hidden = user?.role !== 'buyer';
  $('sellerPanel').hidden = user?.role !== 'seller';
  $('workPanel').hidden = !user;
  $('identity').textContent = user ? user.email + ' · ' + user.role : '';
  $('detail').hidden = true;
  $('detail').replaceChildren();
  $('jobs').replaceChildren();
  $('notes').replaceChildren();
  $('owned').replaceChildren();
  $('brief').value = '';
  $('note').value = '';
  selected = lastJob = null;
  resetQuote();
  edit();
  await loadCatalog();
  if (user) await Promise.all([loadJobs(), user.role === 'buyer' ? loadNotes() : loadOwned()]);
}

$('authForm').addEventListener('submit', act(async event => {
  const mode = event.submitter?.value || 'login';
  const data = {email:$('email').value, password:$('password').value};
  if (mode === 'register') data.role = $('role').value;
  await api('auth/' + mode, 'POST', data);
  $('password').value = '';
  await loadAccount();
}));
$('logout').addEventListener('click', act(async () => {
  await api('auth/logout', 'POST');
  await loadAccount();
}));
$('catalog').addEventListener('click', event => {
  if (!event.target.dataset.listing) return;
  $('listing').value = event.target.dataset.listing;
  resetQuote();
  $('brief').focus();
});
$('quoteForm').addEventListener('input', resetQuote);
$('quoteForm').addEventListener('submit', act(async () => {
  resetQuote();
  quote = await api('market/quotes', 'POST', {
    listing_id:$('listing').value, brief:$('brief').value, budget_cents:Number($('budget').value)
  });
  $('quoteSummary').textContent = `${quote.listing.name} · ${cash(quote.price_cents)} simulated · expires in 10 minutes`;
  $('processingLabel').hidden = quote.listing.tool === 'text_analysis';
  $('memoryLabel').hidden = quote.listing.tool !== 'model';
  $('processing').checked = $('useMemory').checked = false;
  $('quoteBox').hidden = false;
}));
$('hire').addEventListener('click', act(async () => {
  if (!quote) return;
  const job = await api('market/jobs', 'POST', {quote_id:quote.id,
    allow_processing:$('processing').checked, use_memory:$('useMemory').checked});
  selected = job.id;
  resetQuote();
  await Promise.all([loadJobs(true), loadCatalog()]);
}));
$('tool').addEventListener('change', () => {
  $('endpointFields').hidden = $('tool').value !== 'endpoint';
  $('active').checked = false;
});
$('owned').addEventListener('click', event => {
  if (event.target.dataset.edit) edit(event.target.dataset.edit);
});
$('newListing').addEventListener('click', () => edit());
$('listingForm').addEventListener('submit', act(async () => {
  const data = {
    name:$('name').value, capabilities:$('capabilities').value, price_cents:Number($('price').value),
    capacity:Number($('capacity').value), tool:$('tool').value, endpoint:$('endpoint').value, active:$('active').checked
  };
  const listing = await api('market/seller/listings' + (editing ? '/' + editing : ''), editing ? 'PUT' : 'POST', data);
  await Promise.all([loadOwned(), loadCatalog()]);
  edit(listing.id);
  $('notice').textContent = listing.active ? 'Listing published.' : 'Draft saved.';
}));
$('verify').addEventListener('click', act(async () => {
  const id = editing;
  const credential = $('credential').value;
  $('credential').value = '';
  await api('market/seller/listings/' + id + '/verify', 'POST', {credential});
  await loadOwned();
  edit(id);
  $('notice').textContent = 'Execution probe passed. Publish the listing when ready.';
}));
$('notesForm').addEventListener('submit', act(async () => {
  await api('market/memory', 'POST', {note:$('note').value});
  $('note').value = '';
  await loadNotes();
}));
$('notes').addEventListener('click', act(async event => {
  if (!event.target.dataset.delete) return;
  await api('market/memory/' + event.target.dataset.delete, 'DELETE');
  await loadNotes();
}));
$('jobs').addEventListener('click', act(async event => {
  if (!event.target.dataset.job) return;
  selected = event.target.dataset.job;
  await loadJobs(true);
}));
const decide = act(async event => {
  await api('market/jobs/' + selected + '/decision', 'POST', {
    action:event.target.dataset.action, delivery_id:event.target.dataset.delivery,
    feedback:$('feedback')?.value || ''
  });
  await Promise.all([loadJobs(true), loadCatalog()]);
});
$('detail').addEventListener('click', event => {
  if (event.target.dataset.action) decide(event);
});
$('detail').addEventListener('submit', act(async event => {
  if (event.target.id !== 'reviewForm') return;
  await api('market/jobs/' + selected + '/review', 'POST',
    {stars:Number($('stars').value), comment:$('reviewComment').value});
  await Promise.all([loadJobs(true), loadCatalog()]);
}));
$('refresh').addEventListener('click', act(async () => {
  await Promise.all([loadJobs(true), loadCatalog(), user.role === 'seller' ? loadOwned() : null]);
}));
setInterval(() => {
  if (user && !busy) loadJobs().catch(error => { $('notice').textContent = error.message; });
}, 2500);
act(loadAccount)();
