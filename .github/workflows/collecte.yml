name: Collecte historique

on:
  workflow_dispatch:   # lancement manuel depuis l'onglet Actions

permissions:
  contents: write      # autorise le commit du CSV

jobs:
  collecte:
    runs-on: ubuntu-latest
    steps:
      - name: Récupérer le repo
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Installer les dépendances
        run: pip install pandas requests

      - name: Lancer la collecte
        run: python collecte_historique.py

      - name: Committer le CSV dans le repo
        run: |
          git config user.name "collecte-bot"
          git config user.email "bot@users.noreply.github.com"
          git add donnees/matchs_historique.csv
          git commit -m "Collecte historique $(date +%Y-%m-%d)" || echo "Rien à committer"
          git push
