(function initializeDraftDashboard() {
  'use strict';

  const client = globalThis.YahooDraftRecommendationClient;
  const viewModels = globalThis.YahooDraftRecommendationViewModel;
  const renderer = globalThis.YahooDraftRecommendationRenderer;
  const form = document.getElementById('recommendation-form');
  const leagueInput = document.getElementById('league-id');
  const requestStatus = document.getElementById('request-status');
  const recommendationView = document.getElementById('recommendation-view');
  const formControls = [...form.elements];

  function clear(node) {
    node.replaceChildren();
  }

  function textElement(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function finiteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function show(id) {
    document.getElementById(id).classList.remove('hidden');
  }

  function resetAnalysisPanels() {
    [
      'quality-panel',
      'draft-summary',
      'recommendations-panel',
      'scenario-panel',
      'roster-panel',
      'history-panel',
    ].forEach((id) => document.getElementById(id).classList.add('hidden'));
    [
      'quality-summary',
      'diagnostic-details',
      'recommendation-view',
      'scenario-body',
      'roster-counts',
      'roster-list',
      'draft-history',
    ].forEach((id) => clear(document.getElementById(id)));
    ['current-pick', 'next-pick', 'pick-count', 'team-count'].forEach((id) => {
      document.getElementById(id).textContent = '—';
    });
  }

  function setControlsDisabled(disabled) {
    formControls.forEach((control) => {
      control.disabled = disabled;
    });
  }

  function setStatus(message, kind) {
    requestStatus.textContent = message;
    requestStatus.className = `request-status ${kind}`;
  }

  function prefillLeagueFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const leagueId = params.get('leagueId') || '';
    if (/^\d{1,32}$/.test(leagueId)) {
      leagueInput.value = leagueId;
      setStatus(`League ${leagueId} selected from the extension. Refresh when ready.`, 'idle');
    }
  }

  function diagnosticGroup(title, values) {
    const group = textElement('section', '', 'diagnostic-group');
    group.appendChild(textElement('h3', title));
    const list = textElement('ul', '', 'compact-list');
    values.forEach((value) => list.appendChild(textElement('li', value)));
    group.appendChild(list);
    return group;
  }

  function renderDiagnostics(data, model) {
    show('quality-panel');
    const summary = document.getElementById('quality-summary');
    const details = document.getElementById('diagnostic-details');
    clear(summary);
    clear(details);
    summary.appendChild(textElement('strong', model.statusTitle, model.mode === 'success' ? 'ready' : 'blocked'));
    summary.appendChild(textElement('span', model.statusMessage));
    const latency = finiteNumber(data.latencyMs);
    summary.appendChild(textElement(
      'span',
      `Strategy ${data.strategy || 'unknown'} · ${latency === null ? 'scoring latency unavailable' : `${latency.toFixed(1)} ms scoring latency`}`,
    ));

    const sources = data.dataSources && typeof data.dataSources === 'object'
      ? Object.entries(data.dataSources).slice(0, 8).map(([name, source]) => `${name}: ${String(source)}`)
      : ['Source attribution unavailable.'];
    details.appendChild(diagnosticGroup('Data sources', sources));

    const checks = data?.critic?.checks && typeof data.critic.checks === 'object'
      ? Object.entries(data.critic.checks).slice(0, 12).map(([name, passed]) => `${name}: ${passed === true ? 'pass' : 'needs caution'}`)
      : ['Critic checks unavailable.'];
    details.appendChild(diagnosticGroup('Deterministic critic', checks));
  }

  function renderSummary(data) {
    const state = data.state || {};
    show('draft-summary');
    document.getElementById('current-pick').textContent =
      finiteNumber(state.currentOverallPick) ?? '—';
    document.getElementById('next-pick').textContent =
      finiteNumber(state.nextUserPick) ?? '—';
    document.getElementById('pick-count').textContent = Array.isArray(state.picks) ? state.picks.length : '—';
    document.getElementById('team-count').textContent = finiteNumber(state.teamCount) ?? 'unavailable';
  }

  function renderSharedBoard(data, leagueId) {
    show('recommendations-panel');
    const model = viewModels.createRecommendationViewModel(
      data,
      { leagueId },
      { maxRecommendations: 20 },
    );
    renderer.renderRecommendationView(recommendationView, model);
    return model;
  }

  function renderScenarios(data, model) {
    const recommendationMode = model?.mode === 'success' || model?.mode === 'degraded';
    const visibleRecommendationCount = Array.isArray(model?.recommendations)
      ? model.recommendations.length
      : 0;
    const recommendations = recommendationMode && Array.isArray(data.recommendations)
      ? data.recommendations.slice(0, visibleRecommendationCount)
      : [];
    const body = document.getElementById('scenario-body');
    clear(body);
    if (recommendations.length === 0) {
      document.getElementById('scenario-panel').classList.add('hidden');
      return;
    }
    show('scenario-panel');
    const scoreKeys = ['value', 'rosterConstruction', 'draftDynamics', 'opponentModel', 'riskNews', 'scenario'];
    recommendations.forEach((item) => {
      const row = document.createElement('tr');
      row.appendChild(textElement('th', String(item?.player?.name || 'Unknown')));
      scoreKeys.forEach((key) => {
        const cell = document.createElement('td');
        const rawScore = finiteNumber(item?.scores?.[key]);
        if (rawScore === null) {
          cell.appendChild(textElement('span', '—', 'unavailable-score'));
        } else {
          const score = Math.max(0, Math.min(100, rawScore));
          const progress = document.createElement('progress');
          progress.max = 100;
          progress.value = score;
          progress.setAttribute('aria-label', `${key} score ${score} out of 100`);
          cell.appendChild(progress);
          cell.appendChild(textElement('span', score.toFixed(0)));
        }
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  function renderRoster(data) {
    const roster = Array.isArray(data?.state?.userRoster) ? data.state.userRoster : [];
    const counts = document.getElementById('roster-counts');
    const list = document.getElementById('roster-list');
    clear(counts);
    clear(list);
    if (roster.length === 0) {
      document.getElementById('roster-panel').classList.add('hidden');
      return;
    }
    show('roster-panel');
    const totals = new Map();
    roster.forEach((pick) => {
      const position = String(pick.position || 'Unknown');
      totals.set(position, (totals.get(position) || 0) + 1);
      list.appendChild(textElement(
        'li',
        `${pick.player || 'Unknown'} · ${position}${pick.pickNumber ? ` · pick ${pick.pickNumber}` : ''}`,
      ));
    });
    [...totals.entries()].sort().forEach(([position, total]) => {
      counts.appendChild(textElement('span', `${position} ${total}`, 'position-chip'));
    });
  }

  function renderHistory(data) {
    const picks = Array.isArray(data?.state?.picks) ? [...data.state.picks] : [];
    const list = document.getElementById('draft-history');
    clear(list);
    if (picks.length === 0) {
      document.getElementById('history-panel').classList.add('hidden');
      return;
    }
    show('history-panel');
    picks.slice(-24).reverse().forEach((pick) => {
      const item = textElement('li', '', pick.isUserPick ? 'user-pick' : '');
      item.appendChild(textElement('strong', `#${pick.pickNumber ?? '?'}`));
      item.appendChild(textElement('span', String(pick.player || 'Unknown player')));
      item.appendChild(textElement('small', [pick.position, pick.nflTeam, pick.fantasyTeam].filter(Boolean).join(' · ')));
      list.appendChild(item);
    });
  }

  function render(data, leagueId) {
    resetAnalysisPanels();
    const model = renderSharedBoard(data, leagueId);
    renderDiagnostics(data, model);
    if (data.state) renderSummary(data);
    renderScenarios(data, model);
    renderRoster(data);
    renderHistory(data);
    return model;
  }

  function requestStatusForModel(model, leagueId) {
    if (model.mode === 'success') {
      return [`Analysis refreshed for league ${leagueId}.`, 'success'];
    }
    if (model.mode === 'degraded') {
      return [`Analysis for league ${leagueId} needs the cautions below.`, 'warning'];
    }
    if (model.mode === 'blocked') {
      return [`Recommendations are blocked for league ${leagueId}; repair the draft ledger.`, 'warning'];
    }
    return [`Recommendations are unavailable for league ${leagueId}.`, 'error'];
  }

  function renderClientError(message, leagueId) {
    show('recommendations-panel');
    const model = viewModels.createRecommendationViewModel(
      { status: 'error', leagueId, message },
      { leagueId },
    );
    renderer.renderRecommendationView(recommendationView, model);
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const leagueId = leagueInput.value.trim();
    resetAnalysisPanels();
    setControlsDisabled(true);
    setStatus('Refreshing Yahoo context and running bounded specialist scoring…', 'loading');
    try {
      const data = await client.fetchDraftRecommendationsForLeagueId(leagueId, {
        endpoint: '/draft-recommendation',
        strategy: document.getElementById('strategy').value,
        count: document.getElementById('count').value,
        rankingCount: document.getElementById('ranking-count').value,
        simulations: document.getElementById('simulations').value,
        timeoutMs: 30000,
      });
      if (leagueInput.value.trim() !== leagueId) {
        setStatus('League selection changed; the prior response was discarded.', 'warning');
        return;
      }
      const model = render(data, leagueId);
      setStatus(...requestStatusForModel(model, leagueId));
    } catch (error) {
      const message = error?.name === 'AbortError'
        ? 'Recommendation timed out. Confirm the loopback server and Yahoo authentication, then retry.'
        : String(error?.message || 'Recommendation unavailable.');
      resetAnalysisPanels();
      renderClientError(message, leagueId);
      setStatus(message, 'error');
    } finally {
      setControlsDisabled(false);
    }
  });

  if (!client || !viewModels || !renderer) {
    setStatus('Shared recommendation UI modules are unavailable.', 'error');
    setControlsDisabled(true);
    return;
  }
  prefillLeagueFromFragment();
}());
