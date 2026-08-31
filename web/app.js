/* Trouveur — logique de l'interface. Aucun framework, aucune dépendance. */

'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  genres: new Map(),        // id -> nom
  genreChoice: new Map(),   // id -> 'in' | 'out'
  genreMode: 'ou',
  providers: new Set(),     // plateformes retenues ('plex' ou un id TMDB)
  decade: null,
  tracker: { enabled: false, name: '' },
  torrentCache: new Map(),  // id film -> résultat tracker
  seen: new Set(),          // ids TMDB des films déjà vus
  watchlist: new Set(),     // ids TMDB des films à voir
  ignored: new Set(),       // films écartés, plus jamais proposés
  mode: 'decouverte',       // decouverte | foryou | recherche | similaires | watchlist | seen | ignored
  searchQuery: '',
  similarTo: null,      // {id, title} quand on explore les films proches
  plex: { enabled: false },
  deluge: { enabled: false },
  avail: new Map(),         // id TMDB -> où regarder le film (Plex, plateformes)
  lastMovies: [],
  query: null,          // requête figée au lancement de la recherche
  nextPage: 1,
  hasMore: false,
  loading: false,
  hiddenSeen: 0,
  filteredOut: 0,
  totalResults: 0,
  shownIds: new Set(),  // évite les doublons entre pages
  queryId: 0,           // incrémenté à chaque nouvelle recherche
  // Une panne côté TMDB ne doit pas déclencher une rafale d'appels : le
  // défilement automatique s'arrête, le bouton reste pour réessayer à la main.
  autoPause: false,
};

/* ------------------------------ utilitaires ------------------------------ */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function formatSize(bytes) {
  if (!bytes) return '—';
  const units = ['o', 'Ko', 'Mo', 'Go', 'To'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value < 10 && index > 1 ? 1 : 0).replace('.', ',')} ${units[index]}`;
}

function formatRuntime(minutes) {
  if (!minutes) return null;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} h ${String(m).padStart(2, '0')}` : `${m} min`;
}

function formatNumber(value) {
  return (value || 0).toLocaleString('fr-FR');
}

