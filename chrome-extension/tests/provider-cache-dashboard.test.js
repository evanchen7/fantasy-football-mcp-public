const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  createProviderCachePanel,
  fetchProviderCacheStats,
  runProviderCacheJob,
  validateProviderCacheJobResult,
  validateProviderCacheStats,
} = require('../../src/dashboard/provider-cache-client.js');

const FETCHED_AT = '2026-09-04T11:00:00Z';

function statsFixture(overrides = {}) {
  return {
    schemaVersion: 1,
    status: 'success',
    cache: {
      status: 'available',
      snapshotCount: 2,
      recordCount: 770,
      sizeBytes: 8192,
      latestFetchedAt: FETCHED_AT,
      snapshots: [
        {
          provider: 'FantasyPros',
          dataset: 'projections',
          variant: 'preseason-half',
          season: 2026,
          week: 0,
          recordCount: 415,
          fetchedAt: FETCHED_AT,
          stale: false,
        },
        {
          provider: 'Sleeper',
          dataset: 'sleeper_players',
          variant: 'active',
          season: null,
          week: null,
          recordCount: 355,
          fetchedAt: '2026-09-04T10:00:00Z',
          stale: false,
        },
      ],
    },
    fantasyProsBudget: {
      status: 'available',
      utcDate: '2026-09-04',
      used: 8,
      remaining: 87,
      limit: 95,
    },
    ...overrides,
  };
}

function jobStatsFixture(scoring = 'HALF', overrides = {}) {
  const scoringVariant = scoring.toLowerCase();
  const snapshots = [
    {
      provider: 'FantasyPros',
      dataset: 'players',
      variant: scoring === 'HALF' ? 'catalog' : 'catalog-season',
      season: scoring === 'HALF' ? null : 2026,
      week: scoring === 'HALF' ? null : 0,
      recordCount: 503,
      fetchedAt: FETCHED_AT,
      stale: false,
    },
    {
      provider: 'FantasyPros', dataset: 'injuries', variant: 'weekly',
      season: 2026, week: 0, recordCount: 0, fetchedAt: FETCHED_AT, stale: false,
    },
    {
      provider: 'FantasyPros', dataset: 'news', variant: 'recent',
      season: null, week: null, recordCount: 100, fetchedAt: FETCHED_AT, stale: false,
    },
    {
      provider: 'FantasyPros', dataset: 'projections',
      variant: `preseason-${scoringVariant}`, season: 2026, week: 0,
      recordCount: 415, fetchedAt: FETCHED_AT, stale: false,
    },
    {
      provider: 'Sleeper', dataset: 'sleeper_players', variant: 'active',
      season: null, week: null, recordCount: 2761, fetchedAt: FETCHED_AT, stale: false,
    },
  ];
  if (scoring === 'HALF') {
    snapshots.splice(4, 0, {
      provider: 'FantasyPros', dataset: 'adp', variant: 'preseason-half',
      season: 2026, week: 0, recordCount: 355, fetchedAt: FETCHED_AT, stale: false,
    });
  }
  return statsFixture({
    cache: {
      status: 'available',
      snapshotCount: snapshots.length,
      recordCount: snapshots.reduce((total, snapshot) => total + snapshot.recordCount, 0),
      sizeBytes: 8192,
      latestFetchedAt: FETCHED_AT,
      snapshots,
    },
    ...overrides,
  });
}

