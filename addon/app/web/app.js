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
  screen: 'soir',           // soir | parcourir | liste | reglages
  prevScreen: 'soir',       // où revenir en quittant les réglages
  providerList: [],         // plateformes de la région (Plex en tête, s'il est connecté)
  mode: 'decouverte',       // decouverte | foryou | recherche | similaires | watchlist | seen | ignored
  searchQuery: '',
  similarTo: null,      // {id, title} quand on explore les films proches
  plex: { enabled: false },
  deluge: { enabled: false },
  avail: new Map(),         // id TMDB -> où regarder le film (Plex, plateformes)
  availKnown: new Set(),    // films dont les disponibilités ont été demandées (même sans résultat)
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

/* Les billets : une source de visionnage par billet, du plus immédiat au plus
 * long. Partagés par « Ce soir » et par la fiche du film. */

/** Du plus probable au moins probable : 1080p et version française d'abord,
 *  puis le plus alimenté. C'est un ordre, pas un choix : toutes les versions
 *  restent proposées. */
function trierTorrents(torrents) {
  const rang = (t) => {
    const tags = t.tags || {};
    return (/1080/.test(tags.resolution || '') ? 2 : 0) + (tags.language ? 1 : 0);
  };
  return [...torrents].sort((a, b) => rang(b) - rang(a) || (b.seeders || 0) - (a.seeders || 0));
}

/** Un billet. Avec `action.href` c'est un lien, avec `action.faire` un bouton,
 *  sans action il informe seulement. */
function billet({ petit, grand, action, info }) {
  const racine = action && action.href
    ? el('a', 'stub') : action ? el('div', 'stub') : el('div', 'stub is-info');
  if (action && action.href) {
    racine.href = action.href;
    racine.target = action.externe ? '_blank' : '_self';
    if (action.externe) racine.rel = 'noopener noreferrer';
  }
  const corps = el('span', 'stub-main');
  corps.appendChild(el('small', null, petit));
  corps.appendChild(el('strong', null, grand));
  racine.appendChild(corps);
  if (action) {
    if (action.href) {
      racine.appendChild(el('span', 'stub-go', action.texte));
    } else {
      const bouton = el('button', 'stub-go', action.texte);
      bouton.type = 'button';
      bouton.addEventListener('click', () => action.faire(bouton));
      racine.appendChild(bouton);
    }
  }
  if (info) racine.title = info;
  return racine;
}

/** Le billet « à télécharger ». Il dit combien de versions existent et ouvre
 *  leur liste : le choix de la version reste à l'utilisateur, jamais au billet. */
function billetTorrent(liste, surChoisir) {
  const n = liste.length;
  const meilleur = trierTorrents(liste)[0];
  const tags = meilleur.tags || {};
  const apercu = [tags.resolution, tags.language].filter(Boolean).join(', ');
  return billet({
    petit: `À télécharger sur ${state.tracker.name}`,
    grand: `${n} version${n > 1 ? 's' : ''}${apercu ? `, dès ${apercu}` : ''}`,
    action: surChoisir ? { texte: 'Choisir', faire: surChoisir } : null,
  });
}

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

let eventsBound = false;

async function bootstrap() {
  if (!eventsBound) {
    eventsBound = true;
    buildDecades();
    bindEvents();
    syncRangeLabels();
  }

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
    await buildProviders();
    renderSeenCount();
    renderTabCounts();

    // Sans clé TMDB rien ne peut fonctionner : on ouvre directement l'écran de
    // configuration plutôt que d'afficher une grille vide et inexplicable.
    if (data.configured === false) {
      openSettings();
    } else {
      soirInit();
    }
  } catch (error) {
    showNotice(error.message, true);
  }
}

/* ------------------------------- navigation ------------------------------ */

