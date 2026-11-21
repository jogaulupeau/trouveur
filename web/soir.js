/* Trouveur — écran « Ce soir ».
 *
 * Un film à la fois, où le regarder, et le suivant si ce n'est pas le bon soir.
 * Le film est tiré au hasard parmi ce que la phrase en haut de page autorise.
 *
 * Chargé après app.js, dont il reprend l'état (`state`), les appels (`api`) et
 * les gestes sur un film (vu, à voir, ignoré). Rien ici n'écrit sur le serveur
 * en dehors de ces gestes-là.
 */

'use strict';

/* ------------------------------ la phrase -------------------------------- */

// « un film d'action », « une comédie » : l'article suit le genre, un simple
// nom en minuscules ferait des phrases bancales.
const SOIR_GENRES = {
  28: 'un film d’action', 12: 'un film d’aventure', 16: 'un film d’animation',
  35: 'une comédie', 80: 'un film policier', 99: 'un documentaire', 18: 'un drame',
  10751: 'un film familial', 14: 'un film fantastique', 36: 'un film historique',
  27: 'un film d’horreur', 10402: 'un film musical', 9648: 'un film à mystère',
  10749: 'une romance', 878: 'un film de science-fiction', 10770: 'un téléfilm',
  53: 'un thriller', 10752: 'un film de guerre', 37: 'un western',
};

const SOIR_DUREES = [
  { value: null, label: 'de n’importe quelle durée' },
  { value: 90, label: 'de moins d’1 h 30' },
  { value: 120, label: 'de moins de 2 h' },
  { value: 150, label: 'de moins de 2 h 30' },
];

const SOIR_SOURCES = {
  all: 'où que ce soit',
  plex: 'sur mon Plex',
  subs: 'dans mes abonnements',
};

const SOIR_STOCKAGE = 'trouveur.soir';

const soir = {
  ready: false,
  genre: null,          // id TMDB, ou null pour « un film »
  runtime: null,        // minutes maximum, ou null
  source: 'all',        // all | plex | subs
  certification: null,  // classement maximal (« U », « 12 »…), ou null
  pool: [],             // films tirés, pas encore proposés
  done: new Set(),      // films déjà proposés ce soir : on ne les repropose pas
  hero: null,
  idea: 0,
  token: 0,             // invalide les réponses arrivées après un changement de film
  generation: 0,        // invalide les tirages arrivés après un changement de phrase
  fetching: null,
  menu: null,           // menu ouvert : { bouton, liste }
};

function soirCharger() {
  try {
    const saved = JSON.parse(localStorage.getItem(SOIR_STOCKAGE) || '{}');
    if (saved.genre) soir.genre = Number(saved.genre) || null;
    if (SOIR_DUREES.some((d) => d.value === saved.runtime)) soir.runtime = saved.runtime;
    if (SOIR_SOURCES[saved.source]) soir.source = saved.source;
    if (saved.certification) soir.certification = String(saved.certification);
  } catch (error) { /* stockage indisponible : la phrase repart de zéro */ }
}

function soirSauver() {
  try {
    localStorage.setItem(SOIR_STOCKAGE, JSON.stringify({
      genre: soir.genre, runtime: soir.runtime, source: soir.source,
      certification: soir.certification,
    }));
  } catch (error) { /* idem */ }
}

function soirSourcesDispo() {
  const sources = ['all'];
  if (state.plex.enabled) sources.push('plex');
  if (state.providerList.some((p) => !p.local)) sources.push('subs');
  return sources;
}

function soirParams() {
  const params = new URLSearchParams();
  if (soir.genre) params.set('genres', soir.genre);
  if (soir.runtime) params.set('runtime_max', soir.runtime);
  if (soir.certification) params.set('certification_max', soir.certification);
  // Mêmes planchers que l'écran « Parcourir » : un film tiré au hasard doit
  // rester un film dont on a entendu parler.
  params.set('rating_min', '6.5');
  params.set('votes_min', '300');
  params.set('sort', 'hasard');
  params.set('limit', '20');
  params.set('hide_seen', '1');
  if (soir.source === 'plex') params.set('providers', 'plex');
  if (soir.source === 'subs') {
    params.set('providers', state.providerList.filter((p) => !p.local).map((p) => p.id).join(','));
  }
  return params;
}