function jobFixture(overrides = {}) {
  const scoring = overrides.scoring || 'HALF';
  return {
    schemaVersion: 1,
    status: 'success',
    scoring,
    season: 2026,
    startedAt: '2026-09-04T11:01:00Z',
    completedAt: '2026-09-04T11:01:03Z',
    providers: {
      fantasyPros: {
        status: 'success',
        datasets: {
          players: {
            status: 'available', recordCount: 503, fetchedAt: FETCHED_AT,
            stale: false, refreshFailed: false, publicApiLimited: true,
          },
          injuries: {
            status: 'available', recordCount: 0, fetchedAt: FETCHED_AT,
            stale: false, refreshFailed: false, publicApiLimited: true,
          },
          news: {
            status: 'available', recordCount: 100, fetchedAt: FETCHED_AT,
            stale: false, refreshFailed: false, publicApiLimited: true,
          },
          projections: {
            status: 'available', recordCount: 415, fetchedAt: FETCHED_AT,
            stale: false, refreshFailed: false, publicApiLimited: false,
          },
          adp: {
            status: 'available', recordCount: 355, fetchedAt: FETCHED_AT,
            stale: false, refreshFailed: false, publicApiLimited: false,
          },
        },
      },
      sleeper: {
        status: 'success', recordCount: 2761, fetchedAt: FETCHED_AT,
        stale: false, refreshFailed: false,
      },
    },
    stats: jobStatsFixture(scoring),
    ...overrides,
  };
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: {
      get(name) {
        return String(name).toLowerCase() === 'content-type' ? 'application/json' : null;
      },
    },
    async text() { return JSON.stringify(body); },
  };
}

test('dashboard exposes an accessible provider-cache panel and loads its module before app.js', () => {
  const dashboardRoot = path.join(__dirname, '../../src/dashboard');
  const html = fs.readFileSync(path.join(dashboardRoot, 'index.html'), 'utf8');
  const app = fs.readFileSync(path.join(dashboardRoot, 'app.js'), 'utf8');
  const providerScript = html.indexOf('/draft-dashboard/provider-cache-client.js');
  const appScript = html.indexOf('/draft-dashboard/app.js');

  assert.match(html, /<section[^>]+id="provider-cache-panel"[^>]+aria-labelledby="provider-cache-title"[^>]+aria-busy="false"/);
  assert.match(html, /id="provider-cache-scoring"/);
  assert.match(html, /<option value="STD">Standard<\/option>/);
  assert.match(html, /<option value="HALF" selected>Half PPR<\/option>/);
  assert.match(html, /<option value="PPR">PPR<\/option>/);
  assert.match(html, /id="provider-cache-refresh"[^>]+type="button"/);
  assert.match(html, /id="provider-cache-run"[^>]+type="button"/);
  assert.match(html, /id="provider-cache-status"[^>]+role="status"[^>]+aria-live="polite"/);
  assert.ok(providerScript >= 0 && providerScript < appScript);
  assert.match(app, /providerCache\.createProviderCachePanel\(\)/);
});

test('stats client uses a private same-origin GET with no credentials or cache', async () => {
  let request;
  const result = await fetchProviderCacheStats({
    fetchImpl: async (endpoint, options) => {
      request = { endpoint, options };
      return jsonResponse(statsFixture());
    },
  });

  assert.deepEqual(result, statsFixture());
  assert.equal(request.endpoint, '/provider-cache/stats');
  assert.ok(request.options.signal instanceof AbortSignal);
  const { signal: _signal, ...requestOptions } = request.options;
  assert.deepEqual(requestOptions, {
    method: 'GET',
    credentials: 'omit',
    cache: 'no-store',
    headers: { 'X-Fantasy-Draft-UI': '1' },
  });
});

test('cache job client sends only the selected scoring mode in one exact POST body', async () => {
  let request;
  const result = await runProviderCacheJob('PPR', {
    fetchImpl: async (endpoint, options) => {
      request = { endpoint, options };
      return jsonResponse(jobFixture({ scoring: 'PPR' }));
    },
  });

  assert.equal(result.scoring, 'PPR');
  assert.equal(request.endpoint, '/provider-cache/run');
  assert.ok(request.options.signal instanceof AbortSignal);
  const { signal: _signal, ...requestOptions } = request.options;
  assert.deepEqual(requestOptions, {
    method: 'POST',
    credentials: 'omit',
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Fantasy-Draft-UI': '1',
    },
    body: JSON.stringify({ schemaVersion: 1, scoring: 'PPR' }),
  });

  await assert.rejects(runProviderCacheJob('anything', { fetchImpl: async () => null }), /scoring/i);
});

