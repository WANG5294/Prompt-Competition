const $ = id => document.getElementById(id);
let state = null, current = 1, offset = 0, busy = false, polling = false;
const drafts = {};
function getDraft(id) { try { return localStorage.getItem('prompt-arena-draft-'+id) || ''; } catch { return drafts[id] || ''; } }
function saveDraft(id, value) { drafts[id] = value; try { localStorage.setItem('prompt-arena-draft-'+id, value); } catch {} }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 5500); }
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json', 'X-Contest-Request':'1'}, body:JSON.stringify(data)};
  const response = await fetch('/api/'+path, options);
  const result = await response.json();
  if (!response.ok) { if (response.status === 401 && path !== 'login') showLogin(); throw new Error(result.error || '请求失败'); }
  return result;
}
function showLogin() { state = null; $('loginView').hidden = false; $('arenaView').hidden = true; if (typeof guardSync === 'function') guardSync(); }
async function refresh(force = false) {
  if (polling) return;
  polling = true;
  try {
    const next = await api('state');
    offset = next.serverTime * 1000 - Date.now();
    const changed = !state || JSON.stringify(next.submissions) !== JSON.stringify(state.submissions) || next.endsAt !== state.endsAt;
    if (state && state.baseEndsAt === next.baseEndsAt && state.penaltySeconds > next.penaltySeconds) {
      next.endsAt = state.endsAt; next.penaltySeconds = state.penaltySeconds; next.focusCount = state.focusCount;
    }
    state = next; $('loginView').hidden = true; $('arenaView').hidden = false;
    if (force || changed) render();
    tick();
  } finally { polling = false; }
}
function active() { return state && state.endsAt && state.endsAt * 1000 > Date.now() + offset; }
function tick() {
  if (typeof guardSync === 'function') guardSync();
  if (!state) return;
  const seconds = state.endsAt ? Math.max(0, Math.ceil((state.endsAt*1000 - Date.now()-offset)/1000)) : 3600;
  $('timer').textContent = String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
  const finished = state.submissions.filter(s => s.status === 'done').length === state.questions.length;
  $('timerLabel').textContent = finished ? '挑战已完成' : !state.endsAt ? '开始后计时' : seconds ? '剩余作答时间' : '作答时间已结束';
  const sub = state.submissions.find(s => s.question === current);
  $('prompt').disabled = !active() || !!sub;
  $('submit').disabled = !active() || !!sub || busy || (typeof guardPending === 'function' && guardPending()) || !$('prompt').value.trim();
  $('submitHint').textContent = sub ? '答案已锁定，刷新页面不会丢失提交。' : !state.endsAt ? '请先点击上方“开始挑战”。' : !seconds ? '考试已结束，已提交答案仍会继续判分。' : '每题仅一次正式提交，确认后不可修改。';
}
function render() {
  if (!state) return;
  $('lobby').hidden = !!state.endsAt;
  const done = state.submissions.filter(s => s.status === 'done');
  $('maxScore').textContent = ` / ${state.questions.length*5}`;
  $('totalScore').textContent = done.reduce((sum,s) => sum+s.score,0);
  $('progressText').textContent = `已判分 ${done.length} / ${state.questions.length} 题`;
  $('progressBar').style.width = `${done.length/state.questions.length*100}%`;
  $('questionNav').replaceChildren();
  for (const q of state.questions) {
    const sub = state.submissions.find(s => s.question === q.id);
    const status = !sub ? '待作答' : sub.status === 'done' ? '已判分' : sub.status === 'error' ? '待重试' : '判分中';
    const button = document.createElement('button'); button.className = 'nav-item'+(q.id === current ? ' active' : '');
    button.innerHTML = `<span class="nav-number">${String(q.id).padStart(2,'0')}</span><span class="nav-copy"><b></b><small>${status}</small></span><span class="nav-status">${sub?.status === 'done' ? sub.score+' / 5' : '↗'}</span>`;
    button.querySelector('b').textContent = q.title;
    button.onclick = () => { current = q.id; render(); };
    $('questionNav').append(button);
  }
  const nav = $('questionNav'), selected = nav.querySelector('.active');
  if (selected) {
    selected.setAttribute('aria-current', 'step');
    nav.scrollTop += selected.getBoundingClientRect().top - nav.getBoundingClientRect().top - (nav.clientHeight-selected.offsetHeight)/2;
  }
  const q = state.questions.find(q => q.id === current);
  const sub = state.submissions.find(s => s.question === current);
  $('questionTag').textContent = `CHALLENGE ${String(q.id).padStart(2,'0')} / ${q.category}`;
  $('questionTitle').textContent = q.title; $('questionHint').textContent = q.hint; $('target').textContent = q.target;
  $('blocked').replaceChildren();
  for (const word of q.blocked) { const el = document.createElement('span'); el.className = 'blocked-chip'; el.textContent = word; $('blocked').append(el); }
  $('prompt').value = sub ? sub.prompt : getDraft(current);
  $('charCount').textContent = `${Array.from($('prompt').value).length} / 2000`;
  $('submit').textContent = sub ? '已提交 ✓' : '确认提交 ↗';
  $('previous').disabled = current === 1; $('next').disabled = current === state.questions.length; $('pageNumber').textContent = `${String(current).padStart(2,'0')} / ${state.questions.length}`;
  const result = $('result'); result.replaceChildren(); result.hidden = !sub;
  if (sub) {
    result.className = 'result'+(sub.status === 'error' ? ' error-result' : '');
    const title = document.createElement('h3');
    title.textContent = sub.status === 'done' ? `本题得分 ${sub.score} / 5` : sub.status === 'error' ? '判分暂未完成' : sub.status === 'queued' ? '答案已保存 · 等待判分' : 'AI 正在验证你的提示词…';
    result.append(title);
    const reason = document.createElement('p'); reason.textContent = sub.reason || '你可以继续作答其他题目，结果会自动更新。'; result.append(reason);
    if (sub.output !== null && sub.output !== '') { const label = document.createElement('p'); label.textContent = '模型原始输出'; const pre = document.createElement('pre'); pre.textContent = sub.output; result.append(label, pre); }
    if (sub.status === 'error') { const retry = document.createElement('button'); retry.className = 'secondary'; retry.textContent = '重试原答案'; const qid = current; retry.onclick = async () => { retry.disabled = true; try { await api('retry',{question:qid}); await refresh(true); } catch(e) { toast(e.message); retry.disabled = false; } }; result.append(retry); }
  }
  tick();
}
$('loginForm').onsubmit = async e => { e.preventDefault(); $('loginButton').disabled = true; $('loginError').textContent = ''; try { await api('login',{username:$('username').value.trim(),password:$('password').value}); $('password').value = ''; await refresh(true); } catch(e) { $('loginError').textContent = e.message; } finally { $('loginButton').disabled = false; } };
$('logout').onclick = async () => { try { await api('logout',{}); showLogin(); } catch(e) { toast(e.message); } };
$('start').onclick = async () => { $('start').disabled = true; try { await api('start',{}); await refresh(true); } catch(e) { toast(e.message); } finally { $('start').disabled = false; } };
$('prompt').oninput = () => { saveDraft(current,$('prompt').value); $('charCount').textContent = `${Array.from($('prompt').value).length} / 2000`; tick(); };
$('previous').onclick = () => { if(current>1) { current--; render(); } };
$('next').onclick = () => { if(current<state.questions.length) { current++; render(); } };
$('submit').onclick = () => { $('confirmDialog').returnValue = 'cancel'; $('confirmDialog').showModal(); };
$('confirmDialog').addEventListener('close', async () => {
  if ($('confirmDialog').returnValue !== 'confirm' || busy) return;
  if (typeof guardPending === 'function' && guardPending()) { toast('请等待切后台记录同步完成后再提交。'); return; }
  const question = current, prompt = $('prompt').value;
  busy = true; tick();
  try { await api('submit',{question,prompt}); await refresh(true); }
  catch(e) { toast(e.message+'；如网络中断，请刷新确认提交状态。'); }
  finally { busy = false; tick(); }
});
setInterval(tick,1000);
setInterval(() => { if(state && !document.hidden) refresh().catch(e => toast('连接暂时中断：'+e.message)); },2500);
refresh(true).catch(e => { showLogin(); if(e.message !== '请先登录') toast('无法连接本地服务，请确认 server.py 已启动。'); });