function soirOptions(cle) {
  if (cle === 'genre') {
    const genres = [...state.genres.entries()]
      .map(([id, nom]) => ({ value: id, label: SOIR_GENRES[id] || `un film (${nom.toLowerCase()})` }))
      .sort((a, b) => a.label.localeCompare(b.label, 'fr'));
    return [{ value: null, label: 'un film' }, ...genres];
  }
  if (cle === 'runtime') return SOIR_DUREES;
  if (cle === 'certification') {
    return [
      { value: null, label: 'avec n’importe quel classement' },
      ...classementsProposes().map((c) => ({
        value: c.code,
        label: c.code === 'U' ? 'classé tout public' : `classé ${libelleClassement(c.code)} au maximum`,
      })),
    ];
  }
  return soirSourcesDispo().map((value) => ({ value, label: SOIR_SOURCES[value] }));
}

function soirLibelle(cle) {
  const options = soirOptions(cle);
  const found = options.find((o) => o.value === soir[cle]);
  return (found || options[0]).label;
}

function soirRenderSentence() {
  const box = $('soir-sentence');
  box.textContent = '';
  box.appendChild(document.createTextNode('Ce soir, je veux '));
  // Sans classement connu pour la région, la phrase n'a pas ce mot.
  const cles = ['genre', 'runtime', 'source'];
  if (classementsProposes().length) cles.push('certification');
  cles.forEach((cle) => {
    const bouton = el('button', 'soir-choice', soirLibelle(cle));
    bouton.type = 'button';
    bouton.dataset.key = cle;
    bouton.setAttribute('aria-haspopup', 'listbox');
    bouton.setAttribute('aria-expanded', 'false');
    bouton.addEventListener('click', () => soirToggleMenu(bouton));
    box.appendChild(bouton);
    box.appendChild(document.createTextNode(' '));
  });
  const affiner = el('button', 'soir-refine', 'Voir tout le catalogue');
  affiner.type = 'button';
  affiner.addEventListener('click', soirAffiner);
  box.appendChild(affiner);
}

/* Menu d'un choix. Un vrai <select> aurait une largeur fixe et un aspect
 * système : ici le choix doit se lire comme un mot de la phrase. */

function soirFermerMenu(rendreLeFocus) {
  if (!soir.menu) return;
  const { bouton, liste } = soir.menu;
  liste.remove();
  bouton.setAttribute('aria-expanded', 'false');
  soir.menu = null;
  if (rendreLeFocus) bouton.focus();
}

function soirToggleMenu(bouton) {
  const dejaOuvert = soir.menu && soir.menu.bouton === bouton;
  soirFermerMenu(false);
  if (dejaOuvert) return;

  const cle = bouton.dataset.key;
  const liste = el('ul', 'soir-menu');
  liste.setAttribute('role', 'listbox');
  liste.setAttribute('aria-label', 'Choisir');
  soirOptions(cle).forEach((option) => {
    const item = el('li', 'soir-option', option.label);
    item.setAttribute('role', 'option');
    item.tabIndex = -1;
    const actif = option.value === soir[cle];
    item.setAttribute('aria-selected', String(actif));
    item.addEventListener('click', () => soirChoisir(cle, option.value));
    liste.appendChild(item);
  });
  liste.addEventListener('keydown', (event) => {
    const items = [...liste.children];
    const i = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') { event.preventDefault(); items[Math.min(i + 1, items.length - 1)].focus(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); items[Math.max(i - 1, 0)].focus(); }
    else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); document.activeElement.click(); }
    else if (event.key === 'Escape') { event.stopPropagation(); soirFermerMenu(true); }
  });

  bouton.setAttribute('aria-expanded', 'true');
  bouton.after(liste);
  // Sous le mot qu'on vient de toucher, sans déborder de la phrase.
  const phrase = $('soir-sentence');
  liste.style.top = `${bouton.offsetTop + bouton.offsetHeight + 6}px`;
  liste.style.left = `${Math.max(0, Math.min(bouton.offsetLeft, phrase.clientWidth - liste.offsetWidth))}px`;
  soir.menu = { bouton, liste };
  (liste.querySelector('[aria-selected="true"]') || liste.firstChild).focus();
}