// « Parcourir » et « Ma liste » partagent la même grille : l'un montre des
// films à découvrir, l'autre ceux qu'on a déjà triés. Seuls les onglets, le
// panneau de filtres et les titres changent.
const ECRANS = {
  soir: 'screen-soir',
  parcourir: 'screen-parcourir',
  liste: 'screen-parcourir',
  reglages: 'screen-reglages',
};
const MODES_LISTE = ['watchlist', 'seen', 'ignored'];

function showScreen(name) {
  if (name === 'reglages' && state.screen !== 'reglages') state.prevScreen = state.screen;
  state.screen = name;
  [...new Set(Object.values(ECRANS))].forEach((id) => { $(id).hidden = id !== ECRANS[name]; });
  const liste = name === 'liste';
  $('screen-parcourir').classList.toggle('is-liste', liste);
  $('panel').hidden = liste;
  $('open-filters').hidden = liste;
  $('tabs').hidden = liste;
  $('list-tabs').hidden = !liste;
  document.body.dataset.screen = name;
  updateNav();
  window.scrollTo(0, 0);
}

function updateNav() {
  [...$('mainnav').children].forEach((bouton) => {
    if (bouton.dataset.nav === state.screen) bouton.setAttribute('aria-current', 'page');
    else bouton.removeAttribute('aria-current');
  });
}

async function goTo(cible) {
  if (cible === 'reglages') { openSettings(); return; }
  if (cible === 'soir') { showScreen('soir'); return; }
  if (cible === 'liste') {
    await showList(MODES_LISTE.includes(state.mode) ? state.mode : 'watchlist');
    return;
  }
  showScreen('parcourir');
  // Première visite, ou retour depuis une liste : mieux vaut montrer des films
  // que demander de remplir un formulaire.
  if (MODES_LISTE.includes(state.mode) || !state.lastMovies.length) await runSearch();
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
  renderTabCounts();

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
    renderTabCounts();
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
    button.textContent = isSeen ? 'Déjà vu' : 'Vu';
    button.setAttribute('aria-pressed', String(isSeen));
    button.title = isSeen ? 'Marqué comme vu — cliquer pour annuler' : 'Marquer comme vu';
    paintState(card);
  });

  const sheetButton = document.querySelector('.sheet-seen');
  if (sheetButton && Number(sheetButton.dataset.id) === movieId) {
    sheetButton.classList.toggle('is-on', isSeen);
    sheetButton.setAttribute('aria-pressed', String(isSeen));
    sheetButton.querySelector('.sheet-seen-text').textContent = isSeen
      ? 'Déjà vu' : 'Vu';
  }
}

/** Le marqueur posé sur l'affiche : « Vu » ou « À voir », visible sans survol. */
function paintState(card) {
  const marqueur = card.querySelector('.card-state');
  const id = card._movie && card._movie.id;
  const vu = state.seen.has(id);
  const texte = vu ? 'Vu' : state.watchlist.has(id) ? 'À voir' : '';
  marqueur.textContent = texte;
  marqueur.hidden = !texte;
  marqueur.classList.toggle('is-seen', vu);
}

/* ------------------------------ liste à voir ----------------------------- */

