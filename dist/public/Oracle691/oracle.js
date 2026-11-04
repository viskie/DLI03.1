'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
let state = {}, mode = 'login', selectedPlan = null;
let selections = {}, resumePurchase = false;
try { selections = JSON.parse(sessionStorage.getItem('oracleSelections') || '{}'); } catch { selections = {}; }
if(!selections || typeof selections !== 'object' || Array.isArray(selections)) selections={};
const icons = {investment:'◈', prepaid_plus:'⇄'};
const fallbackPlans = {"investment": {"name": "Investment-linked subscription tokens", "number": 1, "min_amount": 30000, "max_amount": 1000000, "description": "Proposed investment rights linked to subscription tokens.", "features": ["Subscription tokens with defined investment rights", "Potential profit-sharing, appreciation or redemption rights", "Rights subject to definitive offering terms"], "notice": "May constitute securities or other regulated investment products. No return or redemption is promised by this preview."}, "prepaid_plus": {"name": "Tradable prepaid subscriptions Plus", "number": 2, "min_amount": 500000, "max_amount": 5000000, "description": "Future API access and resale of unused rights, plus Plan 1 investment-linked benefits.", "features": ["Prepaid rights to future API access", "Proposed resale of unused service rights", "Includes Plan 1 investment-linked benefits, subject to terms"], "notice": "The API component derives value from services. Adding Plan 1 investment rights means this combined plan is not a service-only offering."}};
const planOrder = ['investment','prepaid_plus'];
function amountFor(id) {
  const p = (state.plans || fallbackPlans)[id];
  const value = selections[id];
  return Number.isSafeInteger(value) && value >= p.min_amount && value <= p.max_amount && value % 100 === 0 ? value : p.min_amount;
}
function saveSelection(id, amount) {
  selections[id] = amount;
  sessionStorage.setItem('oracleSelections', JSON.stringify(selections));
}
const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value/100);
async function api(path, data) {
  const response = await fetch(path, {method:data ? 'POST':'GET',credentials:'same-origin',headers:data ? {'Content-Type':'application/json','X-CSRF-Token':state.csrf || ''}:{},body:data ? JSON.stringify(data):undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Something went wrong. Please try again.');
  return result;
}
function message(text) { $('#global-message').textContent = text; $('#global-message').classList.remove('hidden'); }
function authMode(next) {
  mode = next;
  const register = next === 'register';
  $('#auth-title').textContent = register ? 'Find your edge.' : 'Welcome back.';
  $('#auth-subtitle').textContent = register ? 'Create your Oracle 961 account.' : 'Sign in to manage your signal access.';
  $('#auth-identifier-label').textContent=register?'Email address':'Email or admin username';
  $('#auth-form [name=email]').type=register?'email':'text';
  $('#auth-form [name=email]').autocomplete=register?'email':'username';
  $('#name-field').classList.toggle('hidden', !register);
  $('#name-field input').required = register;
  $('#password-hint').classList.toggle('hidden', !register);
  $('#auth-form [name=password]').minLength = register ? 12 : 1;
  $('#auth-form [name=password]').autocomplete = register ? 'new-password':'current-password';
  $('#auth-form button[type=submit]').textContent = register ? 'Create account →':'Sign in →';
  $('#login-tab').classList.toggle('selected', !register);
  $('#register-tab').classList.toggle('selected', register);
  $('#auth-error').textContent = '';
}
function openAuth() { authMode('login'); $('#auth-dialog').showModal(); }
async function refresh() {
  state = await api('/api/session');
  $('#account-button').textContent = state.user ? `${state.user.name.split(' ')[0]} · Account`:'Sign in ↗';
  $('#session-label').textContent = state.user ? `Welcome, ${state.user.name.split(' ')[0]}`:'Your signal. Your edge.';
  $$('.oauth-button').forEach(button=>{
    const enabled=Boolean(state.oauth_providers?.[button.dataset.provider]);
    button.setAttribute('aria-disabled',String(!enabled));
    button.querySelector('.provider-status').textContent=enabled?'':'Setup pending';
  });
  $('#oauth-note').textContent='Sign in or register with Google, Facebook or LinkedIn. A first provider login creates an account.';
  $('#checkout-note').textContent = Object.values(state.payment_pages || {}).some(Boolean) ? 'Pay on a secure Razorpay Payment Page. Confirm the selected USD amount there; use your portal email on Razorpay. Signed confirmation updates My Holdings automatically.' : state.checkout_enabled ? `${state.payment_mode === 'test' ? 'TEST CHECKOUT · No real payment. ' : ''}Selected amounts are validated on the server. Final offering terms apply.`:'Choose an amount and register. Razorpay checkout will be available after credentials and offering eligibility are confirmed.';
  renderPlans(); renderHoldings();
}
function renderPlans() {
  const plans = state.plans || fallbackPlans;
  $('#plan-grid').innerHTML = planOrder.map(id => {
    const p = plans[id], amount = amountFor(id);
    return `<article class="plan ${id === 'investment'?'popular':''}" data-plan="${id}"><div class="plan-head"><i class="plan-icon">${icons[id]}</i><div><p class="eyebrow">PLAN ${p.number}</p><h3>${escapeHTML(p.name)}</h3></div></div><p class="description">${escapeHTML(p.description)}</p><div class="price" id="amount-display-${id}">${money(amount)}</div><p class="per-unit">Choose your starting amount · USD</p><label class="slider-label" for="slider-${id}">Payment scale<input id="slider-${id}" type="range" min="${p.min_amount/100}" max="${p.max_amount/100}" step="1" value="${amount/100}" data-slider="${id}"></label><div class="range-labels"><span>${money(p.min_amount)}</span><span>${money(p.max_amount)}</span></div><label class="amount-label" for="number-${id}">Exact amount (USD)<input id="number-${id}" type="number" min="${p.min_amount/100}" max="${p.max_amount/100}" step="1" value="${amount/100}" data-number="${id}"></label><ul>${p.features.map(f=>`<li>${escapeHTML(f)}</li>`).join('')}</ul><p class="plan-notice">${escapeHTML(p.notice)}</p><button class="primary" data-buy="${id}">${state.user?'Review selected amount':'Register & continue'} <span>↗</span></button></article>`;
  }).join('');
  $$('[data-slider]').forEach(input => input.addEventListener('input',()=>{
    const id=input.dataset.slider, amount=Number(input.value)*100;
    saveSelection(id,amount); $(`#number-${id}`).value=input.value; $(`#amount-display-${id}`).textContent=money(amount);
  }));
  $$('[data-number]').forEach(input=>input.addEventListener('input',()=>{
    if(!input.validity.valid || input.value==='') return;
    const id=input.dataset.number, amount=Number(input.value)*100;
    saveSelection(id,amount); $(`#slider-${id}`).value=input.value; $(`#amount-display-${id}`).textContent=money(amount);
  }));
  $$('[data-buy]').forEach(button=>button.addEventListener('click',()=>{
    const id=button.dataset.buy, input=$(`#number-${id}`);
    if(!input.reportValidity()) return;
    selectedPlan=id; saveSelection(id,Number(input.value)*100);
    sessionStorage.setItem('oraclePendingPlan',id);
    api('/api/analytics/event',{event:`plan.${id}`}).catch(()=>{});
    if(!state.user){api('/api/analytics/event',{event:'registration.opened'}).catch(()=>{});resumePurchase=true;authMode('register');$('#auth-dialog').showModal();}
    else openPurchase(id);
  }));
}
function renderHoldings() {
  const target = $('#holdings-content');
  if (!state.user) {
    target.innerHTML = '<div class="empty-state"><i>◇</i><h3>Your intelligence starts here.</h3><p>Sign in to view your purchase receipts and account.</p><button class="primary" id="holdings-signin">Sign in →</button></div>';
    $('#holdings-signin').onclick = openAuth;
  } else if (!state.holdings.length) {
    target.innerHTML = '<div class="empty-state"><i>▤</i><h3>A clean slate. A new perspective.</h3><p>You have no confirmed purchases yet. Explore the available plans to get started.</p><button class="primary" id="holdings-explore">Explore plans →</button></div>';
    $('#holdings-explore').onclick = () => tab('contracts');
  } else {
    target.innerHTML = state.holdings.map(receipt => `<article class="receipt"><div><h3>${escapeHTML(receipt.plan_name || fallbackPlans[receipt.plan]?.name || receipt.plan)}</h3><p>${escapeHTML(receipt.status)}</p><small>Payment ID: ${escapeHTML(receipt.payment_id)}</small></div><div><strong>${money(receipt.amount)}</strong><p>${escapeHTML(new Date(receipt.created*1000).toLocaleDateString())}</p><small>${receipt.environment === 'test' ? 'Test receipt · no live access' : receipt.source === 'payment_page' ? 'Verified hosted payment' : 'Payment receipt · Allocation pending'}</small></div></article>`).join('');
  }
}
function tab(name) {
  $$('[data-tab]').forEach(button => button.setAttribute('aria-selected',String(button.dataset.tab === name)));
  $('#overview-panel').classList.toggle('hidden',name !== 'overview');
  $('#contracts').classList.toggle('hidden', !['overview','contracts'].includes(name));
  $('#marketplace-panel').classList.toggle('hidden', !['overview','marketplace'].includes(name));
  $('#main-grid').classList.toggle('hidden', !['overview','contracts','marketplace'].includes(name));
  $('#main-grid').classList.toggle('single-view',name !== 'overview');
  $('#bottom-grid').classList.toggle('hidden',name !== 'overview');
  $('#holdings-panel').classList.toggle('hidden',name !== 'holdings');
  $('#faq-panel').classList.toggle('hidden',name !== 'faq');
}
function openPurchase(id) {
  sessionStorage.removeItem('oraclePendingPlan');
  selectedPlan=id;
  const plan = (state.plans || fallbackPlans)[id];
  $('#purchase-title').textContent = plan.name;
  $('#purchase-description').textContent = plan.description;
  $('#purchase-price').textContent = money(amountFor(id));
  const hosted = Boolean(state.payment_pages?.[id]);
  $('#purchase-status').textContent = hosted ? 'Your selected USD amount will be prefilled on Razorpay. Confirm it before paying. Use your portal email and keep the prefilled Portal Reference. Signed confirmation updates your account automatically.' : state.checkout_enabled ? (state.payment_mode === 'test'?'Test mode: no real payment will be collected.':'Sign in to securely purchase service access.'):'Your amount is saved. Razorpay checkout is awaiting credentials and offering eligibility confirmation.';
  $('#purchase-notice').textContent = plan.notice;
  $('#confirm-purchase').classList.remove('hidden');
  $('#confirm-purchase').textContent = !state.user ? 'Create account & continue →' : hosted ? 'Open Razorpay Payment Page →' : state.checkout_enabled ? 'Continue to Razorpay →' : 'Razorpay setup pending';
  $('#confirm-purchase').disabled=Boolean(state.user && !hosted && !state.checkout_enabled);
  $('#request-plan').href=`mailto:hello@delta-indices.info?subject=${encodeURIComponent(`Oracle 961 · ${plan.name} plan access`)}`;
  $('#purchase-dialog').showModal();
}
async function loadRazorpay() {
  if (window.Razorpay) return;
  await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://checkout.razorpay.com/v1/checkout.js';script.onload=resolve;script.onerror=()=>reject(new Error('Razorpay could not load. Check your connection.'));document.head.appendChild(script);});
}
$('#confirm-purchase').onclick = async () => {
  if (!state.user) { resumePurchase=true; $('#purchase-dialog').close(); authMode('register'); $('#auth-dialog').showModal(); return; }
  const button=$('#confirm-purchase'); button.disabled=true;
  try {
    if (state.payment_pages?.[selectedPlan]) {
      const result = await api('/api/payment-page', {plan:selectedPlan,amount:amountFor(selectedPlan)});
      location.assign(result.url);
      return;
    }
    await loadRazorpay();
    api('/api/analytics/event',{event:'checkout.started'}).catch(()=>{});
    const order=await api('/api/checkout',{action:'order',plan:selectedPlan,amount:amountFor(selectedPlan)});
    const checkout=new window.Razorpay({key:order.key,order_id:order.order_id,amount:order.amount,currency:order.currency,name:'Delta Lateral Indices',description:`Oracle 961 · ${order.name}`,prefill:{name:state.user.name,email:state.user.email},theme:{color:'#287eff'},modal:{ondismiss:()=>{button.disabled=false;$('#purchase-status').textContent='Checkout closed. If you completed payment, refresh My Holdings or contact support before retrying.';}},handler:async payment=>{
      try { const result=await api('/api/checkout',{action:'verify',...payment}); $('#purchase-dialog').close(); await refresh();tab('holdings');message(result.message); }
      catch(error){$('#purchase-status').textContent=`${error.message} Payment ID: ${payment.razorpay_payment_id}`;}
    }});
    checkout.on('payment.failed',()=>{$('#purchase-status').textContent='Payment was not completed. Check your payment status before trying again.';button.disabled=false;});
    checkout.open();
  } catch(error) { $('#purchase-status').textContent=error.message;button.disabled=false; }
};
$('#account-button').onclick=()=>{
  if(state.user?.role==='admin'){location.assign('/Oracle691/admin');return;}
  if (!state.user) return openAuth();
  $('#account-form [name=name]').value=state.user.name;
  $('#account-form [name=email]').value=state.user.email;
  $('#account-form [name=current_password]').value='';
  $('#account-form [name=new_password]').value='';
  $('#password-settings').classList.toggle('hidden',state.user.provider !== 'password');
  $('#account-error').textContent='';$('#account-dialog').showModal();
};
$('#auth-form').onsubmit=async event=>{
  event.preventDefault(); const button=event.submitter;button.disabled=true;
  try { const data=Object.fromEntries(new FormData(event.target));const result=await api(`/api/${mode === 'register'?'register':'login'}`,data);if(result.redirect){location.assign(result.redirect);return;}await refresh();$('#auth-dialog').close();event.target.reset();message(`Signed in as ${state.user.name}.`); if(resumePurchase && selectedPlan){resumePurchase=false;openPurchase(selectedPlan);} }
  catch(error){$('#auth-error').textContent=error.message;}finally{button.disabled=false;}
};
$('#account-form').onsubmit=async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{await api('/api/account',Object.fromEntries(new FormData(event.target)));await refresh();$('#account-dialog').close();message('Account updated.');}
  catch(error){$('#account-error').textContent=error.message;}finally{button.disabled=false;}
};
$('#logout').onclick=async()=>{try{await api('/api/logout',{});await refresh();$('#account-dialog').close();message('You have signed out.');}catch(error){$('#account-error').textContent=error.message;}};
$('#refresh-holdings').onclick=()=>refresh().catch(error=>message(error.message));
$('#login-tab').onclick=()=>authMode('login');$('#register-tab').onclick=()=>authMode('register');
$$('dialog .close').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$$('[data-tab]').forEach(button=>button.onclick=()=>tab(button.dataset.tab));
$$('[role=tab]').forEach((button,index,buttons)=>button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const next=buttons[(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length];next.focus();tab(next.dataset.tab);}}));
$('#disclaimer-button').onclick=()=>{tab('faq');$('#faq-panel').scrollIntoView({behavior:'smooth'});};
$('a[href="#contracts"]').onclick=()=>tab('contracts');
// Fixed illustrative entries: these do not represent executed trades.
const demoActivity = [
  ['Plan 1 · Investment-linked', 2, '$600.00', '2 min ago'],
  ['Plan 2 · Prepaid Plus', 1, '$5,000.00', '8 min ago'],
  ['Plan 1 · Investment-linked', 5, '$1,500.00', '14 min ago'],
  ['Plan 2 · Prepaid Plus', 2, '$10,000.00', '27 min ago'],
  ['Plan 1 · Investment-linked', 10, '$3,000.00', '41 min ago'],
  ['Plan 2 · Prepaid Plus', 3, '$15,000.00', '1 hour ago'],
];
$('#activity-rows').innerHTML = demoActivity.map(row => `<tr>${row.map(value => `<td>${escapeHTML(String(value))}</td>`).join('')}</tr>`).join('');
renderPlans();
refresh().then(()=>{if(state.analytics_client)api('/api/analytics/event',{event:'pageview'}).catch(()=>{});const pending=sessionStorage.getItem('oraclePendingPlan');if(state.user && planOrder.includes(pending)){sessionStorage.removeItem('oraclePendingPlan');openPurchase(pending);}}).catch(()=>{message('Account service unavailable. Please reload the local Python preview.');$('#checkout-note').textContent='Account and checkout services are unavailable.';});
const authResult=new URLSearchParams(location.search).get('auth');
if(authResult){message(authResult==='existing'?'An account already uses this email. Use its original sign-in method.':authResult==='unavailable'?'This sign-in provider is awaiting its app credentials.':'Provider sign-in could not be completed. Check email permission and try again.');history.replaceState({},'',location.pathname);}