function formatMoney(value) {
  if (!value) return null;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1).replace('.', ',')} Md $`;
  if (value >= 1e6) return `${Math.round(value / 1e6)} M$`;
  return `${formatNumber(value)} $`;
}

/** Résout un chemin d'API relativement à la page.
 *
 *  Sous l'ingress Home Assistant l'application n'est pas servie à la racine du
 *  domaine : une URL commençant par « / » viserait Home Assistant lui-même.
 */
function apiUrl(path) {
  return new URL(path.replace(/^\//, ''), document.baseURI).href;
}

async function api(path, options) {
  const response = await fetch(apiUrl(path), options);
  const payload = await response.json().catch(() => ({ error: 'Réponse illisible du serveur' }));
  if (!response.ok) throw new Error(payload.error || `Erreur ${response.status}`);
  return payload;
}

function showNotice(message, isError) {
  const box = $('notice');
  if (!message) { box.hidden = true; return; }
  box.textContent = message;
  box.className = isError ? 'notice is-error' : 'notice';
  box.hidden = false;
}

/* ------------------------------ démarrage -------------------------------- */

async function bootstrap() {
  buildDecades();
  bindEvents();
  syncRangeLabels();

  try {
    const data = await api('/api/bootstrap');
    state.tracker = data.tracker;
    state.seen = new Set(data.seen || []);
    state.watchlist = new Set(data.watchlist || []);
    state.ignored = new Set(data.ignored || []);
    state.plex = data.plex || { enabled: false };
    state.deluge = data.deluge || { enabled: false };
    data.genres.forEach((g) => state.genres.set(g.id, g.name));
    buildGenreChips(data.genres);
    buildProviders();
    renderTrackerStatus();
    renderSeenCount();
    renderTabCounts();

    // Sans clé TMDB rien ne peut fonctionner : on ouvre directement l'écran de
    // configuration plutôt que d'afficher une grille vide et inexplicable.
    if (data.configured === false) openSettings();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderTrackerStatus() {
  const box = $('tracker-status');
  box.hidden = false;
  box.classList.toggle('is-on', state.tracker.enabled);
  $('tracker-status-label').textContent = state.tracker.enabled
    ? `${state.tracker.name} connecté`
    : `${state.tracker.name} non configuré`;
}

/* ----------------------------- films déjà vus ---------------------------- */

function renderSeenCount() {
  const count = state.seen.size;
  $('seen-count').textContent = count
    ? `${formatNumber(count)} film${count > 1 ? 's' : ''} marqué${count > 1 ? 's' : ''} comme vu${count > 1 ? 's' : ''}.`
    : 'Aucun film marqué pour l’instant.';
}

/** Bascule l'état « vu » d'un film et met à jour toutes ses vues à l'écran. */
async function toggleSeen(movie) {
  const wasSeen = state.seen.has(movie.id);
  // Bascule optimiste : le serveur est local, l'aller-retour est immédiat,
  // mais on rétablit l'état si l'écriture échoue.
  state.seen[wasSeen ? 'delete' : 'add'](movie.id);
  paintSeen(movie.id);
  renderSeenCount();

  try {
    if (wasSeen) {
      await api(`/api/seen/${movie.id}`, { method: 'DELETE' });
    } else {
      await api('/api/seen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: movie.id, title: movie.title, year: movie.year, poster: movie.poster,
        }),
      });
    }
  } catch (error) {
    state.seen[wasSeen ? 'add' : 'delete'](movie.id);
    paintSeen(movie.id);
    renderSeenCount();
    showNotice(`Impossible d’enregistrer : ${error.message}`, true);
  }
}

/** Applique l'état « vu » aux cartes et à la fiche ouverte pour ce film. */
function paintSeen(movieId) {
  const isSeen = state.seen.has(movieId);

  [...$('grid').children].forEach((card) => {
    if (!card._movie || card._movie.id !== movieId) return;
    card.classList.toggle('is-seen', isSeen);
    const button = card.querySelector('.card-seen');
    button.setAttribute('aria-pressed', String(isSeen));
    button.title = isSeen ? 'Marqué comme vu — cliquer pour annuler' : 'Marquer comme vu';
  });

  const sheetButton = document.querySelector('.sheet-seen');
  if (sheetButton && Number(sheetButton.dataset.id) === movieId) {
    sheetButton.classList.toggle('is-on', isSeen);
    sheetButton.setAttribute('aria-pressed', String(isSeen));
    sheetButton.querySelector('.sheet-seen-text').textContent = isSeen
      ? 'Déjà vu' : 'Marquer comme vu';
  }
}

/* ------------------------------ liste à voir ----------------------------- */

function renderTabCounts() {
  $('count-watchlist').textContent = state.watchlist.size || '';
  $('count-ignored').textContent = state.ignored.size || '';
  $('count-seen').textContent = state.seen.size || '';
}

/** Bascule « à voir ». Même mécanique optimiste que le marquage « vu ». */
async function toggleWant(movie) {
  const wanted = state.watchlist.has(movie.id);
  state.watchlist[wanted ? 'delete' : 'add'](movie.id);
  paintWant(movie.id);
  renderTabCounts();

  try {
    if (wanted) {
      await api(`/api/watchlist/${movie.id}`, { method: 'DELETE' });
    } else {
      await api('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: movie.id, title: movie.title, year: movie.year, poster: movie.poster,
        }),
      });
    }
  } catch (error) {
    state.watchlist[wanted ? 'add' : 'delete'](movie.id);
    paintWant(movie.id);
    renderTabCounts();
    showNotice(`Impossible d’enregistrer : ${error.message}`, true);
  }
}

function paintWant(movieId) {
  const wanted = state.watchlist.has(movieId);

  [...$('grid').children].forEach((card) => {
    if (!card._movie || card._movie.id !== movieId) return;
    const button = card.querySelector('.card-want');
    button.classList.toggle('is-on', wanted);
    button.setAttribute('aria-pressed', String(wanted));
    button.title = wanted ? 'Dans ta liste à voir — cliquer pour retirer' : 'Ajouter à ma liste à voir';
  });

  const sheetButton = document.querySelector('.sheet-want');
  if (sheetButton && Number(sheetButton.dataset.id) === movieId) {
    sheetButton.classList.toggle('is-on', wanted);
    sheetButton.setAttribute('aria-pressed', String(wanted));
    sheetButton.querySelector('.sheet-want-text').textContent = wanted
      ? 'Dans ma liste' : 'À voir';
  }
}

/* -------------------------------- ignorés -------------------------------- */

/** Écarte un film des propositions, ou revient sur cette décision. */
async function toggleIgnore(movie) {
  const ignore = state.ignored.has(movie.id);
  state.ignored[ignore ? 'delete' : 'add'](movie.id);
  renderTabCounts();

  // Un film qu'on vient d'écarter n'a plus rien à faire dans la grille ; dans
  // l'onglet des ignorés c'est l'inverse, on retire celui qu'on réhabilite.
  const carte = [...$('grid').children].find((c) => c._movie && c._movie.id === movie.id);
  if (carte) {
    carte.remove();
    state.lastMovies = state.lastMovies.filter((m) => m.id !== movie.id);
    renderMeta();
  }
  if (!$('sheet-backdrop').hidden) closeSheet();

  try {
    if (ignore) {
      await api(`/api/ignored/${movie.id}`, { method: 'DELETE' });
    } else {
      await api('/api/ignored', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: movie.id, title: movie.title, year: movie.year, poster: movie.poster,
        }),
      });
    }
  } catch (error) {
    state.ignored[ignore ? 'add' : 'delete'](movie.id);
    renderTabCounts();
    showNotice(`Impossible d’enregistrer : ${error.message}`, true);
  }
}

/* --------------------------- modes d'affichage --------------------------- */

const TITRES = {
  decouverte: 'Vos propositions',
  foryou: 'Pour toi',
  cinema: 'Au cinéma',
  recherche: 'Résultats de recherche',
  watchlist: 'À voir',
  ignored: 'Ignorés',
  seen: 'Déjà vus',
};

function setMode(mode) {
  state.mode = mode;
  if (mode === 'recherche') {
    $('results-title').textContent = `Résultats pour « ${state.searchQuery} »`;
  } else if (mode === 'similaires') {
    $('results-title').textContent = `Dans le même esprit que « ${state.similarTo.title} »`;
  } else {
    $('results-title').textContent = TITRES[mode];
  }
  $('back-to-criteria').hidden = mode === 'decouverte';
  [...$('tabs').children].forEach((tab) => {
    tab.classList.toggle('on', tab.dataset.view === mode);
  });
}

/** Vide la grille et remet la pagination à zéro, sans changer les critères. */
function resetGrid() {
  state.queryId += 1;
  state.nextPage = 1;
  state.hasMore = false;
  state.hiddenSeen = 0;
  state.filteredOut = 0;
  state.totalResults = 0;
  state.shownIds.clear();
  state.lastMovies = [];
  state.autoPause = false;
  $('grid').textContent = '';
  showNotice(null);
}

async function runTitleSearch(query) {
  const clean = query.trim();
  if (!clean) return;
  state.searchQuery = clean;
  resetGrid();
  setMode('recherche');
  const added = await fetchPage();
  if (!added) {
    showNotice(`Aucun film ne correspond à « ${clean} ».`, false);
  }
}

/** Suggestions bâties sur l'historique. */
async function showForYou() {
  state.query = buildQuery();
  resetGrid();
  setMode('foryou');
  $('results-meta').textContent = 'Analyse de tes films vus…';
  const added = await fetchPage();
  if (!added) {
    showNotice(
      'Pas encore assez de films vus pour te proposer quelque chose. Marque-en quelques-uns.',
      false,
    );
  }
}

/** Les sorties en salle des dernières semaines, dans ta région. */
async function showCinema() {
  state.query = buildQuery();
  resetGrid();
  setMode('cinema');
  $('results-meta').textContent = 'Sorties en salle…';
  const added = await fetchPage();
  if (!added) {
    showNotice('Aucune sortie ne correspond à tes filtres sur cette période.', false);
  }
}

/** Ouvre la grille sur les films proches de celui-ci. */
async function showSimilar(movie) {
  state.similarTo = { id: movie.id, title: movie.title };
  closeSheet();
  resetGrid();
  setMode('similaires');
  window.scrollTo(0, 0);
  const added = await fetchPage();
  if (!added) {
    showNotice(`TMDB ne propose aucun film proche de « ${movie.title} ».`, false);
  }
}

/** Affiche une liste enregistrée (à voir / déjà vus) : pas de pagination. */
async function showList(mode) {
  resetGrid();
  setMode(mode);
  // La première ouverture reconstitue les cartes depuis TMDB : sur une longue
  // liste cela prend quelques secondes, il faut le dire.
  $('results-meta').textContent = 'Chargement des fiches…';
  try {
    const route = { watchlist: '/api/watchlist', ignored: '/api/ignored' }[mode] || '/api/seen';
    const data = await api(route);
    const movies = data.movies || [];
    movies.forEach((m) => state.shownIds.add(m.id));
    state.lastMovies = movies;
    state.totalResults = data.count || movies.length;
    appendCards(movies);
    $('results-meta').textContent = movies.length
      ? `${formatNumber(movies.length)} film${movies.length > 1 ? 's' : ''}`
      : '';
    $('empty').hidden = movies.length > 0;
    if (!movies.length) {
      showNotice({
        watchlist: 'Ta liste à voir est vide. Le signet sur une affiche y ajoute un film.',
        ignored: 'Aucun film ignoré. La croix sur une affiche écarte un film des propositions.',
      }[mode] || 'Aucun film marqué comme vu pour l’instant.', false);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
  renderMoreState();
}

async function buildProviders() {
  const box = $('providers');
  let data;
  try {
    data = await api('/api/providers');
  } catch (error) {
    box.appendChild(el('p', 'muted', 'Plateformes indisponibles.'));
    return;
  }

  (data.providers || []).forEach((provider) => {
    const chip = el('button', `provider${provider.local ? ' is-local' : ''}`);
    chip.type = 'button';
    chip.dataset.id = provider.id;
    chip.title = provider.name;
    if (provider.logo) {
      const logo = document.createElement('img');
      logo.src = provider.logo;
      logo.alt = '';
      logo.loading = 'lazy';
      chip.appendChild(logo);
    }
    chip.appendChild(el('span', 'provider-name', provider.name));
    chip.addEventListener('click', () => {
      const id = String(provider.id);
      if (state.providers.has(id)) state.providers.delete(id);
      else state.providers.add(id);
      chip.classList.toggle('is-on', state.providers.has(id));
      renderFiltersCount();
      if (state.mode === 'foryou') refreshForYouSoon();
    });
    box.appendChild(chip);
  });
}

function buildGenreChips(genres) {
  const box = $('genres');
  box.textContent = '';
  genres.forEach((genre) => {
    const chip = el('button', 'chip', genre.name);
    chip.type = 'button';
    chip.dataset.id = genre.id;
    chip.addEventListener('click', () => cycleGenre(genre.id, chip));
    box.appendChild(chip);
  });
}

function cycleGenre(id, chip) {
  const current = state.genreChoice.get(id);
  if (!current) {
    state.genreChoice.set(id, 'in');
    chip.className = 'chip is-in';
  } else if (current === 'in') {
    state.genreChoice.set(id, 'out');
    chip.className = 'chip is-out';
  } else {
    state.genreChoice.delete(id);
    chip.className = 'chip';
  }
  const included = [...state.genreChoice.values()].filter((v) => v === 'in').length;
  $('genre-mode').hidden = included < 2;

  // En mode « Pour toi » le panneau n'a pas de bouton de validation : le
  // changement doit prendre effet tout de suite.
  if (state.mode === 'foryou') refreshForYouSoon();
  renderFiltersCount();
}

let refreshTimer = null;

/** Regroupe les clics rapprochés en une seule requête.
 *
 * Passer une pastille de neutre à « exclu » demande deux clics : sans ce
 * regroupement, la requête du premier clic est celle qui s'affiche, et le
 * filtre semble faire l'inverse de ce qu'on lui demande.
 */
function refreshForYouSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => showForYou(), 300);
}

function buildDecades() {
  const box = $('decades');
  const decades = [];
  const currentDecade = Math.floor(new Date().getFullYear() / 10) * 10;
  for (let year = currentDecade; year >= 1930; year -= 10) {
    decades.push(year);
  }
  decades.forEach((year) => {
    const chip = el('button', 'chip', year >= 2000 ? `${year}s` : `${String(year).slice(2)}s`);
    chip.type = 'button';
    chip.title = `${year} – ${year + 9}`;
    chip.addEventListener('click', () => {
      const active = state.decade === year;
      state.decade = active ? null : year;
      [...box.children].forEach((c) => c.classList.remove('is-in'));
      if (!active) {
        chip.classList.add('is-in');
        $('year-min').value = year;
        $('year-max').value = year + 9;
      } else {
        $('year-min').value = '';
        $('year-max').value = '';
      }
    });
    box.appendChild(chip);
  });
}

function bindEvents() {
  $('criteria').addEventListener('submit', (event) => {
    event.preventDefault();
    closeFilters();          // on vient de demander des résultats : on les montre
    runSearch();
  });

  $('reset').addEventListener('click', resetCriteria);

  $('open-filters').addEventListener('click', openFilters);
  $('close-filters').addEventListener('click', closeFilters);
  $('panel-veil').addEventListener('click', closeFilters);
  $('criteria').addEventListener('input', renderFiltersCount);
  $('criteria').addEventListener('change', renderFiltersCount);

  // En mode « Pour toi » le panneau n'a pas de bouton de validation : tout
  // changement de critère doit prendre effet de lui-même.
  $('criteria').addEventListener('input', () => {
    if (state.mode === 'foryou') refreshForYouSoon();
  });
  $('criteria').addEventListener('change', () => {
    if (state.mode === 'foryou') refreshForYouSoon();
  });

  $('genre-mode').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    state.genreMode = button.dataset.mode;
    [...event.currentTarget.children].forEach((c) => c.classList.toggle('on', c === button));
    if (state.mode === 'foryou') refreshForYouSoon();
  });

  ['rating-min', 'runtime-max'].forEach((id) => {
    $(id).addEventListener('input', syncRangeLabels);
  });

  // Saisir une année à la main annule la décennie présélectionnée.
  ['year-min', 'year-max'].forEach((id) => {
    $(id).addEventListener('input', () => {
      state.decade = null;
      [...$('decades').children].forEach((c) => c.classList.remove('is-in'));
    });
  });

  $('search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    runTitleSearch($('search-input').value);
  });

  $('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    const vue = tab.dataset.view;
    if (vue === 'decouverte') { runSearch(); }
    else if (vue === 'foryou') { showForYou(); }
    else if (vue === 'cinema') { showCinema(); }
    else { showList(vue); }
  });

  $('back-to-criteria').addEventListener('click', () => runSearch());

  $('more-button').addEventListener('click', () => {
    state.autoPause = false;   // c'est un geste délibéré
    loadMore();
  });

  // Défilement infini, par deux voies volontairement redondantes : l'observateur
  // est le mécanisme efficace, le défilement écouté prend le relais là où il est
  // indisponible ou suspendu. Le bouton reste le recours accessible au clavier.
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(
      (entries) => { if (entries.some((e) => e.isIntersecting)) maybeLoadMore(); },
      { rootMargin: '600px' },
    ).observe($('more-sentinel'));
  }

  let scrollPending = false;
  window.addEventListener('scroll', () => {
    if (scrollPending) return;
    scrollPending = true;
    setTimeout(() => { scrollPending = false; maybeLoadMore(); }, 150);
  }, { passive: true });

  $('open-settings').addEventListener('click', openSettings);
  $('settings-close').addEventListener('click', closeSettings);
  $('settings-form').addEventListener('submit', saveSettings);
  $('plex-connect').addEventListener('click', plexConnect);
  $('plex-sync-now').addEventListener('click', plexSyncNow);
  $('plex-refresh-now').addEventListener('click', plexRefreshNow);
  $('deluge-test').addEventListener('click', delugeTest);
  ROLES_CERT.forEach((role) => {
    $(`cert-${role}-file`).addEventListener('change', (e) => deposerCertificat(role, e.target));
  });
  $('set-import').addEventListener('change', importerFichiers);
  $('settings-backdrop').addEventListener('click', (event) => {
    if (event.target === $('settings-backdrop')) closeSettings();
  });

  $('sheet-close').addEventListener('click', closeSheet);
  $('sheet-backdrop').addEventListener('click', (event) => {
    if (event.target === $('sheet-backdrop')) closeSheet();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('sheet-backdrop').hidden) closeSheet();
    else if (!$('settings-backdrop').hidden) closeSettings();
    else if ($('panel').classList.contains('is-open')) closeFilters();
  });
}

function syncRangeLabels() {
  const rating = parseFloat($('rating-min').value);
  $('rating-min-out').textContent = rating === 0 ? 'aucune' : rating.toFixed(1).replace('.', ',');

  const runtime = parseInt($('runtime-max').value, 10);
  $('runtime-max-out').textContent = runtime >= 245 ? 'peu importe' : formatRuntime(runtime);
}

function resetCriteria() {
  state.genreChoice.clear();
  state.decade = null;
  state.genreMode = 'ou';
  [...$('genres').children].forEach((c) => { c.className = 'chip'; });
  [...$('decades').children].forEach((c) => c.classList.remove('is-in'));
  $('genre-mode').hidden = true;
  $('year-min').value = '';
  $('year-max').value = '';
  $('rating-min').value = 6.5;
  $('runtime-max').value = 245;
  $('votes-min').value = '300';
  $('original-language').value = '';
  $('keyword').value = '';
  $('sort').value = 'note';
  $('hide-seen').checked = false;
  state.providers.clear();
  [...$('providers').children].forEach((c) => c.classList.remove('is-on'));
  syncRangeLabels();
  renderFiltersCount();
  showNotice(null);
}

/* ------------------------------- recherche ------------------------------- */

function buildQuery() {
  const params = new URLSearchParams();
  const included = [];
  const excluded = [];
  state.genreChoice.forEach((choice, id) => {
    (choice === 'in' ? included : excluded).push(id);
  });
  if (included.length) params.set('genres', included.join(','));
  if (excluded.length) params.set('exclude_genres', excluded.join(','));
  if (included.length > 1) params.set('genre_mode', state.genreMode);

  const yearMin = $('year-min').value.trim();
  const yearMax = $('year-max').value.trim();
  if (yearMin) params.set('year_min', yearMin);
  if (yearMax) params.set('year_max', yearMax);

  const rating = parseFloat($('rating-min').value);
  if (rating > 0) params.set('rating_min', rating);

  const runtime = parseInt($('runtime-max').value, 10);
  if (runtime < 245) params.set('runtime_max', runtime);

  params.set('votes_min', $('votes-min').value);

  const language = $('original-language').value;
  if (language) params.set('original_language', language);

  const keyword = $('keyword').value.trim();
  if (keyword) params.set('keyword', keyword);

  if (state.providers.size) {
    params.set('providers', [...state.providers].join(','));
  }
  params.set('sort', $('sort').value);
  if ($('hide-seen').checked) params.set('hide_seen', '1');
  params.set('limit', '20');
  return params;
}

/** Nouvelle recherche : on repart d'une grille vide. */
async function runSearch() {
  // La requête est figée ici : modifier les critères ensuite ne doit pas
  // changer ce que « afficher plus » va chercher.
  state.query = buildQuery();
  resetGrid();
  setMode('decouverte');
  $('search-input').value = '';

  const button = $('submit');
  button.disabled = true;
  button.textContent = 'Recherche…';
  try {
    const added = await fetchPage();
    if (!added && $('notice').hidden) {
      showNotice('Aucun film ne correspond à ces critères. Essayez d’élargir la période ou de baisser la note minimum.', false);
    }
  } finally {
    button.disabled = false;
    button.textContent = 'Proposer des films';
  }
}

/** Charge une page et l'ajoute à la grille. Renvoie le nombre de films ajoutés. */
async function fetchPage() {
  if (state.loading) return 0;
  state.loading = true;
  renderMoreState();
  const queryId = state.queryId;

  try {
    let route;
    if (state.mode === 'recherche') {
      route = `/api/search?q=${encodeURIComponent(state.searchQuery)}&page=${state.nextPage}`;
    } else if (state.mode === 'similaires') {
      route = `/api/similar?id=${state.similarTo.id}&page=${state.nextPage}`;
    } else if (state.mode === 'cinema') {
      const params = new URLSearchParams(state.query);
      // Note et popularité minimales n'ont pas de sens sur des films tout
      // juste sortis, et le tri est celui de l'onglet : par date.
      ['sort', 'rating_min', 'votes_min', 'year_min', 'year_max', 'keyword']
        .forEach((cle) => params.delete(cle));
      params.set('page', state.nextPage);
      route = `/api/cinema?${params}`;
    } else if (state.mode === 'foryou') {
      const params = new URLSearchParams(state.query);
      // Le tri est le seul critère écarté : l'onglet a son propre ordre.
      params.delete('sort');
      params.delete('limit');
      params.delete('keyword');
      params.delete('hide_seen');
      params.set('page', state.nextPage);
      route = `/api/foryou?${params}`;
    } else {
      const params = new URLSearchParams(state.query);
      params.set('page', state.nextPage);
      route = `/api/discover?${params}`;
    }
    const data = await api(route);

    // Une recherche plus récente a été lancée pendant l'attente : cette
    // réponse est périmée et ne doit pas repeindre la grille.
    if (queryId !== state.queryId) return 0;

    if (data.notice) showNotice(data.notice, false);

    // Le tirage au hasard peut retomber sur une page déjà vue, et les pages
    // successives se chevauchent parfois : on ne garde que l'inédit.
    const fresh = (data.movies || []).filter((movie) => !state.shownIds.has(movie.id));
    fresh.forEach((movie) => state.shownIds.add(movie.id));

    state.lastMovies = state.lastMovies.concat(fresh);
    state.hiddenSeen += data.hidden_seen || 0;
    state.filteredOut = data.filtered_out || 0;
    state.totalResults = data.total_results || 0;
    state.hasMore = Boolean(data.has_more);
    state.nextPage = data.next_page || state.nextPage + 1;
    state.autoPause = false;
    const sautees = data.skipped_pages || [];
    if (sautees.length) {
      // Le trou est réel : mieux vaut l'annoncer que de laisser croire à une
      // liste complète.
      showNotice(`TMDB n’a pas répondu sur ${sautees.length} page`
        + `${sautees.length > 1 ? 's' : ''} de résultats : ces films-là manquent.`,
        false);
    }

    appendCards(fresh);
    renderMeta();
    return fresh.length;
  } catch (error) {
    showNotice(error.message, true);
    // On garde « Afficher plus » : l'incident est passager, et une rafale
    // automatique ne ferait qu'insister sur un service déjà en peine.
    state.autoPause = true;
    return 0;
  } finally {
    state.loading = false;
    renderMoreState();
  }
}

/** La sentinelle est-elle dans le viewport, marge d'anticipation comprise ? */
function sentinelInView(margin = 600) {
  const box = $('more');
  if (box.hidden) return false;
  const rect = $('more-sentinel').getBoundingClientRect();
  return rect.top - margin < window.innerHeight && rect.bottom + margin > 0;
}

/** Déclencheur commun à l'observateur, au défilement et à l'après-chargement. */
function maybeLoadMore() {
  if (!state.hasMore || state.loading || state.autoPause) return;
  if (!sentinelInView()) return;
  loadMore();
}

async function loadMore(depth = 0) {
  if (!state.hasMore || state.loading) return;

  const added = await fetchPage();

  // Une page entièrement composée de doublons ne doit pas figer le défilement :
  // on retente une fois, puis on s'arrête.
  if (added === 0 && state.hasMore) {
    const second = await fetchPage();
    if (second === 0) {
      state.hasMore = false;
      renderMoreState();
      return;
    }
  }

  // L'observateur ne se redéclenche pas tant que l'intersection ne change pas.
  // Si la sentinelle est encore visible, la page reste trop courte : on
  // enchaîne, en bornant la cascade pour ne pas emballer les appels.
  if (depth < 4 && state.hasMore && sentinelInView(0)) {
    await loadMore(depth + 1);
  }
}

function renderMeta() {
  const parts = [];
  if (state.totalResults) {
    parts.push(`${state.lastMovies.length} propositions parmi ${formatNumber(state.totalResults)} films correspondants`);
  } else if (state.lastMovies.length) {
    // Total inconnu : le filtre par plateforme s'applique après la requête,
    // le compte de TMDB ne correspondrait plus à ce qui est montré.
    parts.push(`${state.lastMovies.length} propositions`);
  }
  if (state.hiddenSeen) {
    parts.push(`${state.hiddenSeen} déjà vu${state.hiddenSeen > 1 ? 's' : ''} masqué${state.hiddenSeen > 1 ? 's' : ''}`);
  }
  if (state.filteredOut) {
    parts.push(`${state.filteredOut} écartée${state.filteredOut > 1 ? 's' : ''} par tes filtres`);
  }
  if (state.mode === 'cinema') {
    // Dire ce qui ne s'applique pas vaut mieux que laisser croire à un bug.
    parts.push('sorties des 45 derniers jours, les plus récentes d’abord'
      + '  ·  note et popularité minimales ignorées ici');
  }
  $('results-meta').textContent = parts.join('  ·  ');
  $('empty').hidden = state.lastMovies.length > 0;
}

function renderMoreState() {
  const box = $('more');
  const button = $('more-button');
  const label = $('more-state');

  box.hidden = state.lastMovies.length === 0;
  button.hidden = !state.hasMore;
  button.disabled = state.loading;
  button.textContent = state.loading ? 'Chargement…' : 'Afficher plus';

  if (state.loading) {
    label.textContent = '';
  } else if (!state.hasMore && state.lastMovies.length) {
    label.textContent = 'Tu as vu toutes les propositions pour ces critères.';
  } else {
    label.textContent = '';
  }
}

function appendCards(movies) {
  const grid = $('grid');
  const template = $('tpl-card');
  const nouvelles = [];

  movies.forEach((movie) => {
    const card = template.content.firstElementChild.cloneNode(true);
    const image = card.querySelector('img');
    if (movie.poster) {
      image.src = movie.poster;
      image.alt = `Affiche de ${movie.title}`;
      image.addEventListener('load', () => image.classList.add('is-loaded'));
    } else {
      image.remove();
    }

    const rating = card.querySelector('.card-rating');
    if (movie.votes > 0) {
      rating.textContent = movie.rating.toFixed(1).replace('.', ',');
      // classList.add('') lève une exception : sous 6,5 il faut une vraie classe,
      // sans quoi le rendu de toute la grille s'interrompt à ce film.
      rating.classList.add(
        movie.rating >= 7.5 ? 'is-high' : movie.rating >= 6.5 ? 'is-good' : 'is-low',
      );
    } else {
      rating.textContent = '—';
      rating.classList.add('is-none');
    }

    card.querySelector('.card-title').textContent = movie.title;
    const genreNames = (movie.genre_ids || [])
      .map((id) => state.genres.get(id))
      .filter(Boolean)
      .slice(0, 2)
      .join(' · ');
    card.querySelector('.card-sub').textContent = [movie.year, genreNames].filter(Boolean).join('  ·  ');

    if (movie.because && movie.because.length) {
      const why = el('p', 'card-why');
      why.textContent = `d'après ${movie.because.slice(0, 2).join(', ')}`;
      why.title = `Recommandé à partir de : ${movie.because.join(', ')}`;
      card.querySelector('.card-body').appendChild(why);
    }

    const badge = card.querySelector('.card-torrent');
    if (!state.tracker.enabled) badge.classList.add('is-hidden');
    if (!state.tracker.enabled) badge.classList.add('is-hidden');

    const seenButton = card.querySelector('.card-seen');
    seenButton.addEventListener('click', (event) => {
      event.stopPropagation();   // sans cela, la fiche s'ouvre aussi
      toggleSeen(movie);
    });

    const wantButton = card.querySelector('.card-want');
    wantButton.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleWant(movie);
    });

    const hideButton = card.querySelector('.card-hide');
    hideButton.title = state.ignored.has(movie.id)
      ? 'Ne plus ignorer ce film' : 'Ne plus me proposer ce film';
    hideButton.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleIgnore(movie);
    });

    card.addEventListener('click', () => openSheet(movie.id));
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openSheet(movie.id); }
    });

    card._movie = movie;
    grid.appendChild(card);
    nouvelles.push(card);
    paintSeen(movie.id);
    paintWant(movie.id);
    paintAvailability(card);
  });

  // Seules les nouvelles cartes sont interrogées : les précédentes le sont déjà.
  if (state.tracker.enabled) queueTorrentChecks(nouvelles);
  loadAvailability(movies);
}