function soirChoisir(cle, valeur) {
  soirFermerMenu(false);
  if (soir[cle] === valeur) { soirRenderSentence(); return; }
  soir[cle] = valeur;
  soirSauver();
  // Une nouvelle phrase repart d'un tirage neuf ; ce qui a déjà été proposé ce
  // soir reste exclu.
  soir.generation += 1;
  soir.fetching = null;
  soir.pool = [];
  soir.idea = 0;
  soirRenderSentence();
  soirMontrerSuivant();
}

/** Reprend la phrase dans les filtres de « Parcourir » et lance la recherche. */
function soirAffiner() {
  resetCriteria();
  if (soir.genre) {
    state.genreChoice.set(soir.genre, 'in');
    const chip = $('genres').querySelector(`[data-id="${soir.genre}"]`);
    if (chip) chip.className = 'chip is-in';
  }
  if (soir.runtime) { $('runtime-max').value = soir.runtime; syncRangeLabels(); }
  if (soir.certification) $('certification').value = soir.certification;
  if (soir.source !== 'all') {
    const ids = soir.source === 'plex'
      ? ['plex'] : state.providerList.filter((p) => !p.local).map((p) => String(p.id));
    ids.forEach((id) => state.providers.add(id));
    [...$('providers').children].forEach((c) => {
      c.classList.toggle('is-on', state.providers.has(c.dataset.id));
    });
  }
  renderFiltersCount();
  showScreen('parcourir');
  runSearch();
}

/* ------------------------------ le tirage -------------------------------- */

async function soirTirer() {
  if (soir.fetching) return soir.fetching;
  const generation = soir.generation;
  const promesse = (async () => {
    const data = await api(`/api/discover?${soirParams()}`);
    if (generation !== soir.generation) return 0;
    const connus = new Set(soir.pool.map((m) => m.id));
    const neufs = (data.movies || []).filter((m) => !soir.done.has(m.id) && !connus.has(m.id)
      && !(soir.hero && soir.hero.id === m.id)
      && !state.seen.has(m.id) && !state.ignored.has(m.id));
    soir.pool.push(...neufs);
    return neufs.length;
  })();
  soir.fetching = promesse;
  try {
    return await promesse;
  } finally {
    if (soir.fetching === promesse) soir.fetching = null;
  }
}

/** Remplit le vivier jusqu'à `n` films. Le hasard retombe parfois sur une page
 *  déjà vue : trois tirages au plus avant de conclure qu'il n'y a plus rien. */
async function soirAssurer(n) {
  for (let essai = 0; essai < 3 && soir.pool.length < n; essai += 1) {
    const generation = soir.generation;
    await soirTirer();
    if (generation !== soir.generation) return;
  }
}

