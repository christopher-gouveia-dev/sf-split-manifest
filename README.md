# split_manifest.py

Génère, segmente et retrieve des métadonnées Salesforce depuis une org.

## Prérequis

- **Python 3.10+** (stdlib uniquement, aucun `pip install` requis)
- **Salesforce CLI** (`sf`) installé et dans le PATH → `npm install -g @salesforce/cli`
- **Node.js 18+** (requis par SF CLI)
- Une **org authentifiée** : `sf org login web -a monAlias`

---

## Installation

Copier les fichiers dans un dossier sf-split-manifest/ (nom peut être changé) dans scripts/, dossier auto-généré avec le projet SFDX.

---

## Utilisation avec Powershell/Git Bash

Commandes à exécuter avec Powershell/Git Bash, si le nom du dossier du script est sf-split-manifest/ (sinon adapter) :

```powershell
# 1. Générer les manifests uniquement (pas de retrieve)
python scripts/sf-split-manifest -o monAlias

# 2. Générer + retriever TOUT
python scripts/sf-split-manifest -o monAlias -r

# 3. Générer + retriever uniquement les types search-priority (recommandé)
python scripts/sf-split-manifest -o monAlias -r -s

# 4. Idem, avec 4 retrieves en parallèle
python scripts/sf-split-manifest -o monAlias -r -s -p 4
```

### Options principales

| Option | Défaut | Description |
|---|---|---|
| `--from-org / -o` | *(requis)* | Alias de l'org Salesforce |
| `--retrieve / -r` | off | Lance les retrieves après le split |
| `--search / -s` | off | Retrieve uniquement les types search-priority |
| `--parallel / -p` | 3 | Nombre de retrieves en parallèle |
| `--output-dir / -d` | `manifest/` | Dossier de sortie des manifests |
| `--logs-dir` | `logs/` | Dossier des logs de retrieve |
| `--dry-run` | off | Affiche ce qui serait retrievé sans l'exécuter |

---

## Sorties

- `manifest/` — fichiers `*.xml` segmentés par type (ex: `apex.xml`, `objects_def_1.xml`)
- `logs/` — un fichier `.log` par manifest avec statut, durée et erreurs éventuelles

Les types marqués 🔍 (search-priority) sont retrievés avec `-s`. Les types 📦 sont skippés et la commande manuelle est affichée pour les lancer séparément.

---

## Notes

- Le batch size est configuré par segment dans `SEGMENT_BATCH_SIZES` (ex: 50 pour `CustomObject`).
- Pour ajouter ou reclasser un type metadata, éditer le dictionnaire `SEGMENTS` en tête de script.