/* ---------------------------------- Plex --------------------------------- */

/** Une seule requête pour toute la grille : où regarder chacun de ces films. */
async function loadAvailability(movies) {
  if (!movies.length) return;
  const params = new URLSearchParams();
  movies.forEach((movie) => {
    params.append('id', movie.id);
    params.append('title', movie.title || '');
    params.append('year', movie.year || '');
  });

  let data;
  try {
    data = await api(`/api/availability?${params}`);
  } catch (error) {
    return;   // l'absence de badge n'empêche pas d'utiliser la grille
  }

  Object.entries(data.items || {}).forEach(([id, info]) => {
    state.avail.set(Number(id), info);
  });
  [...$('grid').children].forEach((card) => paintAvailability(card));
}

/** Dessine les badges « où regarder » : le serveur Plex d'abord, puis les
 *  plateformes. Plex passe en premier : c'est disponible tout de suite et sans
 *  abonnement supplémentaire. */
function paintAvailability(card) {
  const box = card.querySelector('.card-avail');
  if (!box) return;
  const info = card._movie && state.avail.get(card._movie.id);
  box.textContent = '';
  if (!info) return;

  if (info.plex) {
    const badge = el('span', 'avail-badge is-plex',
      info.plex.resolution ? `Plex · ${info.plex.resolution}` : 'Plex');
    badge.title = 'Déjà sur ton serveur Plex';
    box.appendChild(badge);
  }

  (info.providers || []).forEach((provider) => {
    const badge = el('span', `avail-badge${provider.logo ? ' is-logo' : ''}`);
    badge.title = `Inclus avec ${provider.name}`;
    if (provider.logo) {
      const logo = document.createElement('img');
      logo.alt = provider.name;
      // Un logo qui n'arrive pas laisserait un carré vide, indéchiffrable :
      // on retombe alors sur le nom du service.
      logo.addEventListener('error', () => {
        badge.classList.remove('is-logo');
        badge.textContent = provider.name;
      });
      logo.src = provider.logo;
      badge.appendChild(logo);
    } else {
      badge.textContent = provider.name;
    }
    box.appendChild(badge);
  });
}