async function soirMontrerSuivant() {
  const token = ++soir.token;
  soirMessageChargement();
  try {
    if (!soir.pool.length) await soirAssurer(1);
  } catch (error) {
    if (token === soir.token) soirMessage('Je n’arrive pas à choisir un film.', error.message, 'Réessayer', soirMontrerSuivant);
    return;
  }
  if (token !== soir.token) return;

  const film = soir.pool.shift();
  if (!film) {
    soir.hero = null;
    soirRenderOthers();
    soirMessage(
      'Aucun film ne correspond à cette phrase.',
      soir.done.size
        ? 'Tout ce qui correspondait a déjà été proposé ce soir. Élargis la durée ou la source.'
        : 'Élargis la durée ou la source, ou change de genre.',
    );
    return;
  }

  soir.hero = film;
  soir.idea += 1;
  soir.chargement = { fiche: true, dispo: true, torrents: state.tracker.enabled };
  soirRenderHero(film);
  soirRenderOthers();
  soirChargerSuite(film, token);
}

async function soirPasCeSoir() {
  if (soir.hero) soir.done.add(soir.hero.id);
  await soirMontrerSuivant();
}

/** Choisit une des « autres idées » : le film qu'on quitte compte comme proposé. */
function soirChoisirFilm(id) {
  const index = soir.pool.findIndex((m) => m.id === id);
  if (index < 0) return;
  if (soir.hero) soir.done.add(soir.hero.id);
  soir.pool.unshift(...soir.pool.splice(index, 1));
  soirMontrerSuivant();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ------------------------- ce qui arrive après coup ------------------------ */

async function soirChargerSuite(film, token) {
  soirAmbiance(film, token);

  // Le fond du tirage : de quoi remplir « Autres idées » sans attendre l'écran suivant.
  soirAssurer(6).then(() => {
    if (token !== soir.token) return;
    soirRenderOthers();
    soirDisponibilites(soir.pool.slice(0, 5));
  }).catch(() => {});

  api(`/api/movie/${film.id}`).then((fiche) => {
    if (token !== soir.token) return;
    Object.assign(film, fiche);
    soirPeindreFaits(film);
  }).catch(() => {}).finally(() => {
    if (token !== soir.token) return;
    soir.chargement.fiche = false;
  });

  await soirDisponibilites([film, ...soir.pool.slice(0, 5)]);
  if (token !== soir.token) return;
  soir.chargement.dispo = false;
  soirPeindreBillets(film);
  soirRenderOthers();

  // Le tracker n'est interrogé que pour un film absent de Plex : un film déjà
  // sur le serveur n'a pas besoin d'être téléchargé.
  const info = state.avail.get(film.id);
  if (state.tracker.enabled && !(info && info.plex)) {
    await fetchTorrents(film);
  }
  if (token !== soir.token) return;
  soir.chargement.torrents = false;
  soirPeindreBillets(film);
}

async function soirDisponibilites(films) {
  const aFaire = films.filter((m) => m && !state.availKnown.has(m.id));
  if (!aFaire.length) return;
  const params = new URLSearchParams();
  aFaire.forEach((m) => {
    params.append('id', m.id);
    params.append('title', m.title || '');
    params.append('year', m.year || '');
  });
  try {
    const data = await api(`/api/availability?${params}`);
    Object.entries(data.items || {}).forEach(([id, info]) => state.avail.set(Number(id), info));
    aFaire.forEach((m) => state.availKnown.add(m.id));
  } catch (error) {
    // Sans disponibilités les billets restent vides : l'écran reste utilisable.
  }
}

/* ------------------------------ l'ambiance -------------------------------- */

function soirAccent(r, g, b) {
  // Teinte et saturation de l'affiche, puis une clarté fixe : l'accent doit
  // porter du texte sombre lisible, quelle que soit l'affiche.
  const rr = r / 255, gg = g / 255, bb = b / 255;
  const max = Math.max(rr, gg, bb);
  const min = Math.min(rr, gg, bb);
  const d = max - min;
  let h = 0;
  if (d) {
    if (max === rr) h = ((gg - bb) / d) % 6;
    else if (max === gg) h = (bb - rr) / d + 2;
    else h = (rr - gg) / d + 4;
  }
  h = Math.round((h * 60 + 360) % 360);
  const l = (max + min) / 2;
  const s0 = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
  const sat = Math.round(Math.min(0.85, Math.max(0.5, s0)) * 100);
  return { fond: `hsl(${h} ${Math.min(sat, 70)}% 16%)`, accent: `hsl(${h} ${sat}% 66%)` };
}

/** Lit la couleur dominante de l'affiche. TMDB doit autoriser la lecture des
 *  pixels (CORS) ; sinon on garde l'ambiance par défaut. */
function soirAmbiance(film, token) {
  const ecran = document.body;
  if (!film.poster) return;
  const image = new Image();
  image.crossOrigin = 'anonymous';
  image.onload = () => {
    if (token !== soir.token) return;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 24; canvas.height = 36;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(image, 0, 0, 24, 36);
      const pixels = ctx.getImageData(0, 0, 24, 36).data;
      // Les pixels les plus saturés décident : une moyenne ferait du gris.
      let poids = 0, r = 0, g = 0, b = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        const max = Math.max(pixels[i], pixels[i + 1], pixels[i + 2]);
        const min = Math.min(pixels[i], pixels[i + 1], pixels[i + 2]);
        const w = (max - min) ** 2 / 255 + 1;
        poids += w; r += pixels[i] * w; g += pixels[i + 1] * w; b += pixels[i + 2] * w;
      }
      const { fond, accent } = soirAccent(r / poids, g / poids, b / poids);
      ecran.style.setProperty('--amb1', fond);
      ecran.style.setProperty('--amb2', accent);
    } catch (error) {
      ecran.style.removeProperty('--amb1');
      ecran.style.removeProperty('--amb2');
    }
  };
  image.onerror = () => {};
  image.src = film.poster;
}