test('response validation accepts bounded stats and rejects extra, inconsistent, or unsafe data', () => {
  const unavailableSize = statsFixture({
    status: 'degraded',
    cache: {
      status: 'unavailable',
      sizeBytes: null,
      snapshotCount: 0,
      recordCount: 0,
      latestFetchedAt: null,
      snapshots: [],
    },
  });
  assert.equal(validateProviderCacheStats(unavailableSize).cache.sizeBytes, null);
  const missing = statsFixture({
    status: 'degraded',
    cache: {
      status: 'missing',
      sizeBytes: null,
      snapshotCount: 0,
      recordCount: 0,
      latestFetchedAt: null,
      snapshots: [],
    },
  });
  assert.equal(validateProviderCacheStats(missing).cache.status, 'missing');
  const missingBudget = statsFixture({
    status: 'success',
    fantasyProsBudget: {
      status: 'missing', utcDate: '2026-09-04', used: 0, remaining: 95, limit: 95,
    },
  });
  assert.equal(validateProviderCacheStats(missingBudget).fantasyProsBudget.status, 'missing');
  const matchingMissingBudget = jobStatsFixture('HALF', {
    fantasyProsBudget: missingBudget.fantasyProsBudget,
  });
  assert.equal(validateProviderCacheJobResult(jobFixture({ stats: matchingMissingBudget })).status, 'success');
  assert.throws(
    () => validateProviderCacheJobResult(jobFixture({ stats: missing })),
    /invalid/i,
  );
  assert.equal(validateProviderCacheJobResult(jobFixture({
    status: 'degraded',
    stats: missing,
  })).status, 'degraded');
  assert.deepEqual(validateProviderCacheJobResult(jobFixture({ scoring: 'PPR' })).stats, jobStatsFixture('PPR'));
  assert.deepEqual(validateProviderCacheJobResult(jobFixture()), jobFixture());

  const cases = [
    { ...statsFixture(), pageUrl: 'https://example.test/?auth=secret' },
    statsFixture({ status: '<img src=x onerror=alert(1)>' }),
    statsFixture({ status: 'degraded' }),
    statsFixture({
      cache: { ...statsFixture().cache, snapshotCount: 3 },
    }),
    statsFixture({
      cache: { ...statsFixture().cache, recordCount: 999 },
    }),
    statsFixture({
      cache: { ...statsFixture().cache, sizeBytes: 99_999_999 },
    }),
    statsFixture({
      cache: {
        ...statsFixture().cache,
        snapshots: Array.from({ length: 17 }, () => statsFixture().cache.snapshots[0]),
        snapshotCount: 17,
      },
    }),
    statsFixture({
      cache: {
        ...statsFixture().cache,
        snapshots: [{
          ...statsFixture().cache.snapshots[0],
          dataset: 'https://example.test/?token=secret',
        }],
        snapshotCount: 1,
        recordCount: 415,
      },
    }),
    statsFixture({
      fantasyProsBudget: { ...statsFixture().fantasyProsBudget, remaining: 88 },
    }),
    statsFixture({
      cache: {
        ...statsFixture().cache,
        snapshots: [{
          ...statsFixture().cache.snapshots[0],
          dataset: 'injuries',
          variant: 'active',
        }],
        snapshotCount: 1,
        recordCount: 415,
      },
    }),
    statsFixture({
      cache: {
        ...statsFixture().cache,
        snapshots: [{ ...statsFixture().cache.snapshots[0], fetchedAt: '2026-09-04T11:00:00' }],
        snapshotCount: 1,
        recordCount: 415,
        latestFetchedAt: '2026-09-04T11:00:00',
      },
    }),
    statsFixture({
      fantasyProsBudget: { ...statsFixture().fantasyProsBudget, limit: 96, remaining: 88 },
    }),
  ];
  cases.forEach((value) => assert.throws(() => validateProviderCacheStats(value), /invalid/i));

  assert.throws(
    () => validateProviderCacheJobResult(jobFixture({ rawRows: [{ player: 'private' }] })),
    /invalid/i,
  );
  assert.throws(
    () => validateProviderCacheJobResult(jobFixture({
      completedAt: '2026-09-04T11:00:59Z',
    })),
    /invalid/i,
  );
  assert.throws(
    () => validateProviderCacheJobResult(jobFixture({
      providers: {
        ...jobFixture().providers,
        sleeper: {
          status: 'success', recordCount: 0, fetchedAt: null,
          stale: false, refreshFailed: true,
        },
      },
    })),
    /invalid/i,
  );
  const essentialZero = jobFixture();
  essentialZero.providers.fantasyPros.datasets.projections = {
    status: 'available', recordCount: 0, fetchedAt: FETCHED_AT,
    stale: false, refreshFailed: false, publicApiLimited: false,
  };
  assert.throws(() => validateProviderCacheJobResult(essentialZero), /invalid/i);
});