/* -------------------------------- torrents ------------------------------- */

function torrentUrl(movie) {
  // tmdb_id d'abord : le tracker sait apparier dessus, sans ambiguïté de titre.
  const params = new URLSearchParams({ title: movie.title, tmdb_id: movie.id });
  if (movie.year) params.set('year', movie.year);
  if (movie.original_title && movie.original_title !== movie.title) {
    params.set('original_title', movie.original_title);
  }
  return `/api/torrents?${params}`;
}

async function fetchTorrents(movie) {
  if (state.torrentCache.has(movie.id)) return state.torrentCache.get(movie.id);
  let result;
  try {
    result = await api(torrentUrl(movie));
  } catch (error) {
    result = { torrents: [], error: error.message };
  }
  state.torrentCache.set(movie.id, result);
  return result;
}

/** Interroge le tracker carte par carte, 3 requêtes en vol au maximum. */
function queueTorrentChecks(cards) {
  const pending = [...cards];
  const worker = async () => {
    while (pending.length) {
      const card = pending.shift();
      const result = await fetchTorrents(card._movie);
      paintBadge(card.querySelector('.card-torrent'), result);
    }
  };
  for (let i = 0; i < 3; i += 1) worker();
}

function paintBadge(badge, result) {
  badge.classList.remove('is-pending');
  if (result.disabled) { badge.classList.add('is-hidden'); return; }
  if (result.error) {
    badge.classList.add('is-no');
    badge.textContent = '!';
    badge.title = result.error;
    return;
  }
  const count = (result.torrents || []).length;
  if (count) {
    badge.classList.add('is-yes');
    badge.textContent = count >= 20 ? '20+' : String(count);
    badge.title = `${count} torrent${count > 1 ? 's' : ''} disponible${count > 1 ? 's' : ''}`;
  } else {
    badge.classList.add('is-no');
    badge.textContent = '0';
    badge.title = 'Aucun torrent trouvé';
  }
}

/* ---------------------------------- fiche -------------------------------- */

function closeSheet() {
  $('sheet-backdrop').hidden = true;
  $('sheet-body').textContent = '';   // coupe la lecture de la bande-annonce
  document.body.style.overflow = '';
}

async function openSheet(movieId) {
  const backdrop = $('sheet-backdrop');
  const body = $('sheet-body');
  backdrop.hidden = false;
  document.body.style.overflow = 'hidden';
  body.textContent = '';
  body.appendChild(el('div', 'empty', 'Chargement de la fiche…'));

  let movie;
  try {
    movie = await api(`/api/movie/${movieId}`);
  } catch (error) {
    body.textContent = '';
    const box = el('div', 'notice is-error', error.message);
    body.appendChild(box);
    return;
  }

  body.textContent = '';
  body.appendChild(renderSheet(movie));

  if (state.tracker.enabled) {
    const slot = $('torrent-slot');
    if (slot) {
      const result = await fetchTorrents(movie);
      slot.textContent = '';
      slot.appendChild(renderTorrents(result));
    }
  }

  await fillSaga(movie);
  await fillSimilar(movie);
}

async function fillSaga(movie) {
  const slot = $('saga-slot');
  if (!slot || !movie.collection) return;

  let data;
  try {
    data = await api(`/api/collection?id=${movie.collection.id}`);
  } catch (error) {
    slot.textContent = '';
    slot.appendChild(el('p', 'muted', `Saga indisponible : ${error.message}`));
    return;
  }
  if (!document.body.contains(slot)) return;

  const parts = (data.parts || []).filter((p) => p.id !== movie.id);
  slot.textContent = '';
  if (!parts.length) {
    slot.appendChild(el('p', 'muted', 'Aucun autre film dans cette saga.'));
    return;
  }

  const vus = parts.filter((p) => state.seen.has(p.id)).length;
  const total = parts.length + 1;                 // le film courant compte aussi
  const vusTotal = vus + (state.seen.has(movie.id) ? 1 : 0);
  slot.appendChild(el(
    'p',
    'muted saga-progress',
    `${total} films dans la saga — tu en as vu ${vusTotal}.`,
  ));
  slot.appendChild(renderSimilarRow(parts));
}

async function fillSimilar(movie) {
  const slot = $('similar-slot');
  if (!slot) return;
  let data;
  try {
    data = await api(`/api/similar?id=${movie.id}`);
  } catch (error) {
    slot.textContent = '';
    slot.appendChild(el('p', 'muted', `Films proches indisponibles : ${error.message}`));
    return;
  }
  // La fiche a pu être fermée ou changée pendant la requête.
  if (!document.body.contains(slot)) return;

  slot.textContent = '';
  const movies = (data.movies || []).filter((m) => m.id !== movie.id);
  if (!movies.length) {
    slot.appendChild(el('p', 'muted', 'TMDB ne propose aucun film proche de celui-ci.'));
    return;
  }

  slot.appendChild(renderSimilarRow(movies.slice(0, 10)));

  if (data.source === 'similarite') {
    slot.appendChild(el(
      'p',
      'muted similar-note',
      'Rapprochement par genres et mots-clés : trop peu de spectateurs pour une vraie recommandation.',
    ));
  }

  const more = el('button', 'btn btn-ghost similar-more', 'Voir tous les films dans le même esprit');
  more.type = 'button';
  more.addEventListener('click', () => showSimilar(movie));
  slot.appendChild(more);
}

