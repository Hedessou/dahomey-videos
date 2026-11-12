# Histoire du Dahomey : vidéos quotidiennes gratuites

Remplace JSON2Video. La vidéo est fabriquée gratuitement sur GitHub Actions :
voix Microsoft (edge-tts, voix Denise), images Wikimedia Commons, sous-titres incrustés, ffmpeg.

```
Make (07:00) ─ Google Sheet ─ IA (script) ─► GitHub Actions (rendu vidéo)
                                                   │ release GitHub (lien .mp4)
Make « Dahomey — publier la vidéo » ◄──────────────┘
   └─ PostPeer (YouTube + TikTok) ─ mise à jour du Google Sheet
```

## Mise en place (une seule fois)

1. Sur github.com, crée un dépôt **public** nommé `dahomey-videos` (compte `Hedessou`).
   Public = minutes Actions gratuites illimitées et lien vidéo lisible par PostPeer.
2. Dans Termux :
   ```bash
   cd ~ && unzip dahomey-videos.zip && cd dahomey-videos
   git init -b main && git add . && git commit -m "Rendu vidéo Dahomey"
   git remote add origin https://github.com/Hedessou/dahomey-videos.git
   git push -u origin main
   ```
3. Dépôt → Settings → Secrets and variables → Actions → **New repository secret**
   - Nom : `MAKE_PUBLISH_WEBHOOK`
   - Valeur : `https://hook.eu1.make.com/4mb6awgrmekt5yehcc82utmi0ou4zp9d`
4. Test : onglet **Actions** → « Rendu épisode Dahomey » → **Run workflow** (laisse « publier » décoché).
   Après 3–5 min, la vidéo est dans **Releases** (et en artefact). Regarde-la.
5. Jeton pour Make : github.com → Settings → Developer settings → Fine-grained tokens → Generate.
   Accès : seulement `dahomey-videos`. Permission : **Contents : Read and write**.
6. Dans Make, scénario « 🇧🇯 Histoire du Bénin — série quotidienne », module
   « Lancer le rendu sur GitHub » : remplace `COLLE_TON_TOKEN_GITHUB_ICI` par le jeton
   (garde `Bearer ` devant). Ouvre aussi le module « Écrire l'épisode (IA) » une fois et clique OK.
   Enregistre, puis **Run once**. Si tout passe, active le planning (07:00).

## Réglages

- Voix : variable `VOICE` (ex. `fr-FR-HenriNeural` pour une voix d'homme), vitesse `VOICE_RATE`.
- Hashtags : variable `HASHTAGS` dans `render.py`.
- Test local : `FAKE_TTS=1 python render.py exemples/episode.json out`