/* -------------------------------- l'écran --------------------------------- */

function soirMessageChargement() {
  const hero = $('soir-hero');
  hero.setAttribute('aria-busy', 'true');
  hero.className = 'soir-hero is-loading';
  hero.textContent = '';
  hero.appendChild(el('p', 'soir-wait', 'Je cherche une idée…'));
}

function soirMessage(titre, texte, action, rappel) {
  const hero = $('soir-hero');
  hero.setAttribute('aria-busy', 'false');
  hero.className = 'soir-hero is-empty';
  hero.textContent = '';
  const box = el('div', 'soir-empty');
  box.appendChild(el('h2', 'soir-empty-title', titre));
  if (texte) box.appendChild(el('p', 'soir-empty-text', texte));
  if (action) {
    const bouton = el('button', 'soir-btn is-main', action);
    bouton.type = 'button';
    bouton.addEventListener('click', rappel);
    box.appendChild(bouton);
  }
  hero.appendChild(box);
}

function soirNomsGenres(film) {
  if (film.genres && film.genres.length) return film.genres;
  return (film.genre_ids || []).map((id) => state.genres.get(id)).filter(Boolean);
}

function soirRenderHero(film) {
  const hero = $('soir-hero');
  hero.setAttribute('aria-busy', 'false');
  hero.className = 'soir-hero';
  hero.textContent = '';

  const copie = el('div', 'soir-copy');
  copie.appendChild(el('p', 'soir-idea', `Idée ${soir.idea}`));
  const titre = el('h2', 'soir-title', film.title);
  if (film.title.length > 26) titre.classList.add('is-longer');
  else if (film.title.length > 13) titre.classList.add('is-long');
  copie.appendChild(titre);
  copie.appendChild(el('div', 'soir-facts'));
  copie.appendChild(el('p', 'soir-syn', film.overview || 'Pas de synopsis en français pour ce film.'));
  const billets = el('div', 'soir-tickets');
  billets.id = 'soir-tickets';
  copie.appendChild(billets);
  const versions = el('div', 'soir-versions');
  versions.id = 'soir-versions';
  copie.appendChild(versions);

  const actions = el('div', 'soir-actions');
  const pas = soirBouton('Pas ce soir', 'is-main', soirPasCeSoir);
  const voir = soirBouton('', '', () => toggleWant(film));
  voir.id = 'soir-want';
  const vu = soirBouton('Déjà vu', '', async () => {
    if (!state.seen.has(film.id)) await toggleSeen(film);
    soirPasCeSoir();
  });
  const fiche = soirBouton('Fiche complète', '', () => openSheet(film.id));
  const ignorer = soirBouton('Ne plus proposer', 'is-quiet', async () => {
    if (!state.ignored.has(film.id)) await toggleIgnore(film);
    soirPasCeSoir();
  });
  [pas, voir, vu, fiche, ignorer].forEach((b) => actions.appendChild(b));
  copie.appendChild(actions);
  hero.appendChild(copie);

  const affiche = el('button', 'soir-poster');
  affiche.type = 'button';
  affiche.setAttribute('aria-label', `Ouvrir la fiche de ${film.title}`);
  if (film.poster) {
    const image = document.createElement('img');
    image.src = film.poster;
    image.alt = '';
    affiche.appendChild(image);
  } else {
    affiche.appendChild(el('span', 'soir-poster-none', film.title));
  }
  affiche.addEventListener('click', () => openSheet(film.id));
  hero.appendChild(affiche);

  soirPeindreFaits(film);
  soirPeindreBillets(film);
  soirPeindreVoir(film.id);
}