function renderSimilarRow(movies) {
  const row = el('div', 'similar-row');
  movies.forEach((movie) => {
    const item = el('button', 'similar-item');
    item.type = 'button';
    if (state.seen.has(movie.id)) item.classList.add('is-seen');

    const poster = el('span', 'similar-poster');
    if (movie.poster) {
      const image = document.createElement('img');
      image.src = movie.poster;
      image.alt = '';
      image.loading = 'lazy';
      poster.appendChild(image);
    }
    if (movie.votes > 0) {
      poster.appendChild(el('span', 'similar-rating', movie.rating.toFixed(1).replace('.', ',')));
    }
    item.appendChild(poster);
    item.appendChild(el('span', 'similar-title', movie.title));
    item.appendChild(el('span', 'similar-year', movie.year ? String(movie.year) : ''));
    item.title = state.seen.has(movie.id) ? `${movie.title} — déjà vu` : movie.title;

    // Enchaîner d'une fiche à l'autre : c'est tout l'intérêt.
    item.addEventListener('click', () => openSheet(movie.id));
    row.appendChild(item);
  });
  return row;
}

function renderSheet(movie) {
  const fragment = document.createDocumentFragment();

  const hero = el('div', 'sheet-hero');
  if (movie.backdrop) hero.style.backgroundImage = `url("${movie.backdrop}")`;
  fragment.appendChild(hero);

  const main = el('div', 'sheet-main');

  // colonne gauche : affiche
  const left = el('div');
  const poster = el('img', 'sheet-poster');
  poster.alt = `Affiche de ${movie.title}`;
  if (movie.poster) poster.src = movie.poster;
  left.appendChild(poster);
  main.appendChild(left);

  // colonne droite
  const right = el('div');
  const head = el('div', 'sheet-head');
  head.appendChild(el('h3', 'sheet-title', movie.title));
  if (movie.original_title && movie.original_title !== movie.title) {
    head.appendChild(el('p', 'sheet-original', `Titre original : ${movie.original_title}`));
  }
  if (movie.tagline) head.appendChild(el('p', 'sheet-tagline', `« ${movie.tagline} »`));

  const facts = el('div', 'sheet-facts');
  const factList = [
    movie.year,
    formatRuntime(movie.runtime),
    (movie.genres || []).join(', ') || null,
    (movie.directors || []).length ? `de ${movie.directors.join(', ')}` : null,
  ].filter(Boolean);
  factList.forEach((text) => facts.appendChild(el('span', 'fact', text)));
  if (movie.certification) facts.appendChild(el('span', 'fact is-cert', movie.certification));
  head.appendChild(facts);
  head.appendChild(renderScore(movie));
  head.appendChild(renderActions(movie));
  right.appendChild(head);

  if (movie.overview) {
    right.appendChild(section('Synopsis', el('p', null, movie.overview)));
  } else {
    right.appendChild(section('Synopsis', el('p', 'muted', 'Aucun synopsis en français n’est disponible pour ce film.')));
  }

  const plexInfo = (state.avail.get(movie.id) || {}).plex;
  if (plexInfo) {
    right.appendChild(section('Sur ton serveur Plex', renderPlex(plexInfo)));
  }

  right.appendChild(section('Où en savoir plus', renderLinks(movie)));

  if (movie.trailer) {
    const wrapper = el('div');
    const frame = el('div', 'trailer');
    const iframe = document.createElement('iframe');
    iframe.src = movie.trailer.embed;
    iframe.title = `Bande-annonce de ${movie.title}`;
    iframe.allow = 'accelerometer; encrypted-media; picture-in-picture; fullscreen';
    iframe.allowFullscreen = true;
    iframe.loading = 'lazy';
    frame.appendChild(iframe);
    wrapper.appendChild(frame);
    const isFrench = movie.trailer.language === 'fr';
    wrapper.appendChild(el(
      'span',
      'trailer-lang',
      isFrench ? 'Bande-annonce française' : 'Bande-annonce en version originale — aucune version française trouvée',
    ));
    right.appendChild(section('Bande-annonce', wrapper));
  }

  if (state.tracker.enabled) {
    const slot = el('div');
    slot.id = 'torrent-slot';
    slot.appendChild(el('p', 'muted', 'Interrogation du tracker…'));
    right.appendChild(section(`Torrents · ${state.tracker.name}`, slot));
  }

  if ((movie.cast || []).length) {
    right.appendChild(section('Distribution', renderCast(movie.cast)));
  }

  if (movie.collection) {
    const sagaSlot = el('div');
    sagaSlot.id = 'saga-slot';
    sagaSlot.dataset.collection = movie.collection.id;
    sagaSlot.appendChild(el('p', 'muted', 'Chargement de la saga…'));
    right.appendChild(section(movie.collection.name, sagaSlot));
  }

  const similarSlot = el('div');
  similarSlot.id = 'similar-slot';
  similarSlot.appendChild(el('p', 'muted', 'Recherche de films proches…'));
  right.appendChild(section('Dans le même esprit', similarSlot));

  const extras = [
    (movie.writers || []).length ? `Scénario : ${movie.writers.join(', ')}` : null,
    (movie.companies || []).length ? `Production : ${movie.companies.join(', ')}` : null,
    formatMoney(movie.budget) ? `Budget : ${formatMoney(movie.budget)}` : null,
    formatMoney(movie.revenue) ? `Recettes : ${formatMoney(movie.revenue)}` : null,
  ].filter(Boolean);
  if (extras.length) {
    right.appendChild(section('Fiche technique', el('p', 'muted', extras.join('  ·  '))));
  }

  main.appendChild(right);
  fragment.appendChild(main);
  return fragment;
}

function section(title, content) {
  const box = el('section', 'sheet-section');
  box.appendChild(el('h4', null, title));
  box.appendChild(content);
  return box;
}

function renderPlex(info) {
  const box = el('div', 'plex-card');

  const head = el('div', 'plex-head');
  head.appendChild(el('span', 'plex-mark', 'Plex'));
  head.appendChild(el('span', 'plex-title', 'Déjà dans ta bibliothèque'));
  box.appendChild(head);

  const facts = el('div', 'plex-facts');
  [
    info.resolution,
    info.codec ? info.codec.toUpperCase() : null,
    info.container ? info.container.toUpperCase() : null,
    info.size ? formatSize(info.size) : null,
    info.view_count ? `déjà lu ${info.view_count} fois sur Plex` : null,
  ].filter(Boolean).forEach((text) => facts.appendChild(el('span', 'fact', text)));
  if (facts.children.length) box.appendChild(facts);

  if (info.matched_by === 'titre') {
    box.appendChild(el(
      'p',
      'muted',
      'Rapproché par titre et année : Plex n’a pas d’identifiant TMDB pour ce film.',
    ));
  }

  if (info.url) {
    const link = el('a', 'link-btn plex-open');
    link.href = info.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const dot = el('span', 'link-dot');
    dot.style.background = '#e5a00d';
    link.appendChild(dot);
    link.appendChild(document.createTextNode('Ouvrir dans Plex'));
    box.appendChild(link);
  }
  return box;
}

function renderActions(movie) {
  const row = el('div', 'sheet-actions');
  row.appendChild(renderSeenButton(movie));
  row.appendChild(renderWantButton(movie));
  row.appendChild(renderIgnoreButton(movie));
  return row;
}

function renderIgnoreButton(movie) {
  const ignore = state.ignored.has(movie.id);
  const button = el('button', 'sheet-hide');
  button.type = 'button';
  button.dataset.id = movie.id;
  button.appendChild(el('span', 'sheet-hide-icon'));
  button.appendChild(el('span', null, ignore ? 'Ne plus ignorer' : 'Ne plus me proposer'));
  button.addEventListener('click', () => toggleIgnore(movie));
  return button;
}

function renderWantButton(movie) {
  const wanted = state.watchlist.has(movie.id);
  const button = el('button', `sheet-want${wanted ? ' is-on' : ''}`);
  button.type = 'button';
  button.dataset.id = movie.id;
  button.setAttribute('aria-pressed', String(wanted));
  button.appendChild(el('span', 'sheet-want-icon'));
  button.appendChild(el('span', 'sheet-want-text', wanted ? 'Dans ma liste' : 'À voir'));
  button.addEventListener('click', () => toggleWant(movie));
  return button;
}

function renderSeenButton(movie) {
  const isSeen = state.seen.has(movie.id);
  const button = el('button', `sheet-seen${isSeen ? ' is-on' : ''}`);
  button.type = 'button';
  button.dataset.id = movie.id;
  button.setAttribute('aria-pressed', String(isSeen));
  button.appendChild(el('span', 'sheet-seen-icon'));
  button.appendChild(el('span', 'sheet-seen-text', isSeen ? 'Déjà vu' : 'Marquer comme vu'));
  button.addEventListener('click', () => toggleSeen(movie));
  return button;
}

function renderScore(movie) {
  const box = el('div', 'score');
  if (movie.votes > 0) {
    box.appendChild(el('span', 'score-big', movie.rating.toFixed(1).replace('.', ',')));
    box.appendChild(el('span', 'score-scale', '/ 10'));
    const meta = el('div', 'score-meta');
    meta.appendChild(el('div', null, `${formatNumber(movie.votes)} votes TMDB`));
    const bar = el('div', 'score-bar');
    const fill = el('span');
    fill.style.width = `${Math.round(movie.rating * 10)}%`;
    bar.appendChild(fill);
    meta.appendChild(bar);
    box.appendChild(meta);
  } else {
    box.appendChild(el('span', 'muted', 'Pas encore de note.'));
  }
  return box;
}