test('job success verifies selected snapshots instead of trusting an unrelated available database', () => {
  assert.equal(validateProviderCacheJobResult(jobFixture()).status, 'success');

  const unrelatedStats = statsFixture();
  assert.throws(
    () => validateProviderCacheJobResult(jobFixture({ stats: unrelatedStats })),
    /invalid/i,
  );
  assert.equal(validateProviderCacheJobResult(jobFixture({
    status: 'degraded',
    stats: unrelatedStats,
  })).status, 'degraded');
});

test('selected snapshot verification matches timestamps at integer-second precision', () => {
  const value = structuredClone(jobFixture());
  value.stats.cache.latestFetchedAt = '2026-09-04T11:00:00.123Z';
  value.stats.cache.snapshots.forEach((snapshot) => {
    snapshot.fetchedAt = '2026-09-04T11:00:00.123Z';
  });
  Object.values(value.providers.fantasyPros.datasets).forEach((dataset) => {
    dataset.fetchedAt = '2026-09-04T11:00:00.987Z';
  });
  value.providers.sleeper.fetchedAt = '2026-09-04T11:00:00.987Z';

  assert.equal(validateProviderCacheJobResult(value).status, 'success');
});

test('client rejects oversized response bodies before parsing them', async () => {
  await assert.rejects(
    fetchProviderCacheStats({
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        headers: { get(name) { return name === 'Content-Length' ? '70000' : null; } },
        async text() { throw new Error('must not read'); },
      }),
    }),
    /too large|invalid/i,
  );
});

test('stats and job requests use bounded abort timers', async () => {
  const delays = [];
  const timerOptions = {
    setTimeoutImpl(callback, delay) {
      delays.push(delay);
      callback();
      return delays.length;
    },
    clearTimeoutImpl() {},
    fetchImpl: async (_endpoint, options) => {
      if (options.signal.aborted) throw new Error('aborted');
      return jsonResponse(statsFixture());
    },
  };
  await assert.rejects(fetchProviderCacheStats(timerOptions), /timed out/i);

  await assert.rejects(runProviderCacheJob('HALF', timerOptions), /timed out/i);
  assert.deepEqual(delays, [5_000, 120_000]);
});