function soirBouton(texte, classe, action) {
  const bouton = el('button', `soir-btn${classe ? ` ${classe}` : ''}`, texte);
  bouton.type = 'button';
  bouton.addEventListener('click', action);
  return bouton;
}

/** Le bouton « à voir » suit l'état de la liste, que d'autres écrans modifient aussi. */
function soirPeindreVoir(id) {
  const bouton = $('soir-want');
  if (!bouton || !soir.hero || soir.hero.id !== id) return;
  const voulu = state.watchlist.has(id);
  bouton.textContent = voulu ? 'Dans ma liste' : 'À voir plus tard';
  bouton.setAttribute('aria-pressed', String(voulu));
  bouton.classList.toggle('is-on', voulu);
}

function soirPeindreFaits(film) {
  const box = document.querySelector('#soir-hero .soir-facts');
  if (!box || !soir.hero || soir.hero.id !== film.id) return;
  box.textContent = '';
  const morceaux = [
    film.year,
    formatRuntime(film.runtime),
    soirNomsGenres(film).slice(0, 3).join(', '),
    (film.directors || []).length ? `de ${film.directors.slice(0, 2).join(', ')}` : null,
  ].filter(Boolean);
  morceaux.forEach((texte) => box.appendChild(el('span', null, String(texte))));
  if (film.votes > 0) {
    const note = el('span', 'soir-score', film.rating.toFixed(1).replace('.', ','));
    note.appendChild(el('small', null, 'sur 10'));
    box.appendChild(note);
  }
}

/* Les billets : une source de visionnage par billet, du plus immédiat au plus long. */

function soirPeindreBillets(film) {
  const box = $('soir-tickets');
  if (!box || !soir.hero || soir.hero.id !== film.id) return;
  box.textContent = '';
  const billets = [];
  const dispo = state.avail.get(film.id) || {};
  const plex = dispo.plex;

  if (plex) billets.push(billetPlex(film, plex, false));

  const plateformes = dispo.providers || [];
  if (plateformes.length) {
    const autres = plateformes.length - 1;
    billets.push(billet({
      petit: 'Abonnement',
      grand: `Inclus avec ${plateformes[0].name}${autres ? ` et ${autres} autre${autres > 1 ? 's' : ''}` : ''}`,
    }));
  }

  if (state.tracker.enabled && !plex) {
    const resultat = state.torrentCache.get(film.id);
    if (!resultat) {
      if (soir.chargement && soir.chargement.torrents) {
        billets.push(billet({ petit: 'À télécharger', grand: 'Recherche sur le tracker…' }));
      }
    } else if (resultat.error) {
      billets.push(billet({
        petit: 'À télécharger', grand: 'Tracker injoignable', info: resultat.error,
      }));
    } else if ((resultat.torrents || []).length) {
      billets.push(billetTorrent(resultat.torrents, (bouton) => soirVersions(film, bouton)));
    }
  }

  const enCours = soir.chargement && (soir.chargement.dispo || soir.chargement.torrents);
  if (!billets.length) {
    billets.push(billet(enCours
      ? { petit: 'Où le regarder', grand: 'Recherche en cours…' }
      : { petit: 'Où le regarder', grand: 'Pas encore disponible chez toi' }));
  }
  billets.forEach((b, i) => { if (i === 0) b.classList.add('is-best'); box.appendChild(b); });
}