function renderLinks(movie) {
  const box = el('div', 'links');
  const query = encodeURIComponent(`${movie.original_title || movie.title} ${movie.year || ''}`.trim());
  const titleQuery = encodeURIComponent(movie.title);

  const entries = [
    movie.imdb_id && ['IMDb', `https://www.imdb.com/title/${movie.imdb_id}/`, '#f5c518'],
    ['SensCritique', `https://www.senscritique.com/search?query=${titleQuery}`, '#ff9d00'],
    ['Allociné', `https://www.allocine.fr/rechercher/?q=${titleQuery}`, '#fecc00'],
    ['Letterboxd', `https://letterboxd.com/tmdb/${movie.id}/`, '#00e054'],
    ['TMDB', `https://www.themoviedb.org/movie/${movie.id}`, '#01b4e4'],
    ['JustWatch', `https://www.justwatch.com/fr/recherche?q=${titleQuery}`, '#fbc500'],
    movie.trailer && ['YouTube', movie.trailer.url, '#ff0033'],
    ['Wikipédia', `https://fr.wikipedia.org/w/index.php?search=${query}+film`, '#cfd6e4'],
  ].filter(Boolean);

  entries.forEach(([label, href, color]) => {
    const link = el('a', 'link-btn');
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const dot = el('span', 'link-dot');
    dot.style.background = color;
    link.appendChild(dot);
    link.appendChild(document.createTextNode(label));
    box.appendChild(link);
  });

  const providers = movie.providers || {};
  const streaming = providers.flatrate || [];
  if (streaming.length) {
    // width 100 % : sans cela la note se glisse dans la rangée de boutons.
    const note = el('p', 'muted links-note');
    note.textContent = `En streaming inclus : ${streaming.join(', ')}`;
    box.appendChild(note);
  }
  return box;
}

function renderCast(cast) {
  const box = el('div', 'cast');
  cast.forEach((person) => {
    const figure = el('figure');
    const image = el('img');
    image.alt = person.name || '';
    image.loading = 'lazy';
    if (person.photo) image.src = person.photo;
    figure.appendChild(image);
    const caption = el('figcaption');
    caption.appendChild(el('div', null, person.name || ''));
    if (person.character) caption.appendChild(el('div', 'cast-role', person.character));
    figure.appendChild(caption);
    box.appendChild(figure);
  });
  return box;
}

function renderTorrents(result) {
  if (result.disabled) {
    return el('p', 'muted', 'Tracker non configuré.');
  }
  if (result.error) {
    return el('div', 'notice is-error', result.error);
  }
  const torrents = result.torrents || [];
  if (!torrents.length) {
    return el('p', 'muted', `Aucun résultat sur ${state.tracker.name} pour « ${result.query} ».`);
  }

  const box = el('div', 'torrent-list');

  if (result.matched_by === 'titre') {
    box.appendChild(el(
      'p',
      'muted torrent-warn',
      'Le tracker ne connaît pas ce film par son identifiant TMDB : résultats rapprochés par titre, à vérifier.',
    ));
  }

  torrents.slice(0, 12).forEach((torrent) => {
    const row = el('div', 'torrent');

    const left = el('div');
    left.appendChild(el('div', 'torrent-name', torrent.name));
    const tags = el('div', 'torrent-tags');
    const { resolution, source, codec, language } = torrent.tags || {};
    if (resolution) tags.appendChild(el('span', 'tag is-res', resolution));
    if (language) tags.appendChild(el('span', 'tag is-lang', language));
    if (source) tags.appendChild(el('span', 'tag', source));
    if (codec) tags.appendChild(el('span', 'tag', codec));
    if (torrent.freeleech) tags.appendChild(el('span', 'tag is-free', 'FREELEECH'));
    if (tags.children.length) left.appendChild(tags);
    row.appendChild(left);

    const stats = el('div', 'torrent-stats');
    if (torrent.size) stats.appendChild(el('span', 'torrent-size', formatSize(torrent.size)));
    if (torrent.seeders != null) {
      stats.appendChild(el('span', 'seeders', `▲ ${formatNumber(torrent.seeders)}`));
    }
    if (torrent.leechers != null) {
      stats.appendChild(el('span', 'leechers', `▼ ${formatNumber(torrent.leechers)}`));
    }
    if (torrent.details) {
      const page = el('a', 'torrent-page', 'Fiche');
      page.href = torrent.details;
      page.target = '_blank';
      page.rel = 'noopener noreferrer';
      page.title = 'Ouvrir la page du torrent sur le tracker';
      stats.appendChild(page);
    }
    if (torrent.slug) {
      // Passe par le serveur local : la clé d'API n'apparaît pas dans la page.
      const get = el('a', 'torrent-get', '.torrent');
      get.href = apiUrl(`api/torrent?slug=${encodeURIComponent(torrent.slug)}`);
      get.title = 'Télécharger le fichier .torrent';
      stats.appendChild(get);

      if (state.deluge.enabled) {
        const envoi = el('button', 'torrent-deluge', 'Deluge');
        envoi.type = 'button';
        envoi.title = 'Lancer le téléchargement sur le serveur Deluge';
        envoi.addEventListener('click', () => envoyerVersDeluge(torrent.slug, envoi));
        stats.appendChild(envoi);
      }
    }
    row.appendChild(stats);

    box.appendChild(row);
  });

  if (torrents.length > 12) {
    box.appendChild(el('p', 'muted', `… et ${torrents.length - 12} autres résultats.`));
  }
  return box;
}

/* ------------------------------- réglages -------------------------------- */

let plexPin = null;
let plexTimer = null;

function openSettings() {
  $('settings-backdrop').hidden = false;
  document.body.style.overflow = 'hidden';
  loadSettings();
}

function closeSettings() {
  $('settings-backdrop').hidden = true;
  document.body.style.overflow = '';
  clearTimeout(plexTimer);
  plexPin = null;
}

async function loadSettings() {
  let s;
  try {
    s = await api('/api/settings');
  } catch (error) {
    settingsNotice(error.message, true);
    return;
  }

  $('settings-intro').textContent = s.configured
    ? 'Les champs de clé restent vides : une valeur enregistrée n’est jamais renvoyée au navigateur.'
    : 'Renseigne au minimum une clé TMDB pour que l’application puisse fonctionner.';

  // Un secret déjà en place n'est pas réaffiché ; on le signale autrement.
  $('set-tmdb-key').placeholder = s.tmdb.has_key ? '•••••••• (enregistrée)' : 'obligatoire';
  $('set-tmdb-key').value = '';
  $('set-tmdb-language').value = s.tmdb.language;
  $('set-tmdb-region').value = s.tmdb.region;

  $('set-tracker-enabled').checked = s.tracker.enabled;
  $('set-tracker-url').value = s.tracker.base_url;
  $('set-tracker-key').placeholder = s.tracker.has_key ? '•••••••• (enregistrée)' : '';
  $('set-tracker-key').value = '';

  $('set-plex-sync').checked = s.plex.sync_watched;
  $('plex-state').textContent = s.plex.has_token
    ? 'Connecté — ' + s.plex.base_url
    : 'Aucun serveur connecté.';
  $('plex-state').className = 'settings-state' + (s.plex.has_token ? ' is-ok' : '');
  $('plex-servers').textContent = '';
  $('plex-sync-state').textContent = '';
  $('plex-refresh-state').textContent = '';
  $('import-state').textContent = '';
  $('set-deluge-enabled').checked = s.deluge.enabled;
  $('set-deluge-url').value = s.deluge.base_url;
  $('set-deluge-password').placeholder = s.deluge.has_password ? '•••••••• (enregistré)' : '';
  $('set-deluge-password').value = '';
  $('set-deluge-keypass').placeholder = s.deluge.has_key_password ? '•••••••• (enregistrée)' : '';
  $('set-deluge-keypass').value = '';
  ROLES_CERT.forEach((role) => afficherCertificat(role, s.deluge));
  $('set-deluge-dir').value = s.deluge.download_location;
  $('set-deluge-label').value = s.deluge.label;
  $('set-deluge-paused').checked = s.deluge.add_paused;
  $('set-deluge-verify').checked = s.deluge.verify_tls;
  $('deluge-state').textContent = '';

  await buildServiceChoices(s.streaming.my_services);
}

function settingsNotice(message, isError) {
  const box = $('settings-notice');
  if (!message) { box.hidden = true; return; }
  box.textContent = message;
  box.className = isError ? 'notice is-error' : 'notice';
  box.hidden = false;
}

async function saveSettings(event) {
  event.preventDefault();
  const bouton = $('settings-save');
  bouton.disabled = true;
  bouton.textContent = 'Enregistrement…';
  settingsNotice(null);

  try {
    await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tmdb: {
          api_key: $('set-tmdb-key').value.trim(),
          language: $('set-tmdb-language').value.trim(),
          region: $('set-tmdb-region').value.trim(),
        },
        tracker: {
          enabled: $('set-tracker-enabled').checked,
          base_url: $('set-tracker-url').value.trim(),
          api_key: $('set-tracker-key').value.trim(),
        },
        streaming: { my_services: [...servicesChoisis] },
        plex: { sync_watched: $('set-plex-sync').checked },
        deluge: {
          enabled: $('set-deluge-enabled').checked,
          base_url: $('set-deluge-url').value.trim(),
          password: $('set-deluge-password').value,
          // Les certificats ne passent pas par ce formulaire : ils sont
          // déposés par leur propre requête, qui en fixe le chemin.
          client_key_password: $('set-deluge-keypass').value,
          download_location: $('set-deluge-dir').value.trim(),
          label: $('set-deluge-label').value.trim(),
          add_paused: $('set-deluge-paused').checked,
          verify_tls: $('set-deluge-verify').checked,
        },
      }),
    });
    settingsNotice('Enregistré. Les réglages sont pris en compte immédiatement.', false);
    await loadSettings();
    // Le bandeau, les genres et les plateformes dépendent de la configuration.
    await /* --------------------------- tiroir de filtres --------------------------- */

/** Le tiroir n'existe qu'en dessous de 1080 px ; au-dessus le panneau est fixe. */
function filtresEnTiroir() {
  return matchMedia('(max-width: 1080px)').matches;
}

