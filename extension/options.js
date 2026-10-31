const $ = id => document.getElementById(id);

async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: 'status' });
  $('agent').className = 'row' + (s && s.agent ? ' ok' : '');
  $('agentText').textContent = s && s.agent ? `Desktop agent found (port ${s.port})` : 'FocusFlow desktop agent is not running';
  $('pair').className = 'row' + (s && s.paired ? ' ok' : '');
  $('pairText').textContent = s && s.paired ? 'Paired. Web assist is active.' : 'Not paired yet';
}

$('save').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'set-token', token: $('token').value });
  $('token').value = '';
  refresh();
};
refresh();
setInterval(refresh, 2000);
