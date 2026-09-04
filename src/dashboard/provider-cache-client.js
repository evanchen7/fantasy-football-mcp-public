(function initProviderCacheClient(globalScope) {
  'use strict';

  const STATS_ENDPOINT = '/provider-cache/stats';
  const RUN_ENDPOINT = '/provider-cache/run';
  const UI_HEADERS = Object.freeze({ 'X-Fantasy-Draft-UI': '1' });
  const SCORING = new Set(['STD', 'HALF', 'PPR']);
  const CACHE_STATUSES = new Set(['available', 'missing', 'unavailable']);
  const RESPONSE_STATUSES = new Set(['success', 'degraded']);
  const JOB_PROVIDER_STATUSES = new Set(['success', 'degraded', 'unavailable']);
  const DATASET_STATUSES = new Set(['available', 'unavailable']);
  const EMPTY_AVAILABLE_DATASETS = new Set(['injuries', 'news']);
  const DATASETS = new Set([
    'players',
    'injuries',
    'news',
    'projections',
    'adp',
    'sleeper_players',
  ]);
  const VARIANTS = new Set([
    'catalog',
    'catalog-season',
    'weekly',
    'recent',
    'preseason-std',
    'preseason-half',
    'preseason-ppr',
    'active',
  ]);
  const MAX_SNAPSHOTS = 16;
  const MAX_RECORD_COUNT = 100_000;
  const MAX_DATABASE_BYTES = 16_777_216;
  const MAX_LOCAL_BUDGET = 95;
  const MAX_RESPONSE_BYTES = 65_536;
  const STATS_TIMEOUT_MS = 5_000;
  const JOB_TIMEOUT_MS = 120_000;
  const TIMEZONE_AWARE_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

  class ProviderCacheResponseError extends Error {}

  function invalidResponse() {
    throw new ProviderCacheResponseError('Provider cache returned an invalid response.');
  }

  function exactObject(value, keys) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) invalidResponse();
    const actual = Object.keys(value).sort();
    const expected = [...keys].sort();
    if (
      actual.length !== expected.length ||
      actual.some((key, index) => key !== expected[index])
    ) invalidResponse();
    return value;
  }

  function boundedInteger(value, minimum, maximum) {
    if (!Number.isInteger(value) || value < minimum || value > maximum) invalidResponse();
    return value;
  }

  function nullableBoundedInteger(value, minimum, maximum) {
    if (value === null) return null;
    return boundedInteger(value, minimum, maximum);
  }

  function oneOf(value, values) {
    if (typeof value !== 'string' || !values.has(value)) invalidResponse();
    return value;
  }

  function booleanValue(value) {
    if (typeof value !== 'boolean') invalidResponse();
    return value;
  }

  function isoTimestamp(value, { nullable = false } = {}) {
    if (nullable && value === null) return null;
    const datePrefix = typeof value === 'string' ? value.slice(0, 10) : '';
    const [year, month, day] = datePrefix.split('-').map(Number);
    const canonicalDate = /^\d{4}-\d{2}-\d{2}$/.test(datePrefix)
      ? new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10)
      : '';
    if (
      typeof value !== 'string' ||
      value.length < 20 ||
      value.length > 40 ||
      !TIMEZONE_AWARE_ISO.test(value) ||
      !Number.isFinite(Date.parse(value)) ||
      year < 2000 ||
      year > 2100 ||
      canonicalDate !== datePrefix
    ) invalidResponse();
    return value;
  }

  function utcDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      invalidResponse();
    }
    const parsed = new Date(`${value}T00:00:00Z`);
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
      invalidResponse();
    }
    return value;
  }

  function nullableSeason(value) {
    if (value === null) return null;
    return boundedInteger(value, 2012, 2100);
  }

  function nullableWeek(value) {
    if (value === null) return null;
    return boundedInteger(value, 0, 25);
  }

  function validateSnapshot(value) {
    exactObject(value, [
      'provider',
      'dataset',
      'variant',
      'season',
      'week',
      'fetchedAt',
      'recordCount',
      'stale',
    ]);
    oneOf(value.provider, new Set(['FantasyPros', 'Sleeper']));
    oneOf(value.dataset, DATASETS);
    oneOf(value.variant, VARIANTS);
    nullableSeason(value.season);
    nullableWeek(value.week);
    isoTimestamp(value.fetchedAt);
    boundedInteger(value.recordCount, 0, 10_000);
    booleanValue(value.stale);
    if (
      (value.provider === 'Sleeper' && value.dataset !== 'sleeper_players') ||
      (value.provider === 'FantasyPros' && value.dataset === 'sleeper_players')
    ) invalidResponse();
    const validScope = (
      (value.dataset === 'players' && value.variant === 'catalog' && value.season === null && value.week === null) ||
      (value.dataset === 'players' && value.variant === 'catalog-season' && value.season !== null && value.week === 0) ||
      (value.dataset === 'injuries' && value.variant === 'weekly' && value.season !== null && value.week !== null) ||
      (value.dataset === 'news' && value.variant === 'recent' && value.season === null && value.week === null) ||
      (value.dataset === 'projections' && /^preseason-(?:std|half|ppr)$/.test(value.variant) && value.season !== null && value.week === 0) ||
      (value.dataset === 'adp' && /^preseason-(?:std|half|ppr)$/.test(value.variant) && value.season !== null && value.week === 0) ||
      (value.dataset === 'sleeper_players' && value.variant === 'active' && value.season === null && value.week === null)
    );
    if (!validScope) invalidResponse();
    return value;
  }

  function validateCache(value) {
    exactObject(value, [
      'status',
      'sizeBytes',
      'snapshotCount',
      'recordCount',
      'latestFetchedAt',
      'snapshots',
    ]);
    oneOf(value.status, CACHE_STATUSES);
    if (value.sizeBytes !== null) boundedInteger(value.sizeBytes, 0, MAX_DATABASE_BYTES);
    boundedInteger(value.snapshotCount, 0, MAX_SNAPSHOTS);
    boundedInteger(value.recordCount, 0, MAX_RECORD_COUNT);
    isoTimestamp(value.latestFetchedAt, { nullable: true });
    if (!Array.isArray(value.snapshots) || value.snapshots.length > MAX_SNAPSHOTS) {
      invalidResponse();
    }
    value.snapshots.forEach(validateSnapshot);
    const recordCount = value.snapshots.reduce(
      (total, snapshot) => total + snapshot.recordCount,
      0,
    );
    if (value.snapshotCount !== value.snapshots.length || value.recordCount !== recordCount) {
      invalidResponse();
    }
    if (value.snapshots.length === 0 && value.latestFetchedAt !== null) invalidResponse();
    if (value.snapshots.length > 0) {
      const latestFetchedAt = value.snapshots.reduce((latest, snapshot) => (
        Date.parse(snapshot.fetchedAt) > Date.parse(latest) ? snapshot.fetchedAt : latest
      ), value.snapshots[0].fetchedAt);
      if (Date.parse(value.latestFetchedAt) !== Date.parse(latestFetchedAt)) invalidResponse();
    }
    if (value.status === 'available' && (value.sizeBytes === null || value.sizeBytes === 0)) {
      invalidResponse();
    }
    if (
      value.status !== 'available' &&
      (value.snapshots.length !== 0 || value.recordCount !== 0)
    ) invalidResponse();
    if (value.status !== 'available' && value.sizeBytes !== null) invalidResponse();
    return value;
  }

  function validateBudget(value) {
    exactObject(value, ['status', 'utcDate', 'used', 'remaining', 'limit']);
    oneOf(value.status, CACHE_STATUSES);
    utcDate(value.utcDate);
    const limit = boundedInteger(value.limit, 1, MAX_LOCAL_BUDGET);
    const used = nullableBoundedInteger(value.used, 0, limit);
    const remaining = nullableBoundedInteger(value.remaining, 0, limit);
    if ((used === null) !== (remaining === null)) invalidResponse();
    if (used !== null && used + remaining !== limit) invalidResponse();
    if (value.status === 'available' && used === null) invalidResponse();
    if (value.status === 'missing' && (used !== 0 || remaining !== limit)) invalidResponse();
    if (value.status === 'unavailable' && (used !== null || remaining !== null)) invalidResponse();
    return value;
  }

  function validateProviderCacheStats(value) {
    exactObject(value, ['schemaVersion', 'status', 'cache', 'fantasyProsBudget']);
    if (value.schemaVersion !== 1) invalidResponse();
    oneOf(value.status, RESPONSE_STATUSES);
    validateCache(value.cache);
    validateBudget(value.fantasyProsBudget);
    const statsHealthy = value.cache.status === 'available' &&
      value.fantasyProsBudget.status !== 'unavailable';
    if (
      (value.status === 'success' && !statsHealthy) ||
      (value.status === 'degraded' && statsHealthy)
    ) invalidResponse();
    return value;
  }

  function validateDatasetResult(value, datasetName) {
    exactObject(value, [
      'status',
      'recordCount',
      'fetchedAt',
      'stale',
      'refreshFailed',
      'publicApiLimited',
    ]);
    oneOf(value.status, DATASET_STATUSES);
    boundedInteger(value.recordCount, 0, 10_000);
    isoTimestamp(value.fetchedAt, { nullable: true });
    booleanValue(value.stale);
    booleanValue(value.refreshFailed);
    booleanValue(value.publicApiLimited);
    if (
      value.status === 'available' &&
      value.fetchedAt === null
    ) invalidResponse();
    if (
      value.status === 'available' &&
      !EMPTY_AVAILABLE_DATASETS.has(datasetName) &&
      value.recordCount === 0
    ) invalidResponse();
    if (
      value.status === 'unavailable' &&
      (value.recordCount !== 0 || value.fetchedAt !== null || value.stale)
    ) invalidResponse();
    return value;
  }

  function validateFantasyProsResult(value) {
    exactObject(value, ['status', 'datasets']);
    oneOf(value.status, JOB_PROVIDER_STATUSES);
    exactObject(value.datasets, ['players', 'injuries', 'news', 'projections', 'adp']);
    Object.entries(value.datasets).forEach(([datasetName, dataset]) => {
      validateDatasetResult(dataset, datasetName);
    });
    const datasets = Object.values(value.datasets);
    const allCurrent = datasets.every((dataset) => (
      dataset.status === 'available' && !dataset.stale && !dataset.refreshFailed
    ));
    const allUnavailable = datasets.every((dataset) => dataset.status === 'unavailable');
    if (
      (value.status === 'success' && !allCurrent) ||
      (value.status === 'unavailable' && !allUnavailable) ||
      (value.status === 'degraded' && (allCurrent || allUnavailable))
    ) invalidResponse();
    return value;
  }

  function validateSleeperResult(value) {
    exactObject(value, [
      'status',
      'recordCount',
      'fetchedAt',
      'stale',
      'refreshFailed',
    ]);
    oneOf(value.status, JOB_PROVIDER_STATUSES);
    boundedInteger(value.recordCount, 0, 10_000);
    isoTimestamp(value.fetchedAt, { nullable: true });
    booleanValue(value.stale);
    booleanValue(value.refreshFailed);
    const current = value.recordCount > 0 && value.fetchedAt !== null && !value.stale && !value.refreshFailed;
    const unavailable = value.recordCount === 0 && value.fetchedAt === null && !value.stale;
    if (
      (value.status === 'success' && !current) ||
      (value.status === 'unavailable' && !unavailable) ||
      (value.status === 'degraded' && (current || unavailable))
    ) invalidResponse();
    return value;
  }

  function sameTimestampSecond(left, right) {
    return Math.floor(Date.parse(left) / 1_000) === Math.floor(Date.parse(right) / 1_000);
  }

  function findJobSnapshot(value, expected) {
    return value.stats.cache.snapshots.find((snapshot) => (
      snapshot.provider === expected.provider &&
      snapshot.dataset === expected.dataset &&
      snapshot.variant === expected.variant &&
      snapshot.season === expected.season &&
      snapshot.week === expected.week &&
      snapshot.stale === false
    )) || null;
  }

  function exactDatasetSnapshot(value, datasetName, scope) {
    const result = value.providers.fantasyPros.datasets[datasetName];
    const snapshot = findJobSnapshot(value, {
      provider: 'FantasyPros',
      dataset: datasetName,
      ...scope,
    });
    return snapshot !== null &&
      result.status === 'available' &&
      result.fetchedAt !== null &&
      snapshot.recordCount === result.recordCount &&
      sameTimestampSecond(snapshot.fetchedAt, result.fetchedAt);
  }

  function selectedSnapshotsVerified(value) {
    if (value.stats.cache.status !== 'available') return false;
    const season = value.season;
    const scoringVariant = value.scoring.toLowerCase();
    const catalogVariant = value.scoring === 'HALF' ? 'catalog' : 'catalog-season';
    const catalogSeason = value.scoring === 'HALF' ? null : season;
    const catalogWeek = value.scoring === 'HALF' ? null : 0;
    const catalogSnapshot = findJobSnapshot(value, {
      provider: 'FantasyPros',
      dataset: 'players',
      variant: catalogVariant,
      season: catalogSeason,
      week: catalogWeek,
    });
    const players = value.providers.fantasyPros.datasets.players;
    if (
      catalogSnapshot === null ||
      players.fetchedAt === null ||
      catalogSnapshot.recordCount !== players.recordCount ||
      !sameTimestampSecond(catalogSnapshot.fetchedAt, players.fetchedAt)
    ) return false;
    if (!exactDatasetSnapshot(value, 'injuries', {
      variant: 'weekly', season, week: 0,
    })) return false;
    if (!exactDatasetSnapshot(value, 'news', {
      variant: 'recent', season: null, week: null,
    })) return false;
    if (!exactDatasetSnapshot(value, 'projections', {
      variant: `preseason-${scoringVariant}`, season, week: 0,
    })) return false;
    const adp = value.providers.fantasyPros.datasets.adp;
    if (value.scoring === 'HALF') {
      if (!exactDatasetSnapshot(value, 'adp', {
        variant: 'preseason-half', season, week: 0,
      })) return false;
    } else if (
      adp.fetchedAt === null ||
      adp.recordCount > catalogSnapshot.recordCount ||
      !sameTimestampSecond(adp.fetchedAt, catalogSnapshot.fetchedAt)
    ) return false;
    const sleeper = value.providers.sleeper;
    const sleeperSnapshot = findJobSnapshot(value, {
      provider: 'Sleeper',
      dataset: 'sleeper_players',
      variant: 'active',
      season: null,
      week: null,
    });
    return sleeperSnapshot !== null &&
      sleeper.fetchedAt !== null &&
      sleeperSnapshot.recordCount === sleeper.recordCount &&
      sameTimestampSecond(sleeperSnapshot.fetchedAt, sleeper.fetchedAt);
  }

  function validateProviderCacheJobResult(value) {
    exactObject(value, [
      'schemaVersion',
      'status',
      'scoring',
      'season',
      'startedAt',
      'completedAt',
      'providers',
      'stats',
    ]);
    if (value.schemaVersion !== 1) invalidResponse();
    oneOf(value.status, RESPONSE_STATUSES);
    oneOf(value.scoring, SCORING);
    boundedInteger(value.season, 2012, 2100);
    isoTimestamp(value.startedAt);
    isoTimestamp(value.completedAt);
    if (Date.parse(value.startedAt) > Date.parse(value.completedAt)) invalidResponse();
    exactObject(value.providers, ['fantasyPros', 'sleeper']);
    validateFantasyProsResult(value.providers.fantasyPros);
    validateSleeperResult(value.providers.sleeper);
    validateProviderCacheStats(value.stats);
    const allSuccess = value.providers.fantasyPros.status === 'success' &&
      value.providers.sleeper.status === 'success' &&
      selectedSnapshotsVerified(value);
    if (
      (value.status === 'success' && !allSuccess) ||
      (value.status === 'degraded' && allSuccess)
    ) invalidResponse();
    return value;
  }

  function selectedFetch(options) {
    const fetchImpl = options.fetchImpl || globalScope.fetch?.bind(globalScope);
    if (typeof fetchImpl !== 'function') {
      throw new ProviderCacheResponseError('Provider cache fetch is unavailable.');
    }
    return fetchImpl;
  }

  function responseByteLength(value) {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(value).byteLength;
    return value.length * 4;
  }

  async function parseResponse(response, validator, operation) {
    if (!response || typeof response !== 'object') {
      throw new ProviderCacheResponseError(`${operation} could not be completed.`);
    }
    if (!response.ok) {
      const status = Number.isInteger(response.status) && response.status >= 400 && response.status <= 599
        ? response.status
        : null;
      const suffix = status === null ? '' : ` (HTTP ${status})`;
      throw new ProviderCacheResponseError(`${operation} could not be completed${suffix}.`);
    }
    const contentType = response.headers?.get?.('Content-Type');
    if (
      typeof contentType !== 'string' ||
      contentType.split(';', 1)[0].trim().toLowerCase() !== 'application/json'
    ) {
      throw new ProviderCacheResponseError(`${operation} returned an invalid response.`);
    }
    const contentLengthText = response.headers?.get?.('Content-Length');
    if (contentLengthText !== null && contentLengthText !== undefined && contentLengthText !== '') {
      const contentLength = Number(contentLengthText);
      if (!Number.isInteger(contentLength) || contentLength < 0 || contentLength > MAX_RESPONSE_BYTES) {
        throw new ProviderCacheResponseError(`${operation} response is too large or invalid.`);
      }
    }
    if (typeof response.text !== 'function') {
      throw new ProviderCacheResponseError(`${operation} returned an invalid response.`);
    }
    let text;
    try {
      text = await response.text();
    } catch (_error) {
      throw new ProviderCacheResponseError(`${operation} returned an invalid response.`);
    }
    if (typeof text !== 'string' || responseByteLength(text) > MAX_RESPONSE_BYTES) {
      throw new ProviderCacheResponseError(`${operation} response is too large or invalid.`);
    }
    let body;
    try {
      body = JSON.parse(text);
    } catch (_error) {
      throw new ProviderCacheResponseError(`${operation} returned an invalid response.`);
    }
    return validator(body);
  }

  async function boundedFetch(fetchImpl, endpoint, request, timeoutMs, options, consume) {
    const AbortControllerImpl = options.AbortControllerImpl || globalScope.AbortController;
    const setTimeoutImpl = options.setTimeoutImpl || globalScope.setTimeout?.bind(globalScope);
    const clearTimeoutImpl = options.clearTimeoutImpl || globalScope.clearTimeout?.bind(globalScope);
    if (
      typeof AbortControllerImpl !== 'function' ||
      typeof setTimeoutImpl !== 'function' ||
      typeof clearTimeoutImpl !== 'function'
    ) {
      throw new ProviderCacheResponseError('Provider cache request controls are unavailable.');
    }
    const controller = new AbortControllerImpl();
    let timer;
    const timeout = new Promise((_resolve, reject) => {
      timer = setTimeoutImpl(() => {
        controller.abort();
        reject(new ProviderCacheResponseError('Provider cache request timed out.'));
      }, timeoutMs);
    });
    try {
      const operation = (async () => {
        const response = await fetchImpl(endpoint, { ...request, signal: controller.signal });
        return consume(response);
      })();
      return await Promise.race([operation, timeout]);
    } catch (error) {
      if (controller.signal.aborted) {
        throw new ProviderCacheResponseError('Provider cache request timed out.');
      }
      if (error instanceof ProviderCacheResponseError) throw error;
      throw new ProviderCacheResponseError('Provider cache request could not be completed.');
    } finally {
      clearTimeoutImpl(timer);
    }
  }

  async function fetchProviderCacheStats(options = {}) {
    const fetchImpl = selectedFetch(options);
    return boundedFetch(fetchImpl, STATS_ENDPOINT, {
      method: 'GET',
      credentials: 'omit',
      cache: 'no-store',
      headers: { ...UI_HEADERS },
    }, STATS_TIMEOUT_MS, options, (response) => (
      parseResponse(response, validateProviderCacheStats, 'Provider cache stats')
    ));
  }

  async function runProviderCacheJob(scoring, options = {}) {
    if (typeof scoring !== 'string' || !SCORING.has(scoring)) {
      throw new ProviderCacheResponseError('Provider cache scoring mode is invalid.');
    }
    const fetchImpl = selectedFetch(options);
    return boundedFetch(fetchImpl, RUN_ENDPOINT, {
      method: 'POST',
      credentials: 'omit',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        ...UI_HEADERS,
      },
      body: JSON.stringify({ schemaVersion: 1, scoring }),
    }, JOB_TIMEOUT_MS, options, async (response) => {
      const result = await parseResponse(
        response,
        validateProviderCacheJobResult,
        'Provider cache job',
      );
      if (result.scoring !== scoring) invalidResponse();
      return result;
    });
  }

  function formatBytes(value) {
    if (value === null) return 'Unavailable';
    if (value < 1024) return `${value} B`;
    if (value < 1_048_576) return `${Math.round(value / 1024)} KB`;
    return `${(value / 1_048_576).toFixed(1)} MB`;
  }

  function formatTimestamp(value) {
    if (value === null) return 'No snapshot yet';
    return new Date(value).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  const STATUS_LABELS = Object.freeze({
    available: 'Available',
    missing: 'Not created yet',
    unavailable: 'Unavailable',
  });
  const JOB_STATUS_LABELS = Object.freeze({
    success: 'Success',
    degraded: 'Degraded',
    unavailable: 'Unavailable',
  });
  const DATASET_LABELS = Object.freeze({
    players: 'Player catalog',
    injuries: 'Injuries',
    news: 'News',
    projections: 'Projections',
    adp: 'ADP',
    sleeper_players: 'Player catalog',
  });
  const VARIANT_LABELS = Object.freeze({
    catalog: 'Catalog',
    'catalog-season': 'Season catalog',
    weekly: 'Weekly',
    recent: 'Recent',
    'preseason-std': 'Standard',
    'preseason-half': 'Half PPR',
    'preseason-ppr': 'PPR',
    active: 'Active NFL players',
  });
  const SCORING_LABELS = Object.freeze({ STD: 'Standard', HALF: 'Half PPR', PPR: 'PPR' });

  function requiredElements(documentObj) {
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
    const result = {};
    identifiers.forEach((identifier) => {
      const node = documentObj?.getElementById?.(identifier);
      if (!node) throw new Error('Provider cache dashboard markup is unavailable.');
      result[identifier] = node;
    });
    return result;
  }

  function renderStats(stats, elements, documentObj) {
    const cache = stats.cache;
    elements['provider-cache-availability'].textContent = STATUS_LABELS[cache.status];
    elements['provider-cache-snapshot-count'].textContent = String(cache.snapshotCount);
    elements['provider-cache-record-count'].textContent = String(cache.recordCount);
    elements['provider-cache-size'].textContent = formatBytes(cache.sizeBytes);
    elements['provider-cache-latest'].textContent = formatTimestamp(cache.latestFetchedAt);
    const budget = stats.fantasyProsBudget;
    elements['provider-cache-budget'].textContent = budget.used === null
      ? `${STATUS_LABELS[budget.status]} · local daily safety limit ${budget.limit}`
      : `${budget.used} of ${budget.limit} locally reserved today · ${budget.remaining} remaining · ${budget.utcDate} UTC`;
    const items = cache.snapshots.map((snapshot) => {
      const item = documentObj.createElement('li');
      const freshness = snapshot.stale ? 'stale' : 'fresh';
      item.textContent = [
        snapshot.provider,
        DATASET_LABELS[snapshot.dataset],
        VARIANT_LABELS[snapshot.variant],
        `${snapshot.recordCount} records`,
        freshness,
        formatTimestamp(snapshot.fetchedAt),
      ].join(' · ');
      return item;
    });
    if (items.length === 0) {
      const item = documentObj.createElement('li');
      item.textContent = 'No provider snapshots are stored yet.';
      items.push(item);
    }
    elements['provider-cache-snapshots'].replaceChildren(...items);
  }

  function createProviderCachePanel(options = {}) {
    const documentObj = options.documentObj || globalScope.document;
    const elements = requiredElements(documentObj);
    const requestOptions = { fetchImpl: options.fetchImpl };
    let busy = false;

    function setBusy(value) {
      busy = value;
      elements['provider-cache-scoring'].disabled = value;
      elements['provider-cache-refresh'].disabled = value;
      elements['provider-cache-run'].disabled = value;
      elements['provider-cache-panel'].setAttribute('aria-busy', value ? 'true' : 'false');
    }

    function setStatus(message, kind) {
      elements['provider-cache-status'].textContent = message;
      elements['provider-cache-status'].className = `provider-cache-status ${kind}`;
    }

    async function refreshStats() {
      if (busy) return null;
      setBusy(true);
      setStatus('Loading provider cache stats…', 'loading');
      try {
        const stats = await fetchProviderCacheStats(requestOptions);
        renderStats(stats, elements, documentObj);
        setStatus(
          stats.status === 'success'
            ? 'Provider cache stats updated.'
            : 'Provider cache stats loaded with missing or unavailable data.',
          stats.status === 'success' ? 'success' : 'warning',
        );
        return stats;
      } catch (_error) {
        setStatus(
          'Provider cache stats could not be loaded. Check that the local server is running and try again.',
          'error',
        );
        return null;
      } finally {
        setBusy(false);
      }
    }

    async function runJob() {
      if (busy) return null;
      const scoring = elements['provider-cache-scoring'].value;
      if (!SCORING.has(scoring)) {
        setStatus('Choose a supported scoring mode before running the provider cache job.', 'error');
        return null;
      }
      setBusy(true);
      setStatus(`Running the ${SCORING_LABELS[scoring]} provider cache job…`, 'loading');
      try {
        const result = await runProviderCacheJob(scoring, requestOptions);
        renderStats(result.stats, elements, documentObj);
        elements['provider-cache-job-summary'].textContent = [
          `FantasyPros: ${JOB_STATUS_LABELS[result.providers.fantasyPros.status]}`,
          `Sleeper: ${JOB_STATUS_LABELS[result.providers.sleeper.status]}`,
          SCORING_LABELS[result.scoring],
          `Season ${result.season}`,
        ].join(' · ');
        setStatus(
          result.status === 'success'
            ? 'Provider cache job completed.'
            : 'Provider cache job completed with stale, missing, or unavailable data.',
          result.status === 'success' ? 'success' : 'warning',
        );
        return result;
      } catch (_error) {
        setStatus(
          'Provider cache job could not be completed. Wait for any current job to finish, then try again.',
          'error',
        );
        return null;
      } finally {
        setBusy(false);
      }
    }

    elements['provider-cache-refresh'].addEventListener('click', () => { void refreshStats(); });
    elements['provider-cache-run'].addEventListener('click', () => { void runJob(); });
    const ready = refreshStats();
    return { ready, refreshStats, runJob };
  }

  const api = {
    createProviderCachePanel,
    fetchProviderCacheStats,
    runProviderCacheJob,
    validateProviderCacheJobResult,
    validateProviderCacheStats,
  };
  globalScope.YahooDraftProviderCache = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
