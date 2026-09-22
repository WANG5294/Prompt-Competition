// Signals of leaving the page are reminders, not tamper-proof proctoring.
let guardScope = '', guardAway = false, guardWarning = false;
let guardFlushBusy = false, guardCount = 0, guardQueue = [];
let guardStorageOK = true;
const guardTitle = document.title;
function guardPersist() {
  if (!guardScope) return;
  try { localStorage.setItem(guardScope, JSON.stringify({queue:guardQueue, warning:guardWarning})); }
  catch { guardStorageOK = false; }
}
function guardStatus() {
  if (!$('guardStatus')) return;
  const running = active();
  $('guardStatus').textContent = !state?.endsAt ? '开赛后自动检测' : running ? '离开页面检测中' : '考试结束 · 检测已停止';
  $('guardCount').textContent = `切后台 ${guardCount} 次`;
  $('guardPenalty').textContent = `累计罚时 ${state?.penaltySeconds || 0} 秒 · 下次罚时 ${(guardCount+1)*30} 秒`;
  $('guardSaveState').textContent = guardQueue.length ? `有 ${guardQueue.length} 条记录等待同步${guardStorageOK ? '' : '；浏览器存储不可用，请保持页面开启'}` : '记录已同步';
}
function guardShowWarning() {
  if (!guardWarning || !state) return;
  document.title = '⚠ 请返回考试页面 · 言外之意';
  $('guardMessage').textContent = guardQueue.length ? '检测到离开页面，正在同步罚时。第 n 次罚时 30 × n 秒；同步完成前不能提交答案。' : `已记录 ${guardCount} 次切后台，累计罚时 ${state.penaltySeconds || 0} 秒。${active() ? '截止时间已提前，请保持页面在前台。' : '作答时间已结束，不能继续提交新答案。'}`;
  if (!$('guardDialog').open) $('guardDialog').showModal();

}
function guardLeave(kind) {
  if (!active() || guardAway) return;
  guardAway = true; guardWarning = true;
  guardQueue.push({id:crypto.randomUUID(), kind, occurred:(Date.now()+offset)/1000});
  guardPersist(); guardShowWarning(); guardStatus(); void guardFlush();
}
async function guardFlush() {
  if (guardFlushBusy || !state || !guardQueue.length) return;
  guardFlushBusy = true;
  const scope = guardScope;
  try {
    while (guardQueue.length && state && guardScope === scope) {
      const event = guardQueue[0];
      const response = await fetch('/api/focus-event', {method:'POST', keepalive:true,
        headers:{'Content-Type':'application/json', 'X-Contest-Request':'1'}, body:JSON.stringify(event)});
      if (!response.ok) break;
      const result = await response.json();
      if (guardScope !== scope) break;
      guardQueue = guardQueue.filter(item => item.id !== event.id);
      guardApplyTiming(result);
      guardCount = state.focusCount;
      if (!result.applied) toast(result.reason || '该记录未产生额外罚时。');
      if (guardWarning) guardShowWarning();
      guardPersist();
    }
  } catch { /* Retry on return, reconnection, or periodic sync. */ }
  finally { guardFlushBusy = false; guardStatus(); }
}
function guardSync() {
  if (!state) {
    guardAway = false; guardWarning = false; guardScope = '';
    if ($('guardDialog').open) $('guardDialog').close();
    document.title = guardTitle; return;
  }
  const scope = `prompt-arena-penalty-v2-${state.user}-${state.baseEndsAt}`;
  if (scope !== guardScope) {
    guardScope = scope; guardQueue = []; guardWarning = false; guardAway = false;
    try {
      const saved = JSON.parse(localStorage.getItem(scope) || '{}');
      guardQueue = Array.isArray(saved.queue) ? saved.queue : [];
      guardWarning = !!saved.warning;
    } catch { guardStorageOK = false; }
    guardCount = state.focusCount || 0;
    if (guardWarning) guardShowWarning();
    void guardFlush();
  }
  guardCount = Math.max(guardCount, state.focusCount || 0);
  if (active() && document.hidden) guardLeave('hidden');
  guardStatus();
}
function guardReturn() {
  if (document.hidden || !document.hasFocus()) return;
  guardAway = false;
  if (guardWarning) guardShowWarning();
  void guardFlush();
}
document.addEventListener('visibilitychange', () => { if(document.hidden) guardLeave('hidden'); else guardReturn(); });
window.addEventListener('blur', () => guardLeave('blur'));
window.addEventListener('focus', guardReturn);
window.addEventListener('pagehide', () => guardLeave('pagehide'));
window.addEventListener('online', () => void guardFlush());
$('guardAcknowledge').onclick = () => {
  if (document.hidden) return;
  guardWarning = false; guardAway = false; guardPersist();
  $('guardDialog').close(); document.title = guardTitle; void guardFlush();
};
$('guardDialog').addEventListener('cancel', event => event.preventDefault());
function guardApplyTiming(result) {
  if (!state || result.baseEndsAt !== state.baseEndsAt) return;
  if (result.penaltySeconds >= (state.penaltySeconds || 0)) {
    state.endsAt = result.endsAt; state.penaltySeconds = result.penaltySeconds; state.focusCount = result.focusCount;
  }
  offset = result.serverTime*1000-Date.now();
  tick();
}
function guardPending() { return guardQueue.length > 0; }
setInterval(() => void guardFlush(), 5000);