function openFilters() {
  if (!filtresEnTiroir()) return;
  $('panel').classList.add('is-open');
  $('panel-veil').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeFilters() {
  $('panel').classList.remove('is-open');
  $('panel-veil').hidden = true;
  // La fiche peut être ouverte par-dessus : ne pas lui rendre le défilement.
  if ($('sheet-backdrop').hidden && $('settings-backdrop').hidden) {
    document.body.style.overflow = '';
  }
}

/** Compte les critères actifs, pour que le bouton dise s'il se passe quelque
 *  chose derrière lui — sinon le tiroir fermé cache l'état des filtres. */
function renderFiltersCount() {
  let n = state.genreChoice.size + state.providers.size;
  if ($('year-min').value.trim() || $('year-max').value.trim()) n += 1;
  if (parseFloat($('rating-min').value) !== 6.5) n += 1;
  if (parseInt($('runtime-max').value, 10) < 245) n += 1;
  if ($('original-language').value) n += 1;
  if ($('keyword').value.trim()) n += 1;
  if ($('hide-seen').checked) n += 1;
  $('filters-count').textContent = n || '';
}

bootstrap();
  } catch (error) {
    settingsNotice(error.message, true);
  } finally {
    bouton.disabled = false;
    bouton.textContent = 'Enregistrer';
  }
}

/* ---- connexion Plex, pilotée depuis la page ---- */

async function plexConnect() {
  const bouton = $('plex-connect');
  bouton.disabled = true;
  $('plex-servers').textContent = '';
  $('plex-state').textContent = 'Ouverture de la page Plex…';
  $('plex-state').className = 'settings-state';

  try {
    const demande = await api('/api/plex/login/start', { method: 'POST' });
    plexPin = demande.id;
    window.open(demande.url, '_blank', 'noopener');

    // Un bloqueur de fenêtres peut avoir empêché l'ouverture : on laisse le lien.
    const lien = el('p', 'settings-state');
    lien.appendChild(document.createTextNode('Si rien ne s’est ouvert, '));
    const a = el('a', null, 'clique ici pour approuver');
    a.href = demande.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    lien.appendChild(a);
    lien.appendChild(document.createTextNode('.'));
    $('plex-servers').appendChild(lien);

    $('plex-state').textContent = 'En attente de ton approbation chez Plex…';
    plexPoll(0);
  } catch (error) {
    $('plex-state').textContent = error.message;
    $('plex-state').className = 'settings-state is-error';
    bouton.disabled = false;
  }
}

function plexPoll(essais) {
  clearTimeout(plexTimer);
  // ~2,5 min : au-delà le PIN de Plex a de toute façon expiré.
  if (!plexPin || essais > 75) {
    $('plex-state').textContent = 'Délai dépassé. Relance la connexion.';
    $('plex-connect').disabled = false;
    return;
  }
  plexTimer = setTimeout(async () => {
    let r;
    try {
      r = await api('/api/plex/login/poll?id=' + plexPin);
    } catch (error) {
      $('plex-state').textContent = error.message;
      $('plex-state').className = 'settings-state is-error';
      $('plex-connect').disabled = false;
      return;
    }
    if (r.status === 'attente') { plexPoll(essais + 1); return; }
    showPlexServers(r.servers || []);
  }, 2000);
}

function showPlexServers(serveurs) {
  const box = $('plex-servers');
  box.textContent = '';
  $('plex-connect').disabled = false;

  if (!serveurs.length) {
    $('plex-state').textContent = 'Aucun serveur Plex sur ce compte.';
    return;
  }
  $('plex-state').textContent = serveurs.length === 1
    ? 'Connecté. Test des adresses…'
    : 'Connecté. Choisis ton serveur :';

  serveurs.forEach((serveur) => {
    const bouton = el('button', 'plex-server');
    bouton.type = 'button';
    bouton.appendChild(el('span', 'plex-server-name', serveur.name));
    bouton.appendChild(el('span', 'plex-server-meta',
      serveur.owned ? 'le tien' : 'partagé avec toi'));
    bouton.addEventListener('click', () => plexFinish(serveur.index, bouton));
    box.appendChild(bouton);
  });

  // Un seul serveur : inutile de faire cliquer pour un choix qui n'en est pas un.
  if (serveurs.length === 1) plexFinish(serveurs[0].index, box.firstElementChild);
}

async function plexFinish(index, bouton) {
  if (bouton) bouton.disabled = true;
  $('plex-state').textContent = 'Test des adresses du serveur…';

  try {
    const r = await api('/api/plex/login/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: plexPin, index }),
    });
    if (r.status !== 'connecte') {
      $('plex-state').textContent = r.message || 'Serveur injoignable.';
      $('plex-state').className = 'settings-state is-error';
      if (bouton) bouton.disabled = false;
      return;
    }
    $('plex-state').textContent =
      'Connecté à ' + r.server + ' — ' + r.base_url + ' (' + r.route + ')';
    $('plex-state').className = 'settings-state is-ok';
    $('plex-servers').textContent = '';
    plexPin = null;
    await /* --------------------------- tiroir de filtres --------------------------- */

/** Le tiroir n'existe qu'en dessous de 1080 px ; au-dessus le panneau est fixe. */
function filtresEnTiroir() {
  return matchMedia('(max-width: 1080px)').matches;
}

function openFilters() {
  if (!filtresEnTiroir()) return;
  $('panel').classList.add('is-open');
  $('panel-veil').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeFilters() {
  $('panel').classList.remove('is-open');
  $('panel-veil').hidden = true;
  // La fiche peut être ouverte par-dessus : ne pas lui rendre le défilement.
  if ($('sheet-backdrop').hidden && $('settings-backdrop').hidden) {
    document.body.style.overflow = '';
  }
}

/** Compte les critères actifs, pour que le bouton dise s'il se passe quelque
 *  chose derrière lui — sinon le tiroir fermé cache l'état des filtres. */
function renderFiltersCount() {
  let n = state.genreChoice.size + state.providers.size;
  if ($('year-min').value.trim() || $('year-max').value.trim()) n += 1;
  if (parseFloat($('rating-min').value) !== 6.5) n += 1;
  if (parseInt($('runtime-max').value, 10) < 245) n += 1;
  if ($('original-language').value) n += 1;
  if ($('keyword').value.trim()) n += 1;
  if ($('hide-seen').checked) n += 1;
  $('filters-count').textContent = n || '';
}

bootstrap();
  } catch (error) {
    $('plex-state').textContent = error.message;
    $('plex-state').className = 'settings-state is-error';
    if (bouton) bouton.disabled = false;
  }
}

/* ---- abonnements ---- */

let servicesChoisis = new Set();

async function buildServiceChoices(selection) {
  const box = $('set-services');
  box.textContent = '';
  servicesChoisis = new Set((selection || []).map(Number));

  let data;
  try {
    // « all » : sans lui on ne verrait que les services déjà cochés, et il
    // deviendrait impossible d'en ajouter un.
    data = await api('/api/providers?all=1');
  } catch (error) {
    box.appendChild(el('p', 'settings-state is-error', error.message));
    return;
  }

  (data.providers || []).filter((p) => !p.local).forEach((provider) => {
    const chip = el('button', 'provider');
    chip.type = 'button';
    chip.title = provider.name;
    chip.classList.toggle('is-on', servicesChoisis.has(provider.id));
    if (provider.logo) {
      const logo = document.createElement('img');
      logo.src = provider.logo;
      logo.alt = '';
      chip.appendChild(logo);
    }
    chip.appendChild(el('span', 'provider-name', provider.name));
    chip.addEventListener('click', () => {
      if (servicesChoisis.has(provider.id)) servicesChoisis.delete(provider.id);
      else servicesChoisis.add(provider.id);
      chip.classList.toggle('is-on', servicesChoisis.has(provider.id));
    });
    box.appendChild(chip);
  });
}

/* ---- relecture de la bibliothèque Plex à la demande ---- */

async function plexRefreshNow() {
  const bouton = $('plex-refresh-now');
  const etat = $('plex-refresh-state');
  bouton.disabled = true;
  bouton.textContent = 'Relecture…';
  etat.textContent = 'Inventaire de ta bibliothèque Plex…';
  etat.className = 'settings-state';

  try {
    const r = await api('/api/plex/refresh');
    if (r.disabled) {
      etat.textContent = 'Aucun serveur Plex connecté.';
    } else if (r.error) {
      etat.textContent = r.error;
      etat.className = 'settings-state is-error';
    } else {
      const films = `${r.count} film${r.count > 1 ? 's' : ''}`;
      let bilan = '.';
      if (r.known) {
        // Sans inventaire précédent, annoncer « aucun changement » serait faux.
        const bouges = [];
        if (r.added) bouges.push(`+${r.added}`);
        if (r.removed) bouges.push(`−${r.removed}`);
        bilan = bouges.length ? ` (${bouges.join(', ')}).` : ', rien de changé.';
      }
      etat.textContent = `Bibliothèque relue : ${films}${bilan}`;
      etat.className = 'settings-state is-ok';
      // Les badges déjà dessinés viennent de l'ancien inventaire.
      state.avail.clear();
      const visibles = [...$('grid').children]
        .map((carte) => carte._movie).filter(Boolean);
      if (visibles.length) await loadAvailability(visibles);
    }
  } catch (error) {
    etat.textContent = error.message;
    etat.className = 'settings-state is-error';
  } finally {
    bouton.disabled = false;
    bouton.textContent = 'Rafraîchir la bibliothèque Plex';
  }
}

/* ---- synchronisation Plex à la demande ---- */

async function plexSyncNow() {
  const bouton = $('plex-sync-now');
  const etat = $('plex-sync-state');
  bouton.disabled = true;
  bouton.textContent = 'Synchronisation…';
  etat.textContent = 'Lecture de ta bibliothèque Plex…';
  etat.className = 'settings-state';

  try {
    const r = await api('/api/plex/sync');
    if (r.disabled) {
      etat.textContent = 'Aucun serveur Plex connecté.';
    } else if (r.error) {
      etat.textContent = r.error;
      etat.className = 'settings-state is-error';
    } else {
      const n = r.added || 0;
      etat.textContent = n
        ? `${n} film${n > 1 ? 's' : ''} ajouté${n > 1 ? 's' : ''} aux déjà vus `
          + `(${r.watched_on_plex} lus sur Plex).`
        : `Rien à ajouter : les ${r.watched_on_plex} films lus sur Plex y sont déjà.`;
      etat.className = 'settings-state is-ok';
      await bootstrap();   // le compteur de l'onglet doit suivre
    }
  } catch (error) {
    etat.textContent = error.message;
    etat.className = 'settings-state is-error';
  } finally {
    bouton.disabled = false;
    bouton.textContent = 'Synchroniser les « déjà vus » maintenant';
  }
}