/** Déplie la liste des versions du tracker sous les billets : c'est là qu'on
 *  choisit laquelle télécharger, le billet ne le fait pas à notre place. */
function soirVersions(film, bouton) {
  const boite = $('soir-versions');
  if (!boite || !soir.hero || soir.hero.id !== film.id) return;
  if (boite.childElementCount) {
    boite.textContent = '';
    if (bouton) bouton.textContent = 'Choisir';
    return;
  }
  const resultat = state.torrentCache.get(film.id);
  if (!resultat) return;
  boite.appendChild(el('h3', 'soir-versions-title', `Versions sur ${state.tracker.name}`));
  boite.appendChild(renderTorrents(resultat));
  if (bouton) bouton.textContent = 'Fermer';
  boite.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function soirRenderOthers() {
  const box = $('soir-others');
  box.textContent = '';
  const autres = soir.pool.slice(0, 5);
  box.hidden = !autres.length;
  autres.forEach((film) => {
    const bouton = el('button', 'soir-pick');
    bouton.type = 'button';
    const affiche = el('span', 'soir-pick-poster');
    if (film.poster) {
      const image = document.createElement('img');
      image.src = film.poster;
      image.alt = '';
      image.loading = 'lazy';
      affiche.appendChild(image);
    }
    bouton.appendChild(affiche);
    bouton.appendChild(el('b', null, film.title));
    const dispo = state.avail.get(film.id);
    const connu = state.availKnown.has(film.id);
    const ligne = el('span', 'soir-pick-where');
    let classe = 'is-none';
    let texte = '';
    if (dispo && dispo.plex) { classe = 'is-plex'; texte = 'Sur ton Plex'; }
    else if (dispo && (dispo.providers || []).length) { classe = 'is-stream'; texte = `Inclus avec ${dispo.providers[0].name}`; }
    else if (connu) { texte = 'Pas sur tes services'; }
    if (texte) {
      ligne.appendChild(el('i', `dot ${classe}`));
      ligne.appendChild(document.createTextNode(texte));
    }
    bouton.appendChild(ligne);
    bouton.setAttribute('aria-label', `${film.title}${texte ? `, ${texte}` : ''}. Choisir ce film.`);
    bouton.addEventListener('click', () => soirChoisirFilm(film.id));
    box.appendChild(bouton);
  });
}

/* ------------------------------- démarrage --------------------------------- */

function soirInit() {
  if (soir.ready) {
    // Les réglages ont changé : la phrase doit proposer ce qui existe encore.
    if (!soirSourcesDispo().includes(soir.source)) soir.source = 'all';
    if (!classementsProposes().some((c) => c.code === soir.certification)) soir.certification = null;
    soirRenderSentence();
    return;
  }
  soir.ready = true;
  soirCharger();
  if (!soirSourcesDispo().includes(soir.source)) soir.source = 'all';
  if (!classementsProposes().some((c) => c.code === soir.certification)) soir.certification = null;
  soirRenderSentence();

  document.addEventListener('click', (event) => {
    if (soir.menu && !event.target.closest('.soir-menu, .soir-choice')) soirFermerMenu(false);
  });
  soirMontrerSuivant();
}