function renderTabCounts() {
  $('count-liste').textContent = state.watchlist.size || '';
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
  soirPeindreVoir(movieId);   // l'écran « Ce soir » suit la même liste

  [...$('grid').children].forEach((card) => {
    if (!card._movie || card._movie.id !== movieId) return;
    const button = card.querySelector('.card-want');
    button.classList.toggle('is-on', wanted);
    button.textContent = wanted ? 'Dans ma liste' : 'À voir';
    button.setAttribute('aria-pressed', String(wanted));
    button.title = wanted ? 'Dans ta liste à voir — cliquer pour retirer' : 'Ajouter à ma liste à voir';
    paintState(card);
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
  decouverte: 'Propositions',
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
  $('back-to-criteria').hidden = mode === 'decouverte' || MODES_LISTE.includes(mode);
  [...$('tabs').children, ...$('list-tabs').children].forEach((tab) => {
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
  showScreen('parcourir');
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
  if (state.screen !== 'liste') showScreen('liste');
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
        watchlist: 'Ta liste à voir est vide. « À voir », au survol d’une affiche, y ajoute un film.',
        ignored: 'Aucun film ignoré. « Ignorer », au survol d’une affiche, écarte un film des propositions.',
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

  state.providerList = data.providers || [];
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
    if (!$('search-input').value.trim()) return;
    showScreen('parcourir');
    runTitleSearch($('search-input').value);
  });

  $('mainnav').addEventListener('click', (event) => {
    const bouton = event.target.closest('button[data-nav]');
    if (bouton) goTo(bouton.dataset.nav);
  });

  $('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    const vue = tab.dataset.view;
    if (vue === 'decouverte') { runSearch(); }
    else if (vue === 'foryou') { showForYou(); }
    else if (vue === 'cinema') { showCinema(); }
  });
  $('list-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (tab) showList(tab.dataset.view);
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

  $('settings-form').addEventListener('submit', saveSettings);
  $('plex-connect').addEventListener('click', plexConnect);
  $('plex-sync-now').addEventListener('click', plexSyncNow);
  $('plex-refresh-now').addEventListener('click', plexRefreshNow);
  $('set-tracker-profile').addEventListener('change', () => {
    // Changer de tracker rend l'ancienne adresse caduque.
    $('set-tracker-url').value = '';
    decrireProfilTracker();
  });
  $('deluge-test').addEventListener('click', delugeTest);
  $('tracker-test').addEventListener('click', trackerTest);
  ROLES_CERT.forEach((role) => {
    $(`cert-${role}-file`).addEventListener('change', (e) => deposerCertificat(role, e.target));
  });
  $('set-import').addEventListener('change', importerFichiers);

  $('sheet-close').addEventListener('click', closeSheet);
  $('sheet-backdrop').addEventListener('click', (event) => {
    if (event.target === $('sheet-backdrop')) closeSheet();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('sheet-backdrop').hidden) closeSheet();
    else if (state.screen === 'reglages') closeSettings();
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
  } else if (!state.hasMore && state.lastMovies.length && !MODES_LISTE.includes(state.mode)) {
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
    hideButton.textContent = state.ignored.has(movie.id) ? 'Ne plus ignorer' : 'Ignorer';
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
    paintWhere(card);
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
  movies.forEach((movie) => state.availKnown.add(movie.id));
  [...$('grid').children].forEach((card) => paintWhere(card));
}

/** La ligne « où regarder » sous le titre : une seule, la plus immédiate.
 *  Plex d'abord (disponible tout de suite, sans abonnement de plus), puis un
 *  abonnement, puis le tracker. Le détail de chaque source est dans la fiche. */
function paintWhere(card) {
  const box = card.querySelector('.card-where');
  const movie = card._movie;
  if (!box || !movie) return;
  box.textContent = '';
  box.title = '';
  const info = state.avail.get(movie.id);
  const connu = state.availKnown.has(movie.id);
  let classe = null;
  let texte = '';

  if (info && info.plex) {
    classe = 'is-plex';
    texte = info.plex.resolution ? `Sur ton Plex, ${info.plex.resolution}` : 'Sur ton Plex';
  } else if (info && (info.providers || []).length) {
    classe = 'is-stream';
    texte = `Inclus avec ${info.providers[0].name}`;
  } else if (state.tracker.enabled) {
    // Une erreur du tracker ne dit rien du film : mieux vaut ne rien afficher
    // que « pas disponible » à tort. La fiche montre le détail de l'erreur.
    const resultat = state.torrentCache.get(movie.id);
    if (resultat && !resultat.error && !resultat.disabled) {
      const n = (resultat.torrents || []).length;
      if (n) {
        classe = 'is-tor';
        texte = `${n >= 20 ? '20+' : n} version${n > 1 ? 's' : ''} à télécharger`;
      } else if (connu) {
        classe = 'is-none';
        texte = 'Pas sur tes services';
      }
    }
  } else if (connu) {
    classe = 'is-none';
    texte = 'Pas sur tes services';
  }

  if (!texte) return;
  box.appendChild(el('i', `dot ${classe}`));
  box.appendChild(document.createTextNode(texte));
  box.title = texte;
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
      await fetchTorrents(card._movie);
      paintWhere(card);
    }
  };
  for (let i = 0; i < 3; i += 1) worker();
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
  $('sheet').scrollTop = 0;
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
  loadSheetWhere(movie).catch(() => {});

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

  const hero = el('div', `sheet-hero${movie.backdrop ? '' : ' is-empty'}`);
  if (movie.backdrop) hero.style.backgroundImage = `url("${movie.backdrop}")`;
  fragment.appendChild(hero);

  // En-tête : l'affiche, puis ce qui identifie le film. Le reste vient dessous,
  // dans l'ordre où on en a besoin : où le regarder avant tout.
  const top = el('div', 'sheet-top');
  const poster = el('img', 'sheet-poster');
  poster.alt = `Affiche de ${movie.title}`;
  if (movie.poster) poster.src = movie.poster;
  top.appendChild(poster);

  const head = el('div', 'sheet-head');
  head.appendChild(el('h3', 'sheet-title', movie.title));
  if (movie.original_title && movie.original_title !== movie.title) {
    head.appendChild(el('p', 'sheet-original', `Titre original : ${movie.original_title}`));
  }
  if (movie.tagline) head.appendChild(el('p', 'sheet-tagline', `« ${movie.tagline} »`));

  const facts = el('div', 'sheet-facts');
  [
    movie.year,
    formatRuntime(movie.runtime),
    (movie.genres || []).join(', ') || null,
    (movie.directors || []).length ? `de ${movie.directors.join(', ')}` : null,
  ].filter(Boolean).forEach((text) => facts.appendChild(el('span', null, String(text))));
  if (movie.certification) facts.appendChild(el('span', 'fact-cert', movie.certification));
  head.appendChild(facts);
  head.appendChild(renderScore(movie));
  top.appendChild(head);
  fragment.appendChild(top);

  const sections = el('div', 'sheet-sections');

  const slotOu = el('div', 'where-slot');
  slotOu.id = 'where-slot';
  slotOu.dataset.id = movie.id;
  sections.appendChild(section('Où le regarder', slotOu));

  sections.appendChild(section('Synopsis', movie.overview
    ? el('p', null, movie.overview)
    : el('p', 'muted', 'Aucun synopsis en français n’est disponible pour ce film.')));

  if (movie.trailer) sections.appendChild(section('Bande-annonce', renderTrailer(movie)));

  if ((movie.cast || []).length) {
    sections.appendChild(section('Distribution', renderCast(movie.cast)));
  }

  if (movie.collection) {
    const sagaSlot = el('div');
    sagaSlot.id = 'saga-slot';
    sagaSlot.dataset.collection = movie.collection.id;
    sagaSlot.appendChild(el('p', 'muted', 'Chargement de la saga…'));
    sections.appendChild(section(movie.collection.name, sagaSlot));
  }

  const similarSlot = el('div');
  similarSlot.id = 'similar-slot';
  similarSlot.appendChild(el('p', 'muted', 'Recherche de films proches…'));
  sections.appendChild(section('Dans le même esprit', similarSlot));

  const technique = renderTechnique(movie);
  if (technique) sections.appendChild(section('Fiche technique', technique));

  sections.appendChild(section('Plus d’infos', renderLinks(movie)));
  fragment.appendChild(sections);

  // Les trois gestes restent à portée de pouce, où que l'on soit dans la fiche.
  fragment.appendChild(renderActions(movie));
  return fragment;
}

function section(title, content) {
  const box = el('section', 'sheet-section');
  box.appendChild(el('h4', null, title));
  box.appendChild(content);
  return box;
}

/** Les billets d'un film, dans la fiche : Plex, abonnements, tracker. */
function paintSheetWhere(movie) {
  const slot = $('where-slot');
  if (!slot || Number(slot.dataset.id) !== movie.id) return;
  slot.textContent = '';

  const dispo = state.avail.get(movie.id) || {};
  const plex = dispo.plex;
  const connu = state.availKnown.has(movie.id);
  const resultat = state.torrentCache.get(movie.id);
  const billets = el('div', 'tickets');
  const notes = [];

  if (plex) {
    const detail = [
      plex.resolution,
      plex.codec ? plex.codec.toUpperCase() : null,
      plex.container ? plex.container.toUpperCase() : null,
      plex.size ? formatSize(plex.size) : null,
    ].filter(Boolean).join(', ');
    billets.appendChild(billet({
      petit: 'Sur ton Plex, prêt maintenant',
      grand: detail || 'Dans ta bibliothèque',
      action: plex.url ? { href: plex.url, texte: 'Ouvrir', externe: true } : null,
    }));
    if (plex.view_count) {
      notes.push(`Déjà lu ${plex.view_count} fois sur Plex.`);
    }
    if (plex.matched_by === 'titre') {
      notes.push('Rapproché par titre et année : Plex n’a pas d’identifiant TMDB pour ce film.');
    }
  }

  const noms = (dispo.providers || []).map((p) => p.name);
  if (noms.length) {
    const liste = noms.length > 1
      ? `${noms.slice(0, -1).join(', ')} et ${noms[noms.length - 1]}` : noms[0];
    billets.appendChild(billet({ petit: 'Abonnement', grand: `Inclus avec ${liste}` }));
  }
  // Ce que TMDB connaît en plus de tes abonnements : à titre d'information.
  const ailleurs = ((movie.providers || {}).flatrate || []).filter((n) => !noms.includes(n));
  if (ailleurs.length) {
    notes.push(noms.length
      ? `Aussi en streaming chez ${ailleurs.join(', ')}.`
      : `En streaming chez ${ailleurs.join(', ')}, mais pas dans tes abonnements.`);
  }

  if (state.tracker.enabled && !plex) {
    if (!resultat) {
      billets.appendChild(billet({ petit: 'À télécharger', grand: 'Recherche sur le tracker…' }));
    } else if (resultat.error) {
      billets.appendChild(billet({
        petit: 'À télécharger', grand: 'Tracker injoignable', info: resultat.error,
      }));
    } else if ((resultat.torrents || []).length) {
      billets.appendChild(billetTorrent(resultat.torrents, () => {
        const liste = slot.querySelector('.versions');
        if (!liste) return;
        liste.open = true;
        liste.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }));
    }
  }

  const tout = connu && (!state.tracker.enabled || plex || resultat);
  if (!billets.children.length) {
    billets.appendChild(billet(tout
      ? { petit: 'Où le regarder', grand: 'Pas encore disponible chez toi' }
      : { petit: 'Où le regarder', grand: 'Recherche en cours…' }));
  }
  billets.firstElementChild.classList.add('is-best');
  slot.appendChild(billets);
  notes.forEach((texte) => slot.appendChild(el('p', 'muted where-note', texte)));
  if (state.tracker.enabled) slot.appendChild(renderVersions(movie, slot, Boolean(plex)));
}

/** Les versions du tracker. Ouvertes d'emblée quand c'est la seule façon de voir
 *  le film : le choix doit se voir, pas se chercher. Repliées pour un film déjà
 *  sur Plex, où le tracker n'est interrogé que si on le demande. Un choix fait à
 *  la main l'emporte, y compris quand la fiche se repeint. */
function renderVersions(movie, slot, surPlex) {
  const boite = el('details', 'versions');
  const resultat = state.torrentCache.get(movie.id);
  const n = resultat && !resultat.error ? (resultat.torrents || []).length : 0;
  boite.appendChild(el('summary', null, n
    ? `Versions sur ${state.tracker.name} (${n})`
    : `Versions sur ${state.tracker.name}`));
  const corps = el('div', 'versions-body');
  boite.appendChild(corps);

  const remplir = async () => {
    if (corps.dataset.rempli) return;
    corps.dataset.rempli = '1';
    corps.appendChild(el('p', 'muted', 'Interrogation du tracker…'));
    const r = await fetchTorrents(movie);
    corps.textContent = '';
    corps.appendChild(renderTorrents(r));
  };
  boite.addEventListener('toggle', () => {
    slot.dataset.versions = boite.open ? 'ouvert' : 'ferme';
    if (boite.open) remplir();
  });

  const voulu = slot.dataset.versions;
  const ouvert = voulu ? voulu === 'ouvert' : !surPlex && n > 0;
  if (ouvert) { boite.open = true; remplir(); }
  return boite;
}

async function loadSheetWhere(movie) {
  paintSheetWhere(movie);
  if (!state.availKnown.has(movie.id)) await loadAvailability([movie]);
  paintSheetWhere(movie);
  const info = state.avail.get(movie.id);
  // Un film déjà sur Plex n'a pas besoin du tracker : on n'y va que si on nous le demande.
  if (state.tracker.enabled && !(info && info.plex)) {
    await fetchTorrents(movie);
    paintSheetWhere(movie);
  }
}

function renderTrailer(movie) {
  const boite = el('div', 'trailer-box');
  const bouton = el('button', 'trailer-open');
  bouton.type = 'button';
  bouton.appendChild(el('span', 'trailer-open-title', 'Voir la bande-annonce'));
  const francais = movie.trailer.language === 'fr';
  bouton.appendChild(el('span', 'trailer-open-lang', francais
    ? 'En français'
    : 'En version originale, aucune version française trouvée'));
  // Rien n'est demandé à YouTube tant qu'on ne clique pas.
  bouton.addEventListener('click', () => {
    const cadre = el('div', 'trailer');
    const iframe = document.createElement('iframe');
    let src = movie.trailer.embed;
    try {
      const url = new URL(src);
      url.searchParams.set('autoplay', '1');
      src = url.href;
    } catch (error) { /* adresse gardée telle quelle */ }
    iframe.src = src;
    iframe.title = `Bande-annonce de ${movie.title}`;
    iframe.allow = 'accelerometer; encrypted-media; picture-in-picture; fullscreen; autoplay';
    iframe.allowFullscreen = true;
    cadre.appendChild(iframe);
    boite.textContent = '';
    boite.appendChild(cadre);
  });
  boite.appendChild(bouton);
  return boite;
}

function renderTechnique(movie) {
  const lignes = [
    (movie.writers || []).length ? ['Scénario', movie.writers.join(', ')] : null,
    (movie.companies || []).length ? ['Production', movie.companies.join(', ')] : null,
    formatMoney(movie.budget) ? ['Budget', formatMoney(movie.budget)] : null,
    formatMoney(movie.revenue) ? ['Recettes', formatMoney(movie.revenue)] : null,
  ].filter(Boolean);
  if (!lignes.length) return null;
  const liste = el('dl', 'technique');
  lignes.forEach(([nom, valeur]) => {
    liste.appendChild(el('dt', null, nom));
    liste.appendChild(el('dd', null, valeur));
  });
  return liste;
}

function renderActions(movie) {
  const barre = el('div', 'sheet-bar');
  barre.appendChild(renderWantButton(movie));
  barre.appendChild(renderSeenButton(movie));
  barre.appendChild(renderIgnoreButton(movie));
  return barre;
}

function renderIgnoreButton(movie) {
  const ignore = state.ignored.has(movie.id);
  const button = el('button', 'sheet-hide');
  button.type = 'button';
  button.dataset.id = movie.id;
  button.appendChild(el('span', null, ignore ? 'Ne plus ignorer' : 'Ne plus proposer'));
  button.addEventListener('click', () => toggleIgnore(movie));
  return button;
}

function renderWantButton(movie) {
  const wanted = state.watchlist.has(movie.id);
  const button = el('button', `sheet-want${wanted ? ' is-on' : ''}`);
  button.type = 'button';
  button.dataset.id = movie.id;
  button.setAttribute('aria-pressed', String(wanted));
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
  button.appendChild(el('span', 'sheet-seen-text', isSeen ? 'Déjà vu' : 'Vu'));
  button.addEventListener('click', () => toggleSeen(movie));
  return button;
}

function renderScore(movie) {
  const box = el('p', 'sheet-score');
  if (movie.votes > 0) {
    box.appendChild(el('strong', null, movie.rating.toFixed(1).replace('.', ',')));
    box.appendChild(el('span', null, `sur 10, ${formatNumber(movie.votes)} votes TMDB`));
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
    movie.imdb_id && ['IMDb', `https://www.imdb.com/title/${movie.imdb_id}/`],
    ['SensCritique', `https://www.senscritique.com/search?query=${titleQuery}`],
    ['Allociné', `https://www.allocine.fr/rechercher/?q=${titleQuery}`],
    ['Letterboxd', `https://letterboxd.com/tmdb/${movie.id}/`],
    ['TMDB', `https://www.themoviedb.org/movie/${movie.id}`],
    ['JustWatch', `https://www.justwatch.com/fr/recherche?q=${titleQuery}`],
    movie.trailer && ['YouTube', movie.trailer.url],
    ['Wikipédia', `https://fr.wikipedia.org/w/index.php?search=${query}+film`],
  ].filter(Boolean);

  entries.forEach(([label, href]) => {
    const link = el('a', 'link-btn');
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = label;
    box.appendChild(link);
  });

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

  const lignes = trierTorrents(torrents).map((torrent) => {
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

    return row;
  });

  // Douze d'abord pour ne pas noyer la fiche, mais rien n'est caché : un bouton
  // montre tout le reste.
  lignes.slice(0, 12).forEach((row) => box.appendChild(row));
  if (lignes.length > 12) {
    const reste = lignes.slice(12);
    const plus = el('button', 'btn btn-ghost torrent-more',
      `Afficher les ${reste.length} autres versions`);
    plus.type = 'button';
    plus.addEventListener('click', () => {
      reste.forEach((row) => box.insertBefore(row, plus));
      plus.remove();
    });
    box.appendChild(plus);
  }
  return box;
}

/* ------------------------------- réglages -------------------------------- */

let plexPin = null;
let plexTimer = null;

function openSettings() {
  showScreen('reglages');
  loadSettings();
}

/** Quitte les réglages pour l'écran d'où l'on vient. */
function closeSettings() {
  clearTimeout(plexTimer);
  plexPin = null;
  showScreen(state.prevScreen || 'soir');
}

async function loadSettings() {
  let s;
  try {
    s = await api('/api/settings');
  } catch (error) {
    settingsNotice(error.message, true);
    return;
  }

  renderServiceStatus(s);
  $('settings-intro').textContent = s.configured
    ? 'Les champs de clé restent vides : une valeur enregistrée n’est jamais renvoyée au navigateur.'
    : 'Renseigne au minimum une clé TMDB pour que l’application puisse fonctionner.';

  // Un secret déjà en place n'est pas réaffiché ; on le signale autrement.
  $('set-tmdb-key').placeholder = s.tmdb.has_key ? '•••••••• (enregistrée)' : 'obligatoire';
  $('set-tmdb-key').value = '';
  $('set-tmdb-language').value = s.tmdb.language;
  $('set-tmdb-region').value = s.tmdb.region;

  $('set-tracker-enabled').checked = s.tracker.enabled;
  remplirProfilsTracker(s.tracker);
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
  $('tracker-state').textContent = '';

  await buildServiceChoices(s.streaming.my_services);
}

/** Résumé des services en tête des réglages : ce qui est en place, d'un coup d'œil.
 *  « Configuré » ne veut pas dire « répond » : le test reste au bouton de chaque bloc. */
function renderServiceStatus(s) {
  const lignes = [
    { cible: 'set-tmdb', nom: 'TMDB', ok: s.tmdb.has_key,
      texte: s.tmdb.has_key ? 'Clé enregistrée' : 'Clé manquante' },
    { cible: 'set-plex', nom: 'Plex', ok: Boolean(s.plex.has_token),
      texte: s.plex.has_token ? 'Serveur connecté' : 'Aucun serveur' },
    { cible: 'set-tracker', nom: s.tracker.name || 'Tracker',
      ok: s.tracker.enabled && s.tracker.has_key,
      texte: s.tracker.enabled ? (s.tracker.has_key ? 'Activé' : 'Clé manquante') : 'Désactivé' },
    { cible: 'set-deluge', nom: 'Deluge', ok: s.deluge.enabled && s.deluge.has_password,
      texte: s.deluge.enabled ? 'Activé' : 'Désactivé' },
  ];
  const liste = $('status-list');
  liste.textContent = '';
  lignes.forEach((ligne) => {
    const item = el('li');
    const bouton = el('button', `status-row${ligne.ok ? ' is-ok' : ''}`);
    bouton.type = 'button';
    bouton.appendChild(el('span', 'status-dot'));
    bouton.appendChild(el('span', 'status-name', ligne.nom));
    bouton.appendChild(el('span', 'status-text', ligne.texte));
    bouton.addEventListener('click', () => {
      $(ligne.cible).scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    item.appendChild(bouton);
    liste.appendChild(item);
  });
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
          profile: $('set-tracker-profile').value,
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
  await bootstrap();
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
  await bootstrap();
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

/* ---- choix du tracker ---- */

/** « Aucun torrent » peut venir de l'adresse, de la clé, de son scope ou du
 *  catalogue : le diagnostic sépare ces cas plutôt que de laisser chercher. */
async function trackerTest() {
  const bouton = $('tracker-test');
  const box = $('tracker-state');
  bouton.disabled = true;
  bouton.textContent = 'Test en cours…';
  box.textContent = '';
  box.appendChild(el('p', 'settings-state', 'Interrogation du tracker…'));

  let r;
  try {
    r = await api('/api/tracker/test');
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
    box.appendChild(el('p', 'settings-state', r.message || 'Tracker non configuré.'));
  }

  bouton.disabled = false;
  bouton.textContent = 'Tester la connexion';
  await bootstrap();
}


const ETIQUETTES_TRACKER = {
  'tr4ker.net': 'tr4ker.net',
  'v3x.club': 'v3x.club',
  torznab: 'Autre indexeur Torznab',
};

function remplirProfilsTracker(tracker) {
  const select = $('set-tracker-profile');
  select.textContent = '';
  (tracker.profiles || []).forEach((nom) => {
    const option = el('option', null, ETIQUETTES_TRACKER[nom] || nom);
    option.value = nom;
    select.appendChild(option);
  });
  select.value = tracker.profile;
  decrireProfilTracker();
}

/** Dit ce que le choix implique, au lieu de laisser deviner. */
function decrireProfilTracker() {
  const profil = $('set-tracker-profile').value;
  const url = $('set-tracker-url');
  const aide = $('tracker-profile-help');

  if (profil === 'torznab') {
    aide.textContent = 'Indexeur Torznab quelconque : renseigne son adresse '
      + 'ci-dessous. Les détails (authentification, chemin d’API) se règlent '
      + 'dans config.json.';
    url.placeholder = 'https://mon-indexeur.example';
  } else if (profil === 'v3x.club') {
    aide.textContent = 'Clé à créer dans Réglages → API de V3X, avec le scope '
      + '« torznab ». Laisse l’adresse vide pour utiliser celle du profil.';
    url.placeholder = 'https://api.v3x.club';
  } else {
    aide.textContent = 'Laisse l’adresse vide pour utiliser celle du profil.';
    url.placeholder = 'https://tr4ker.net';
  }
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
      state.availKnown.clear();
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
  await bootstrap();
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
  if ($('sheet-backdrop').hidden) document.body.style.overflow = '';
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