/* ---- import d'une base existante ---- */

const LISTES_CONNUES = {
  'seen.json': 'seen',
  'watchlist.json': 'watchlist',
  'ignored.json': 'ignored',
};

/** Devine la liste visée d'après le nom du fichier. */
function listeDuFichier(nom) {
  const base = nom.toLowerCase().split(/[\\/]/).pop();
  if (LISTES_CONNUES[base]) return LISTES_CONNUES[base];
  // Tolère seen.json.backup, seen-2026.json, mon_seen.json…
  const trouve = Object.keys(LISTES_CONNUES).find((c) => base.includes(c.replace('.json', '')));
  return trouve ? LISTES_CONNUES[trouve] : null;
}

async function importerFichiers(event) {
  const fichiers = [...event.target.files];
  const etat = $('import-state');
  if (!fichiers.length) return;

  etat.className = 'settings-state';
  etat.textContent = 'Lecture…';
  const lignes = [];

  for (const fichier of fichiers) {
    const liste = listeDuFichier(fichier.name);
    if (!liste) {
      lignes.push(`${fichier.name} : liste non reconnue, ignoré.`);
      continue;
    }
    let contenu;
    try {
      contenu = JSON.parse(await fichier.text());
    } catch (error) {
      lignes.push(`${fichier.name} : JSON illisible, ignoré.`);
      continue;
    }
    if (!contenu || typeof contenu.movies !== 'object') {
      lignes.push(`${fichier.name} : format inattendu, ignoré.`);
      continue;
    }
    try {
      const r = await api('/api/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ list: liste, movies: contenu.movies }),
      });
      lignes.push(
        `${fichier.name} : ${r.added} ajouté${r.added > 1 ? 's' : ''}, `
        + `${r.skipped} déjà présent${r.skipped > 1 ? 's' : ''}`
        + (r.invalid ? `, ${r.invalid} invalide${r.invalid > 1 ? 's' : ''}` : '')
        + ` — ${r.total} au total.`,
      );
    } catch (error) {
      lignes.push(`${fichier.name} : ${error.message}`);
    }
  }

  etat.textContent = lignes.join(' ');
  etat.className = 'settings-state is-ok';
  event.target.value = '';   // permet de réimporter le même fichier
  await /* --------------------------- tiroir de filtres --------------------------- */

/** Le tiroir n'existe qu'en dessous de 1080 px ; au-dessus le panneau est fixe. */
function filtresEnTiroir() {
  return matchMedia('(max-width: 1080px)').matches;
}

function openFilters() {
  if (!filtresEnTiroir()) return;
  $('panel').classList.add('is-open');
  $('panel-veil').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeFilters() {
  $('panel').classList.remove('is-open');
  $('panel-veil').hidden = true;
  // La fiche peut être ouverte par-dessus : ne pas lui rendre le défilement.
  if ($('sheet-backdrop').hidden && $('settings-backdrop').hidden) {
    document.body.style.overflow = '';
  }
}

/** Compte les critères actifs, pour que le bouton dise s'il se passe quelque
 *  chose derrière lui — sinon le tiroir fermé cache l'état des filtres. */
function renderFiltersCount() {
  let n = state.genreChoice.size + state.providers.size;
  if ($('year-min').value.trim() || $('year-max').value.trim()) n += 1;
  if (parseFloat($('rating-min').value) !== 6.5) n += 1;
  if (parseInt($('runtime-max').value, 10) < 245) n += 1;
  if ($('original-language').value) n += 1;
  if ($('keyword').value.trim()) n += 1;
  if ($('hide-seen').checked) n += 1;
  $('filters-count').textContent = n || '';
}

bootstrap();
}

/* --------------------------- tiroir de filtres --------------------------- */

/** Le tiroir n'existe qu'en dessous de 1080 px ; au-dessus le panneau est fixe. */
function filtresEnTiroir() {
  return matchMedia('(max-width: 1080px)').matches;
}

function openFilters() {
  if (!filtresEnTiroir()) return;
  $('panel').classList.add('is-open');
  $('panel-veil').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeFilters() {
  $('panel').classList.remove('is-open');
  $('panel-veil').hidden = true;
  // La fiche peut être ouverte par-dessus : ne pas lui rendre le défilement.
  if ($('sheet-backdrop').hidden && $('settings-backdrop').hidden) {
    document.body.style.overflow = '';
  }
}

/** Compte les critères actifs, pour que le bouton dise s'il se passe quelque
 *  chose derrière lui — sinon le tiroir fermé cache l'état des filtres. */
function renderFiltersCount() {
  let n = state.genreChoice.size + state.providers.size;
  if ($('year-min').value.trim() || $('year-max').value.trim()) n += 1;
  if (parseFloat($('rating-min').value) !== 6.5) n += 1;
  if (parseInt($('runtime-max').value, 10) < 245) n += 1;
  if ($('original-language').value) n += 1;
  if ($('keyword').value.trim()) n += 1;
  if ($('hide-seen').checked) n += 1;
  $('filters-count').textContent = n || '';
}

/* --------------------------- Certificats client -------------------------- */

const ROLES_CERT = ['client', 'client_key', 'ca'];

/** Rappelle le fichier en place, avec de quoi le retirer. Le contenu n'est
 *  jamais renvoyé par le serveur : on n'en connaît que le nom et la taille. */
function afficherCertificat(role, deluge) {
  const box = $(`cert-${role}-current`);
  box.textContent = '';
  const fichier = (deluge.files || {})[role];
  if (!fichier) {
    // Un chemin saisi autrefois, ou renseigné dans config.json à la main :
    // il reste valable, on ne le fait pas disparaître en silence.
    const chemin = { client: deluge.client_cert, client_key: deluge.client_key,
                     ca: deluge.ca_cert }[role];
    if (chemin) box.appendChild(el('span', 'cert-hint', chemin));
    return;
  }
  const nom = el('span', 'cert-name');
  nom.textContent = `${fichier.label || fichier.name} · ${Math.max(1, Math.round(fichier.size / 1024))} Ko`;
  box.appendChild(nom);
  const retirer = el('button', 'cert-remove', 'Retirer');
  retirer.type = 'button';
  retirer.addEventListener('click', () => retirerCertificat(role, retirer));
  box.appendChild(retirer);
}

/** Lit le fichier choisi et l'envoie encodé. */
function lireBase64(fichier) {
  return new Promise((resolve, reject) => {
    const lecteur = new FileReader();
    lecteur.onerror = () => reject(new Error('Lecture du fichier impossible.'));
    // readAsDataURL donne « data:...;base64,XXXX » : on ne garde que la charge.
    lecteur.onload = () => resolve(String(lecteur.result).split(',')[1] || '');
    lecteur.readAsDataURL(fichier);
  });
}

async function deposerCertificat(role, input) {
  const fichier = input.files && input.files[0];
  if (!fichier) return;
  const box = $(`cert-${role}-current`);
  box.textContent = '';
  box.appendChild(el('span', 'cert-hint', 'Envoi…'));

  try {
    await api('/api/certificates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        role, filename: fichier.name, data: await lireBase64(fichier),
      }),
    });
    settingsNotice(null);
  } catch (error) {
    settingsNotice(error.message, true);
  }
  // Le champ garde sinon le fichier : re-choisir le même ne déclencherait
  // plus rien.
  input.value = '';
  await loadSettings();
}

async function retirerCertificat(role, bouton) {
  bouton.disabled = true;
  try {
    await api(`/api/certificates?role=${encodeURIComponent(role)}`, { method: 'DELETE' });
    settingsNotice(null);
  } catch (error) {
    settingsNotice(error.message, true);
  }
  await loadSettings();
}

/* -------------------------------- Deluge -------------------------------- */

/** Diagnostic étape par étape : « ça ne marche pas » n'aide personne quand la
 *  chaîne compte un certificat, un TLS, un mot de passe et un démon. */
async function delugeTest() {
  const bouton = $('deluge-test');
  const box = $('deluge-state');
  bouton.disabled = true;
  bouton.textContent = 'Test en cours…';
  box.textContent = '';
  box.appendChild(el('p', 'settings-state', 'Connexion au serveur Deluge…'));

  let r;
  try {
    r = await api('/api/deluge/test');
  } catch (error) {
    box.textContent = '';
    box.appendChild(el('p', 'settings-state is-error', error.message));
    bouton.disabled = false;
    bouton.textContent = 'Tester la connexion';
    return;
  }

  box.textContent = '';
  (r.steps || []).forEach((etape) => {
    const ligne = el('p', `settings-state ${etape.ok ? 'is-ok' : 'is-error'}`);
    ligne.textContent = `${etape.ok ? '✓' : '✗'} ${etape.etape}`
      + (etape.detail ? ` — ${etape.detail}` : '')
      + (etape.erreur ? ` — ${etape.erreur}` : '');
    box.appendChild(ligne);
  });
  if (!r.steps || !r.steps.length) {
    box.appendChild(el('p', 'settings-state', r.message || 'Deluge n’est pas configuré.'));
  }

  bouton.disabled = false;
  bouton.textContent = 'Tester la connexion';
  await bootstrap();
}

/** Envoie un torrent au serveur, depuis sa ligne dans la fiche du film. */
async function envoyerVersDeluge(slug, bouton) {
  const initial = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = 'Envoi…';

  try {
    const r = await api('/api/deluge/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    // « Déjà présent » n'est pas une erreur : c'est une information utile.
    bouton.textContent = r.added ? 'Envoyé' : 'Déjà là';
    bouton.classList.add(r.added ? 'is-done' : 'is-known');
    bouton.title = r.message || '';
  } catch (error) {
    bouton.disabled = false;
    bouton.textContent = initial;
    bouton.classList.add('is-failed');
    bouton.title = error.message;
    showNotice(`Deluge : ${error.message}`, true);
  }
}

bootstrap();
