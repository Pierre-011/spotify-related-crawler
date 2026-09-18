# Spotify Related Artists Crawler

Crawler automatique basé EXCLUSIVEMENT sur le site web public de Spotify.

## Principe

Le projet n'utilise pas :

- Spotify Web API
- Spotipy
- token Spotify
- Client ID / Client Secret

Il utilise uniquement Playwright + Chromium pour ouvrir les pages Spotify comme un navigateur et inspecter le DOM/HTML rendu par JavaScript.

Pour chaque artiste :

1. ouvre `https://open.spotify.com/intl-fr/artist/ID/related`
2. attend le rendu JavaScript
3. inspecte les liens `/artist/ID`
4. extrait les IDs des artistes liés
5. compare ces IDs à `data/artists.json`
6. ouvre la page `/artist/ID` des nouveaux artistes
7. inspecte le DOM/HTML rendu
8. extrait les informations disponibles
9. ajoute le profil à `artists.json`
10. ajoute le nouvel artiste à la file d'exploration
11. sauvegarde immédiatement
12. continue avec le suivant

## Persistance

`data/state.json` contient :

- `queue`: artistes restant à explorer
- `processed`: artistes déjà parcourus
- `stats`: statistiques
- `last_run`: date de la dernière sauvegarde

Ainsi, une exécution GitHub Actions peut s'arrêter puis reprendre sans repartir de zéro.

## GitHub Actions

Le workflow `.github/workflows/crawler.yml` :

- peut être lancé manuellement ;
- est planifié toutes les 15 minutes ;
- lance Chromium en mode headless ;
- exécute le crawler pendant environ 9 minutes ;
- sauvegarde `artists.json` et `state.json` ;
- commit les modifications dans le dépôt.

GitHub Actions n'est pas un processus infini : la continuité est obtenue par des exécutions successives + la file persistante.

## GitHub Pages

`index.html` est une interface statique qui lit `data/artists.json` et `data/state.json` depuis le même dépôt.

Dans GitHub :

Settings → Pages → Deploy from a branch → choisir `main` et `/ (root)`.

## Important

Spotify peut modifier le DOM de son site. Les sélecteurs et l'extraction du texte peuvent donc nécessiter une adaptation si la structure de Spotify change.

Le crawler ne contourne pas une authentification, ne résout pas de CAPTCHA et ne tente pas de contourner une mesure anti-bot.

Respectez les conditions d'utilisation et les règles applicables au site que vous automatisez.