const outcomeView={rows:[],shown:0,timer:null,visible:false};
const compactPrice=value=>{const number=Number(value);if(!Number.isFinite(number))return '—';return new Intl.NumberFormat('en-US',{maximumFractionDigits:number<1?8:number<100?5:2}).format(number);};
const compactWindow=seconds=>{const value=Number(seconds)||0;return value>=3600?`${Math.round(value/360)/10}h`:value>=60?`${Math.round(value/6)/10}m`:`${value}s`;};
function outcomeRow(row){
  const correct=Number(row.directionCorrect)===1,move=Number(row.actualMovePercent),verified=Number(row.verifiedAtMs||row.targetAtMs);
  return `<tr class="outcome-enter"><td><span class="market-glyph">◇</span>${escapeHTML(row.market||'—')}</td><td>${escapeHTML(compactWindow(row.horizonSeconds))}</td><td>${escapeHTML(compactPrice(row.predictedPrice))}</td><td>${escapeHTML(compactPrice(row.actualPrice))}</td><td class="${move>=0?'positive':'negative'}">${Number.isFinite(move)?`${move>=0?'+':''}${move.toFixed(3)}%`:'—'}</td><td><span class="direction ${correct?'correct':'miss'}">${correct?'✓ MATCH':'↯ MISS'}</span></td><td>${verified?escapeHTML(new Date(verified).toLocaleString([],{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'})):'—'}</td></tr>`;
}
function revealOutcome(){
  if(!outcomeView.visible||outcomeView.shown>=outcomeView.rows.length)return;
  $('#outcome-rows').insertAdjacentHTML('beforeend',outcomeRow(outcomeView.rows[outcomeView.shown++]));
  const latest=$('#outcome-rows').lastElementChild;latest.classList.add('outcome-latest');$('.outcome-scroll').scrollTo({top:$('.outcome-scroll').scrollHeight,behavior:'smooth'});$('#outcome-live-label').textContent='LIVE SHADOW · NEW ACTUALIZED OUTCOME';setTimeout(()=>latest.classList.remove('outcome-latest'),1500);
}
function scheduleOutcomes(){clearInterval(outcomeView.timer);if(outcomeView.visible&&outcomeView.shown<outcomeView.rows.length)outcomeView.timer=setInterval(revealOutcome,2000);}
async function loadOutcomes(){
  try{
    const feed=await api('/api/oracle961/outcomes');outcomeView.rows=Array.isArray(feed.rows)?feed.rows:[];outcomeView.shown=Math.min(20,outcomeView.rows.length);
    $('#outcome-rows').innerHTML=outcomeView.rows.slice(0,outcomeView.shown).map(outcomeRow).join('')||'<tr class="outcome-placeholder"><td colspan="7">No actualized predictions are published yet.</td></tr>';
    $('#outcome-round').textContent=feed.generatedAtMs?`Round ${new Date(feed.generatedAtMs).toLocaleString()}`:'Hourly round';
    $('#outcome-live-label').textContent=outcomeView.rows.length?'LIVE SHADOW · VERIFIED OUTCOMES':'WAITING FOR ACTUALIZED OUTCOMES';scheduleOutcomes();
  }catch(error){$('#outcome-rows').innerHTML='<tr class="outcome-placeholder"><td colspan="7">The verified outcome feed is between hourly publications.</td></tr>';$('#outcome-live-label').textContent='HOURLY FEED TEMPORARILY UNAVAILABLE';}
}
const outcomeStage=$('#prediction-reality');
if(outcomeStage){
  new IntersectionObserver(entries=>{outcomeView.visible=entries[0].isIntersecting;scheduleOutcomes();},{threshold:.18}).observe(outcomeStage);
  let parallaxFrame=0;addEventListener('scroll',()=>{if(parallaxFrame)return;parallaxFrame=requestAnimationFrame(()=>{parallaxFrame=0;const box=outcomeStage.getBoundingClientRect(),offset=Math.max(-28,Math.min(28,(innerHeight/2-(box.top+box.height/2))*.045));outcomeStage.style.setProperty('--outcome-parallax',`${offset}px`);});},{passive:true});
  loadOutcomes();
}

$$('.oauth-button').forEach(button=>button.addEventListener('click',event=>{
  if(button.getAttribute('aria-disabled')==='true'){
    event.preventDefault();$('#auth-error').textContent='This provider is awaiting app credentials. Email registration and sign-in are available.';
  }
}));

if(new URLSearchParams(location.search).get('admin')==='signin') openAuth();

window.addEventListener('focus',()=>refresh().catch(()=>{}));