test('stats timeout remains active while the response body is stalled', async () => {
  let timeoutCallback;
  const pending = fetchProviderCacheStats({
    setTimeoutImpl(callback) {
      timeoutCallback = callback;
      return 1;
    },
    clearTimeoutImpl() {},
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      headers: {
        get(name) {
          return String(name).toLowerCase() === 'content-type' ? 'application/json' : null;
        },
      },
      async text() { return new Promise(() => {}); },
    }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  timeoutCallback();
  await assert.rejects(pending, /timed out/i);
});

class FakeNode {
  constructor(value = '') {
    this.value = value;
    this.textContent = '';
    this.className = '';
    this.disabled = false;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  appendChild(child) {
    this.children.push(child);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  click() {
    return this.listeners.get('click')?.({ preventDefault() {} });
  }
}

function fakeDocument() {
  const identifiers = [
    'provider-cache-panel',
    'provider-cache-scoring',
    'provider-cache-refresh',
    'provider-cache-run',
    'provider-cache-status',
    'provider-cache-availability',
    'provider-cache-snapshot-count',
    'provider-cache-record-count',
    'provider-cache-size',
    'provider-cache-latest',
    'provider-cache-budget',
    'provider-cache-job-summary',
    'provider-cache-snapshots',
  ];
  const nodes = new Map(identifiers.map((identifier) => [identifier, new FakeNode()]));
  nodes.get('provider-cache-scoring').value = 'HALF';
  return {
    nodes,
    getElementById(identifier) { return nodes.get(identifier) || null; },
    createElement() { return new FakeNode(); },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test('panel auto-loads stats without running a job and renders only inert bounded text', async () => {
  const documentObj = fakeDocument();
  const requests = [];
  const controller = createProviderCachePanel({
    documentObj,
    fetchImpl: async (endpoint, options) => {
      requests.push({ endpoint, options });
      return jsonResponse(statsFixture());
    },
  });
  await controller.ready;

  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.method, 'GET');
  assert.equal(documentObj.nodes.get('provider-cache-availability').textContent, 'Available');
  assert.equal(documentObj.nodes.get('provider-cache-snapshot-count').textContent, '2');
  assert.equal(documentObj.nodes.get('provider-cache-record-count').textContent, '770');
  assert.match(documentObj.nodes.get('provider-cache-budget').textContent, /8 of 95 locally reserved today · 87 remaining/);
  assert.equal(documentObj.nodes.get('provider-cache-snapshots').children.length, 2);
  assert.match(documentObj.nodes.get('provider-cache-snapshots').children[0].textContent, /FantasyPros · Projections/);

  const source = fs.readFileSync(
    path.join(__dirname, '../../src/dashboard/provider-cache-client.js'),
    'utf8',
  );
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
});

test('panel suppresses double runs, disables controls while busy, and recovers after errors', async () => {
  const documentObj = fakeDocument();
  const runGate = deferred();
  let postCount = 0;
  let failNext = false;
  const controller = createProviderCachePanel({
    documentObj,
    fetchImpl: async (_endpoint, options) => {
      if (options.method === 'GET') return jsonResponse(statsFixture());
      postCount += 1;
      if (failNext) throw new Error('<img src=x onerror=alert(1)> token=private');
      return runGate.promise;
    },
  });
  await controller.ready;

  const first = controller.runJob();
  const duplicate = controller.runJob();
  assert.equal(postCount, 1);
  assert.equal(await duplicate, null);
  assert.equal(documentObj.nodes.get('provider-cache-refresh').disabled, true);
  assert.equal(documentObj.nodes.get('provider-cache-run').disabled, true);
  assert.equal(documentObj.nodes.get('provider-cache-panel').attributes.get('aria-busy'), 'true');
  assert.match(documentObj.nodes.get('provider-cache-status').textContent, /Running/i);

  runGate.resolve(jsonResponse(jobFixture()));
  await first;
  assert.equal(documentObj.nodes.get('provider-cache-refresh').disabled, false);
  assert.equal(documentObj.nodes.get('provider-cache-run').disabled, false);
  assert.equal(documentObj.nodes.get('provider-cache-panel').attributes.get('aria-busy'), 'false');
  assert.match(documentObj.nodes.get('provider-cache-job-summary').textContent, /FantasyPros.*success.*Sleeper.*success/i);

  failNext = true;
  await controller.runJob();
  assert.equal(documentObj.nodes.get('provider-cache-refresh').disabled, false);
  assert.equal(documentObj.nodes.get('provider-cache-run').disabled, false);
  assert.match(documentObj.nodes.get('provider-cache-status').textContent, /could not be completed/i);
  assert.doesNotMatch(documentObj.nodes.get('provider-cache-status').textContent, /img|token|private/i);
});
